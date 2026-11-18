"""设备 WebSocket 服务：连接管理 + 消息队列 + 设备注册表（单例）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed

from deskbot_server.constants import (
    PB_ACK_END_GRACE_SEC,
    PB_ACK_END_TIMEOUT_SEC,
    PB_ACK_WINDOW_TIMEOUT_SEC,
    PB_CHUNK_GAP_SEC,
    PB_MAX_PCM_BIN_BYTES,
    SAFE_SEND_TIMEOUT,
)
from deskbot_server.model.pb_seq import PbBlock, PbSeq, PbType, PlaybackOutcome, PlaybackStatus
from deskbot_server.pb.media_wire import CAPABILITIES as MEDIA_CAPABILITIES
from deskbot_server.pb.media_wire import MediaWire
from deskbot_server.pb.wire import device_pb_json_msg
from deskbot_server.service.application.asr_chat_uplink import pack_ws_downlink_frame, parse_packed_frame
from deskbot_server.service.application.playback_echo_guard import PlaybackEchoGuard
from deskbot_server.service.camera_face_service import CameraFaceService
from deskbot_server.utils.async_helpers import spawn
from deskbot_server.utils.singleton import SingletonMeta
from deskbot_server.utils.util import _json_msg
from deskbot_server.utils.ws_utils import WsUtils

logger = logging.getLogger("deskbot-server")


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------

_WINDOW_SIZE = 10
_IDLE_TIMEOUT = 300
_PB_IDLE_SEC = 10.0     # PbSeq 空闲超时：超过此时长无下发则触发 LiveService
_IDLE = object()         # _dequeue 超时哨兵

# 连接接管的有界等待上限（防卡死）：
# 曾发生同设备 3ms 双连触发接管竞态 → register() 在旧连接清理上无限等待，
# 此后每条新连接都卡死、设备永远收不到 ready，形成 ~14.6s 重连风暴且不自愈。
# 原则：新连接的服务绝不依赖旧连接清理完成；任何清理步骤超时只告警不阻塞。
_SUPERSEDE_CLOSE_TIMEOUT = 1.0   # 顶替旧连接时 ws.close() 的有界等待（僵尸 peer 不答 close 握手）
_RETIRE_TIMEOUT = 2.0            # 退役 worker（seq/dl 任务）的整体有界等待


@dataclass
class _PbDownlinkJob:
    """ws 下行 pb 发送任务。"""
    wire: str | bytes
    binaries: list[bytes] = field(default_factory=list)
    generation: int | None = None   # 入队时的设备代数；worker 发送前校验，过期 job 直接丢弃
    done: asyncio.Event = field(default_factory=asyncio.Event)
    ok_json: bool = False
    ok_bins: bool = True


@dataclass
class _DeviceEntry:
    """单个设备的统一状态：元数据 + ws 连接 + 消息队列。"""

    # ── 元数据 ──
    device_id: str
    first_seen_ts: float = field(default_factory=time.time)
    last_seen_ts: float = field(default_factory=time.time)
    online: bool = True
    last_pb_ack: dict[str, Any] | None = None
    last_pb_ack_ts: float = 0.0
    last_pb_ack_mono: float = 0.0
    last_pb_send_mono: float = 0.0    # 最后一次 PbBlock 发送的单调时间戳
    convo_ts: float = 0.0             # 最后一轮对话结束的 wall-clock 时间（touch_device 打点）
    last_status: str | None = None
    event_count: int = 0

    # ── ws 连接（每个设备同时只有一条连接）──
    ws: Any = None  # WebSocket | None
    generation: int = 0  # 连接代数：每次抢占槽位 +1；worker/job 按代数门控，旧代任务不得向新连接发送

    # ── PbSeq 消息队列（优先级 + 抢占 + ACK 流控）──
    queue: list = field(default_factory=list)                           # list[PbSeq]
    ack_queue: Any = field(default_factory=asyncio.Queue)               # asyncio.Queue[dict]
    seq_task: Any = None                                                # PbSeq 队列协程
    event: asyncio.Event = field(default_factory=asyncio.Event)
    sending_seq: Any = None                                             # PbSeq | None
    stopped: bool = False
    capabilities: set[str] = field(default_factory=set)
    anim_version: str = ""
    media_wire: MediaWire = field(default_factory=MediaWire)
    speech_epoch: int = 0
    speech_revision: int = 0
    confirmed_asr_mono: float = 0.0
    audio_ready_mono: float = 0.0
    barge_until_mono: float = 0.0
    raw_preview_seen: bool = False
    conversation_provider: str = ""  # Set when the primary realtime session attaches.
    conversation_session: Any = None
    rom_waiters: dict[str, asyncio.Future] = field(default_factory=dict)
    user_engaged_until_mono: float = 0.0
    echo_guard: PlaybackEchoGuard = field(default_factory=PlaybackEchoGuard)

    # ── pb downlink 队列（帧打包 + 串行发送）──
    dl_queue: Any = None                                                # asyncio.Queue[_PbDownlinkJob]
    dl_task: Any = None                                                 # downlink worker 协程


# ---------------------------------------------------------------------------
# pb wire 日志 + 帧打包
# ---------------------------------------------------------------------------


def _log_pb_tx_wire(device_id: str, payload: dict, wire: str, *, pcm_bytes: int = 0) -> None:
    audio_n = int((payload.get("audio") or {}).get("next_bin_len") or 0)
    logger.info(
        "[pb TX] device_id=%s req=%s type=%s idx=%s chunk_ms=%s "
        "anim_n=%d servo_n=%d audio_next_bin_len=%d wire_json %s",
        device_id,
        payload.get("req"),
        payload.get("type"),
        payload.get("idx"),
        payload.get("chunk_ms"),
        len(payload.get("anim") or []) if isinstance(payload.get("anim"), list) else 0,
        len(payload.get("servo") or []) if isinstance(payload.get("servo"), list) else 0,
        audio_n,
        wire,
    )


def _expected_pb_bin_lens(wire: str) -> list[int]:
    try:
        from deskbot_server.pb.servo_pcm import pb_expected_binary_lengths
        data = json.loads(wire)
        if isinstance(data, dict):
            return pb_expected_binary_lengths(data)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return []


# ---------------------------------------------------------------------------
# DeviceWsService
# ---------------------------------------------------------------------------


class DeviceWsService(metaclass=SingletonMeta):
    """设备 WebSocket 服务（单例）。

    统一管理：设备元数据、ws 连接生命周期、PbSeq 消息队列 + 窗口流控、pb 帧下行。
    每个设备只有一条主实时连接；相机等纯上行连接不占用该槽位。

    下行管道（两层队列串行）：
    send(PbSeq) → PbSeq 队列（优先级/抢占/ACK 流控）
      → _device_loop 取出 PbSeq → _do_send_to_device(PbBlock)
        → pb downlink 队列（帧打包/二进制校验/串行 ws.send）
    """

    def __init__(self) -> None:
        # ── 设备索引 ──
        self._devices: dict[str, _DeviceEntry] = {}
        self._lock = asyncio.Lock()
        # 每设备最近一帧 camera_frame（mono_ts, jpeg bytes），供实时对话按轮采样贴图
        self._camera_frame_cache: dict[str, tuple[float, bytes]] = {}

        # ── PbSeq 队列 ──
        self._queue_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

        # ── 应用依赖（bind 注入）──
        self._bus_service: Any | None = None
        self._live_service: Any = None  # LiveService | None

    @staticmethod
    def instance() -> DeviceWsService | None:
        """返回已有实例，未创建时返回 None。"""
        return SingletonMeta._instances.get(DeviceWsService)  # type: ignore[attr-defined]

    def is_device_online(self, device_id: str) -> bool:
        """判断设备是否在线。"""
        entry = self._devices.get(str(device_id or "").strip())
        return entry is not None and entry.online

    def latest_camera_frame(self, device_id: str | None, *, max_age_s: float = 45.0) -> bytes | None:
        """最近一帧 camera_frame（jpeg bytes）；超过 ``max_age_s``（monotonic）视为过期。

        语音轮开始时采样，作为该轮机器人「看到的」画面；播放期间固件暂停上行，
        缓存通常即说话句尾一帧。无人脸跟踪期间也可能无帧，返回 None。
        """
        key = str(device_id or "").strip()
        entry = self._camera_frame_cache.get(key)
        if entry is None:
            return None
        mono_ts, jpeg = entry
        if time.monotonic() - mono_ts > max_age_s:
            self._camera_frame_cache.pop(key, None)
            return None
        return jpeg

    def _mark_device_online(self, device_id: str) -> None:
        """测试辅助：标记设备为在线（不建立真实 ws 连接）。"""
        did = str(device_id or "").strip()
        if not did:
            return
        if did not in self._devices:
            self._devices[did] = _DeviceEntry(device_id=did)
        self._devices[did].online = True

    # ======================================================================
    # 生命周期
    # ======================================================================

    def bind(self, *, bus_service: Any | None = None) -> None:
        """注入设备事件总线；语音识别、对话与合成均由外部服务负责。"""
        self._bus_service = bus_service

    def bind_live_service(self, ls: Any) -> None:
        """注入 LiveService（用于 PbSeq 空闲唤醒）。"""
        self._live_service = ls

    @property
    def bus_service(self):
        """暴露给应用层的 BusService 引用。"""
        return self._bus_service

    async def shutdown(self) -> None:
        """停止所有设备协程，清理资源。"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
        async with self._lock:
            entries = list(self._devices.values())
        for entry in entries:
            if entry.conversation_session is not None:
                await entry.conversation_session.close()
            await self._retire_workers(entry)
        logger.info("[DeviceWsService] Shutdown complete")

    # ======================================================================
    # 连接管理（一个设备同时只有一条主实时连接）
    # ======================================================================

    async def register(self, device_id: str, ws, *, claim_slot: bool = True) -> None:
        """注册设备连接。

        ``claim_slot=True``（asr_chat）：抢占设备唯一下行槽位——**先换** ``entry.ws``
        并递增 ``generation``，同步摘除并取消旧代 seq/dl worker 后立即拉起新代 worker，
        最后**有界**关闭旧连接；新连接的服务绝不依赖旧连接清理完成，任一步超时只告警。

        ``claim_slot=False``（device_pipeline 生产者等）：只登记在线状态，不覆盖
        ``entry.ws``，避免把设备的真实 asr_chat 连接挤掉造成反复重连。
        """
        if not device_id:
            return

        from deskbot_server.service.device_identity_service import ensure_device_identity
        from deskbot_server.utils.async_helpers import run_blocking

        device_id = await run_blocking(ensure_device_identity, device_id)

        if not self._cleanup_task:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        is_new = False
        old_ws = None
        async with self._lock:
            now = time.time()
            entry = self._devices.get(device_id)
            if entry is None:
                entry = _DeviceEntry(device_id=device_id, first_seen_ts=now, last_seen_ts=now)
                self._devices[device_id] = entry
                is_new = True
            entry.last_seen_ts = now
            entry.online = True
            if claim_slot and entry.ws is not ws:
                old_ws = entry.ws
                entry.ws = ws
                entry.generation += 1
                entry.capabilities.clear()
                entry.media_wire = MediaWire()
                entry.anim_version = ""
                entry.echo_guard = PlaybackEchoGuard()
                entry.confirmed_asr_mono = 0.0
                entry.audio_ready_mono = 0.0
                entry.barge_until_mono = 0.0
                entry.raw_preview_seen = False
                entry.conversation_provider = ""
                entry.conversation_session = None
                entry.user_engaged_until_mono = 0.0

        if claim_slot:
            # 队列 worker 换代：摘除旧任务并取消（纯同步，无 await，不等待其退出；
            # 旧任务即使滞留也由 generation 门控，无法向新连接发送或污染共享状态）
            async with self._queue_lock:
                entry.stopped = False
                old_seq = entry.seq_task
                if old_seq is not None and not old_seq.done():
                    old_seq.cancel()
                entry.seq_task = None
                for queued in entry.queue:
                    queued.finish(PlaybackStatus.DISCONNECTED, reason="connection_replaced")
                entry.queue.clear()
                old_dl = entry.dl_task
                if old_dl is not None and not old_dl.done():
                    old_dl.cancel()
                entry.dl_queue = None
                entry.dl_task = None
                while not entry.ack_queue.empty():
                    try:
                        entry.ack_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                entry.event.set()
                entry.seq_task = asyncio.create_task(
                    self._device_loop(device_id, entry.generation)
                )

            # 有界关闭被顶替的旧连接（正常毫秒级完成；僵尸 peer 超时即放行）
            if old_ws is not None:
                await self._close_old_connection(device_id, old_ws)
        else:
            # 非抢占注册：仅确保 PbSeq 队列协程存活
            async with self._queue_lock:
                entry.stopped = False
                if entry.seq_task is None or entry.seq_task.done():
                    entry.seq_task = asyncio.create_task(
                        self._device_loop(device_id, entry.generation)
                    )

        logger.info(
            "[DeviceWsService] %s device_id=%s 设备数=%d",
            "新设备" if is_new else "复用设备",
            device_id,
            len(self._devices),
        )

    async def unregister(self, device_id: str, ws) -> None:
        """注销设备连接。幂等：仅当 ``ws`` 仍是当前连接时清理；worker 退役整体有界。

        注意：被新连接顶替的旧连接走 ``register`` 内的换代路径，此处身份校验不通过
        直接返回，避免旧连接的 teardown 反手停掉新注册的队列协程。
        """
        if not device_id:
            return
        async with self._lock:
            entry = self._devices.get(device_id)
            if entry is None or entry.ws is not ws:
                return
            entry.ws = None
            entry.online = False
            entry.last_seen_ts = time.time()

        await self._retire_workers(entry)

    async def _close_old_connection(self, device_id: str, old_ws) -> None:
        """有界关闭被顶替的旧连接（worker 已由 register 换代，这里只关 ws）。"""
        logger.info("[DeviceWsService] 关闭旧连接 device_id=%s", device_id)
        try:
            await asyncio.wait_for(
                old_ws.close(code=1000, reason="superseded by new connection"),
                timeout=_SUPERSEDE_CLOSE_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "[DeviceWsService] 旧连接 close 超时(%.1fs) device_id=%s —— 新连接已接管，交由连接自身收敛",
                _SUPERSEDE_CLOSE_TIMEOUT,
                device_id,
            )
        except Exception:
            logger.debug("[DeviceWsService] 旧连接 close 异常 device_id=%s", device_id, exc_info=True)

    # ======================================================================
    # 连接查询
    # ======================================================================

    def _get_ws(self, device_id: str):
        """返回设备的 WebSocket 连接（无连接返回 None）。"""
        entry = self._devices.get(device_id)
        return entry.ws if entry is not None else None

    # ======================================================================
    # PbSeq 消息发送（上层队列：优先级 + 抢占 + ACK 流控）
    # ======================================================================

    async def send(
        self, device_id: str, pb_seq: PbSeq, *, wait: bool = False, expected_generation: int | None = None
    ) -> int:
        """发送 PbSeq 到设备。

        wait=True 时阻塞到设备播完（设备回 pb_end 后解除）。
        返回 0=失败（无连接或被丢弃），1=入队成功。
        """
        if not device_id:
            return 0

        async with self._queue_lock:
            entry = self._devices.get(device_id)
            if entry is not None and expected_generation is not None and entry.generation != expected_generation:
                pb_seq.finish(PlaybackStatus.DISCONNECTED, reason="connection_replaced")
                return 0
            if entry is None or entry.stopped:
                logger.warning("[send] 设备不可用 device_id=%s entry=%s stopped=%s", device_id, entry is not None, getattr(entry, 'stopped', None) if entry else None)
                pb_seq.finish(PlaybackStatus.DROPPED, reason="device_unavailable")
                return 0
            n = self._enqueue(entry, pb_seq)
            if n > 0:
                entry.event.set()

        if wait and n > 0:
            await pb_seq._done.wait()
        return n

    async def send_and_wait(self, device_id: str, pb_seq: PbSeq, *, expected_generation: int | None = None) -> PlaybackOutcome:
        """发送并返回明确播放终态；旧 ``send`` 接口继续返回入队结果以保持兼容。"""
        accepted = await self.send(device_id, pb_seq, wait=False, expected_generation=expected_generation)
        if accepted:
            await pb_seq._done.wait()
        return pb_seq.outcome or pb_seq.finish(PlaybackStatus.DROPPED, reason="not_accepted")

    async def rom_command(self, device_id: str, op: str, arguments: dict, *, media: bytes = b"", timeout: float = 8,
                          expected_generation: int | None = None) -> dict:
        """Send a connection-scoped ROM command and await its actual result."""
        entry = self._devices.get(device_id)
        if entry is None or entry.ws is None or entry.stopped:
            return {"ok": False, "error": "设备未连接"}
        if expected_generation is not None and entry.generation != expected_generation:
            return {"ok": False, "status": "cancelled", "error": "设备连接已变更"}
        if "rom_tools_v1" not in entry.capabilities:
            return {"ok": False, "error": "当前 ROM 不支持设备工具，请升级固件"}
        if op == "image" and "image_rgb565_v1" not in entry.capabilities:
            return {"ok": False, "error": "当前 ROM 不支持图片显示，请升级固件"}
        if op not in {"volume", "image", "stop_image"} or len(entry.rom_waiters) >= 8:
            return {"ok": False, "error": "设备指令无效或过于繁忙"}
        command_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        entry.rom_waiters[command_id] = future
        packet = {"type": "rom_command", "id": command_id, "op": op, "arguments": arguments, "next_bin_len": len(media)}
        try:
            if not await self._enqueue_dl(entry, json.dumps(packet), binaries=[media] if media else [], generation=entry.generation):
                return {"ok": False, "error": "设备指令发送失败"}
            async with asyncio.timeout(timeout):
                result = await future
            expected_status = "displayed" if op == "image" else "completed"
            if result.get("ok") and result.get("status") != expected_status:
                return {"ok": False, "error": "设备回执没有确认操作完成"}
            if result.get("ok") and op == "volume" and (type(result.get("volume")) is not int or not 0 <= result["volume"] <= 100):
                return {"ok": False, "error": "设备返回的音量无效"}
            if result.get("ok") and op == "volume" and type(result.get("volume")) is int and 0 <= result["volume"] <= 100:
                from deskbot_server.dao.device_mapper import update_volume
                await asyncio.to_thread(update_volume, device_id, result["volume"])
            return result
        except TimeoutError:
            return {"ok": False, "error": "设备未在时限内确认指令"}
        finally:
            entry.rom_waiters.pop(command_id, None)

    def accept_rom_result(self, device_id: str, data: dict) -> None:
        entry = self._devices.get(device_id)
        command_id = data.get("id")
        if entry is not None and isinstance(command_id, str):
            future = entry.rom_waiters.get(command_id)
            if future is not None and not future.done() and isinstance(data.get("ok"), bool):
                future.set_result({k: v for k, v in data.items() if k in {"ok", "status", "error", "volume", "width", "height"}})

    async def open_speech_stream(self, device_id: str, req: str, *, sr: int, fmt: str, level: int = 1, voice_key: str = "",
                                 expected_generation: int | None = None):
        from deskbot_server.service.application.pb_stream import PbSpeechStream
        async with self._queue_lock:
            entry = self._devices.get(device_id)
            if entry is not None and expected_generation is not None and entry.generation != expected_generation:
                return None
            if entry is None or entry.stopped or "pb_stream_v1" not in entry.capabilities:
                return None
            entry.speech_epoch += 1
            seq = PbSeq(req=req[:36], sr=sr, fmt="opus" if fmt == "opus" else "s16le", level=level, epoch=entry.speech_epoch,
                        source=asyncio.Queue(maxsize=4), voice_key=voice_key)
            if not self._enqueue(entry, seq):
                raise RuntimeError("speech stream not accepted")
            entry.event.set()
            return PbSpeechStream(seq)

    def note_spoken_text(self, device_id: str, req: str, text: str) -> None:
        entry = self._devices.get(device_id)
        if entry:
            entry.echo_guard.note_text(req, text)

    async def interrupt_current_playback(self, device_id: str, *, reason: str = "user_turn", expected_req: str | None = None) -> int:
        """Stop current/queued PB work for an explicit user interruption.

        Normal application turns no longer use REPLACE against one another.  This
        is the single explicit barge-in path: a recognized user utterance clears
        stale queued speech and asks the worker to cancel the sequence on device.
        Returns the number of affected sequences.
        """
        dev = str(device_id or "").strip()
        if not dev:
            return 0
        affected = 0
        async with self._queue_lock:
            entry = self._devices.get(dev)
            if entry is None or entry.stopped:
                return 0
            keep = []
            for queued in entry.queue:
                if expected_req is not None and queued.req != expected_req:
                    keep.append(queued)
                    continue
                queued.finish(PlaybackStatus.PREEMPTED, reason=reason)
                affected += 1
            entry.queue[:] = keep
            sending = entry.sending_seq
            if sending is not None and not sending._done.is_set() and (expected_req is None or sending.req == expected_req):
                entry.ack_queue.put_nowait(
                    {
                        "type": "pb_cancel",
                        "status": PlaybackStatus.PREEMPTED.value,
                        "reason": reason,
                    }
                )
                affected += 1
        if affected:
            logger.info(
                "[interaction] 用户插话中止播放 device_id=%s affected=%d reason=%s",
                dev,
                affected,
                reason,
            )
        return affected

    async def ack(self, device_id: str, ack: dict) -> None:
        """将 ACK 通知转发给设备队列。"""
        async with self._queue_lock:
            entry = self._devices.get(device_id)
            if entry is None or entry.stopped:
                return
            await entry.ack_queue.put(ack)

    # ======================================================================
    # PbSeq 队列（优先级 + 抢占 + ACK 流控）
    # ======================================================================

    @staticmethod
    def _enqueue(entry: _DeviceEntry, new_seq: PbSeq) -> int:
        """按 level + action 规则将 PbSeq 插入队列。返回 0=丢弃，1=入队。"""
        q = entry.queue
        dev = entry.device_id
        new_info = f"req={new_seq.req} level={new_seq.level} action={new_seq.action.wire}"

        while q:
            old = q[-1]
            cmp = new_seq.compare(old)
            if cmp == 1:
                q.pop()
                old.finish(PlaybackStatus.PREEMPTED, reason=f"evicted_by:{new_seq.req}")
                logger.debug("[_enqueue] %s evict queued req=%s level=%d action=%s", dev, old.req, old.level, old.action.wire)
            elif cmp == -1:
                new_seq.finish(PlaybackStatus.DROPPED, reason=f"lower_than_queued:{old.req}")
                logger.debug("[_enqueue] %s drop (lower priority) %s", dev, new_info)
                return 0
            else:
                q.append(new_seq)
                logger.debug("[_enqueue] %s coexist %s", dev, new_info)
                return 1

        sending = entry.sending_seq
        if sending is None:
            q.append(new_seq)
            logger.debug("[_enqueue] %s enqueue (idle) %s", dev, new_info)
            return 1

        cmp = new_seq.compare(sending)
        if cmp == -1:
            new_seq.finish(PlaybackStatus.DROPPED, reason=f"lower_than_running:{sending.req}")
            logger.debug("[_enqueue] %s drop (lower than running) %s", dev, new_info)
            return 0
        if cmp == 1:
            q.append(new_seq)
            entry.ack_queue.put_nowait({
                "type": "pb_cancel",
                "status": PlaybackStatus.PREEMPTED.value,
                "reason": f"replaced_by:{new_seq.req}",
            })
            logger.debug("[_enqueue] %s preempt running -> pb_cancel, %s", dev, new_info)
            return 1
        q.append(new_seq)
        logger.debug("[_enqueue] %s enqueue (after running) %s", dev, new_info)
        return 1

    async def _device_loop(self, device_id: str, generation: int = 0):
        """每个设备的 PbSeq 队列协程：取 PbSeq → 逐 block 发送 → 等 ACK。

        ``generation`` 为该 worker 服务的连接代数：换代后被顶替的旧 worker 即使未
        及时退出，也会在下一个发送点因代数不匹配主动退出，且退出时不得回写共享状态。
        """
        entry = self._devices.get(device_id)
        if not entry:
            return
        try:
            while not entry.stopped and entry.generation == generation:
                pb_seq = await self._dequeue(entry, timeout=_PB_IDLE_SEC)
                if pb_seq is None:
                    break
                if pb_seq is _IDLE:
                    # Local idle programs must get an uninterrupted quiet
                    # interval. Legacy polling would continually reset it.
                    if self._live_service is not None and "local_idle_v1" not in entry.capabilities:
                        live_seq = self._live_service.get_live_pb_seq(device_id)
                        if live_seq is not None:
                            async with self._queue_lock:
                                self._enqueue(entry, live_seq)
                            entry.event.set()
                    continue
                entry.sending_seq = pb_seq
                outcome_status = PlaybackStatus.COMPLETED
                outcome_reason = ""
                while not entry.ack_queue.empty():
                    try:
                        entry.ack_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    if pb_seq.source is not None:
                        outcome_status, outcome_reason = await self._send_stream(entry, pb_seq, generation)
                        continue
                    entries = pb_seq.entries
                    n = len(entries)
                    req = pb_seq.req
                    seq_level = pb_seq.level
                    seq_sr = pb_seq.sr
                    seq_fmt = pb_seq.fmt
                    seq_ch = pb_seq.ch
                    i = 0
                    while i < n and not entry.stopped and entry.generation == generation:
                        batch_end = min(i + _WINDOW_SIZE, n)
                        for bi in range(i, batch_end):
                            if not await self._do_send_to_device(
                                device_id, entries[bi],
                                level=seq_level, sr=seq_sr, fmt=seq_fmt, ch=seq_ch, voice_key=pb_seq.voice_key,
                                generation=generation,
                            ):
                                outcome_status = PlaybackStatus.DISCONNECTED
                                outcome_reason = "downlink_unavailable"
                                return
                        is_last_window = batch_end >= n
                        if is_last_window:
                            t_pb_end_wait = time.monotonic()
                            logger.info(
                                "[pb TX] %s 末窗口已下发 req=%s last_idx=%d 等待 pb_end",
                                device_id, req, entries[batch_end - 1].idx,
                            )
                        ack_timeout = max(PB_ACK_WINDOW_TIMEOUT_SEC,
                                          sum(max(0, int(b.chunk_ms or 0)) for b in entries[i:batch_end]) / 1000 + PB_ACK_END_GRACE_SEC)
                        if is_last_window:
                            declared_sec = sum(max(0, int(item.chunk_ms or 0)) for item in entries) / 1000.0
                            ack_timeout = max(
                                PB_ACK_END_TIMEOUT_SEC,
                                declared_sec + PB_ACK_END_GRACE_SEC,
                            )
                        ack_type = await self._wait_ack(
                            entry, req, want_end=is_last_window, timeout=ack_timeout,
                            min_idx=entries[batch_end-1].idx if "pb_epoch_v1" in entry.capabilities else 0,
                        )
                        if is_last_window and ack_type == "pb_end":
                            logger.info(
                                "[pb ACK] %s 收到 pb_end req=%s 末窗口到播毕 %.0fms",
                                device_id, req, (time.monotonic() - t_pb_end_wait) * 1000,
                            )
                        if ack_type == "pb_cancel":
                            outcome_status = PlaybackStatus.PREEMPTED
                            outcome_reason = "replaced"
                            cancel_block = PbBlock(type=PbType.CANCEL, req=req, idx=0, epoch=pb_seq.epoch)
                            await self._do_send_to_device(device_id, cancel_block, generation=generation)
                            break
                        if ack_type in {"pb_timeout", "pb_error"}:
                            outcome_status = PlaybackStatus.TIMEOUT if ack_type == "pb_timeout" else PlaybackStatus.DROPPED
                            outcome_reason = "pb_ack_timeout" if ack_type == "pb_timeout" else "device_playback_error"
                            logger.warning(
                                "[pb ACK] %s 设备错误或等待超时 req=%s last_idx=%d want_end=%s reason=%s，取消当前链",
                                device_id, req, entries[batch_end - 1].idx, is_last_window, outcome_reason,
                            )
                            cancel_block = PbBlock(type=PbType.CANCEL, req=req, idx=0, epoch=pb_seq.epoch)
                            await self._do_send_to_device(device_id, cancel_block, generation=generation)
                            break
                        i = batch_end
                except asyncio.CancelledError:
                    outcome_status = PlaybackStatus.DISCONNECTED
                    outcome_reason = "device_worker_cancelled"
                    raise
                except ValueError as exc:
                    outcome_status = PlaybackStatus.DROPPED
                    outcome_reason = f"invalid_playback:{exc}"
                    logger.warning("[DeviceWsService] rejected playback device=%s req=%s: %s", device_id, pb_seq.req, exc)
                except Exception:
                    outcome_status = PlaybackStatus.DISCONNECTED
                    outcome_reason = "device_worker_error"
                    raise
                finally:
                    if entry.sending_seq is pb_seq:
                        entry.sending_seq = None
                    pb_seq.finish(outcome_status, reason=outcome_reason)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("[DeviceWsService] Device loop error for %s: %s", device_id, e)
        finally:
            # 只有自己仍是该设备当前代 worker 才回写共享状态；
            # 被顶替的旧代 worker 退出时不得污染新代状态（曾因此把新 worker 的
            # entry.stopped 置 True，导致换代后队列协程立刻静默退出）
            if entry.generation == generation:
                entry.stopped = True
                entry.sending_seq = None
                entry.event.set()

    async def _send_stream(self, entry, seq, generation):
        from dataclasses import replace
        # Both input and ACKs remain live. This sends the first audio at once,
        # then fills up to one second ahead without a stop-and-wait RTT per block.
        get = asyncio.create_task(seq.source.get())
        ack = asyncio.create_task(entry.ack_queue.get())
        pending = None
        inflight = {}
        last_idx = -1
        end_idx = None
        total_ms = 0
        started = time.monotonic()
        deadline = started + 45
        status, reason = PlaybackStatus.DISCONNECTED, "connection_replaced"
        try:
            while entry.generation == generation and not entry.stopped:
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                ready = [ack]
                can_read = end_idx is None and pending is None
                if can_read:
                    ready.append(get)
                if pending is not None and (pending.type == PbType.END or
                                            ((not inflight or sum(inflight.values()) + pending.chunk_ms <= 1000) and len(inflight) < 4)):
                    block, pending = pending, None
                    if not await self._do_send_to_device(entry.device_id, block, level=seq.level,
                                                         sr=seq.sr, fmt=seq.fmt, ch=seq.ch, generation=generation, voice_key=seq.voice_key):
                        return PlaybackStatus.DISCONNECTED, "downlink_unavailable"
                    last_idx = block.idx
                    total_ms += block.chunk_ms
                    inflight[block.idx] = block.chunk_ms
                    if block.type == PbType.END:
                        end_idx = block.idx
                        deadline = time.monotonic() + max(PB_ACK_END_TIMEOUT_SEC, total_ms / 1000 + 3)
                    elif len(inflight) == 1:
                        deadline = time.monotonic() + max(PB_ACK_WINDOW_TIMEOUT_SEC, block.chunk_ms / 1000 + 3)
                    continue
                done, _ = await asyncio.wait(ready, timeout=max(0, deadline-time.monotonic()),
                                              return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    status, reason = PlaybackStatus.TIMEOUT, "pb_ack_timeout" if inflight else "producer_idle"
                    break
                if ack in done:
                    value = ack.result()
                    ack = asyncio.create_task(entry.ack_queue.get())
                    if value.get("type") == "pb_cancel":
                        status, reason = PlaybackStatus.PREEMPTED, "replaced"
                        break
                    if value.get("req") == seq.req and value.get("epoch") == seq.epoch:
                        kind, idx = value.get("ack_type"), int(value.get("idx", -1))
                        if kind == "pb_error":
                            status, reason = PlaybackStatus.DROPPED, value.get("error") or "device_audio_error"
                            break
                        if kind == "pb_end" and end_idx is not None and idx == end_idx:
                            logger.info("[pb stream] completed device_id=%s req=%s epoch=%d duration_ms=%d wall_ms=%.0f",
                                        entry.device_id, seq.req, seq.epoch, total_ms, (time.monotonic()-started)*1000)
                            return PlaybackStatus.COMPLETED, ""
                        if kind == "pb_credit" and idx in inflight and idx <= last_idx:
                            inflight = {i: ms for i, ms in inflight.items() if i > idx}
                            if end_idx is None:
                                deadline = time.monotonic() + (PB_ACK_WINDOW_TIMEOUT_SEC+1 if inflight else 45)
                if get in done:
                    pending = get.result()
                    get = asyncio.create_task(seq.source.get())
        finally:
            for task in (get, ack):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get, ack, return_exceptions=True)
        cancel = replace(PbBlock(type=PbType.CANCEL, req=seq.req), epoch=seq.epoch)
        await self._do_send_to_device(entry.device_id, cancel, generation=generation)
        return status, reason

    async def _dequeue(self, entry: _DeviceEntry, timeout: float = 0) -> PbSeq | None:
        while True:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            async with self._queue_lock:
                if entry.queue:
                    return entry.queue.pop(0)
                if entry.stopped:
                    return None
                entry.event.clear()
                if entry.queue:
                    return entry.queue.pop(0)
            try:
                await asyncio.wait_for(entry.event.wait(), timeout=timeout or None)
            except TimeoutError:
                return _IDLE  # type: ignore[return-value]

    async def _wait_ack(
        self,
        entry: _DeviceEntry,
        req: str,
        *,
        want_end: bool = False,
        timeout: float | None = None,
        epoch: int = 0,
        min_idx: int = 0,
    ) -> str | None:
        """等待匹配 req 的 pb_ack（消费 ack_queue）。

        - ``pb_cancel``（_enqueue 抢占哨兵，无 req）优先返回；
        - 非匹配 req 的 ack 一律跳过；
        - ``want_end=False``（非末窗口）：任意同 req ack 即放行（兼容旧行为）；
        - 同 req 的 ``pb_error`` 立即返回；支持索引的设备拒收落后于窗口的 ACK；
        - ``want_end=True``（末窗口）：必须等到 ``ack_type == "pb_end"``，
          期间 ``pb_chunk`` 等同 req ack 静默消费。
        """
        if timeout is None:
            timeout = PB_ACK_END_TIMEOUT_SEC if want_end else PB_ACK_WINDOW_TIMEOUT_SEC
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "pb_timeout"
            try:
                ack = await asyncio.wait_for(entry.ack_queue.get(), timeout=remaining)
            except TimeoutError:
                return "pb_timeout"
            if ack.get("type") == "pb_cancel":
                return "pb_cancel"
            if ack.get("req") != req:
                continue
            ack_type = ack.get("ack_type") or ack.get("type")
            if epoch and ack.get("epoch") != epoch:
                continue
            if ack_type == "pb_error":
                return ack_type
            if int(ack.get("idx", -1)) < min_idx:
                continue
            if epoch and not want_end and ack_type != "pb_credit":
                continue
            if not want_end:
                return ack_type
            if ack_type == "pb_end":
                return "pb_end"
            # 末窗口：其余 ack（pb_chunk 等）继续等待 pb_end

    # ======================================================================
    # pb downlink（下层队列：帧打包 + 二进制校验 + 串行 ws.send）
    # ======================================================================

    async def _do_send_to_device(
        self, device_id: str, block: PbBlock,
        *, level: int = 1, sr: int = 0, fmt: str = "", ch: int = 0,
        generation: int | None = None,
        voice_key: str = "",
    ) -> bool:
        """PbBlock → wire JSON → pb downlink 队列 → ws.send。

        ``generation``：调用方服务的连接代数；与当前代数不一致（被顶替/换代）时
        立即放弃，返回 False 让旧代队列协程退出，杜绝旧代任务向新连接发帧。
        """
        t0 = time.monotonic()
        ws = None
        async with self._lock:
            entry = self._devices.get(device_id)
            if entry is not None:
                ws = entry.ws
                if generation is not None and entry.generation != generation:
                    logger.debug(
                        "[_do_send] %s req=%s 代数过期 gen=%d cur=%d 放弃",
                        device_id, block.req, generation, entry.generation,
                    )
                    return False
        if ws is None:
            return False
        if ((block.audio is not None or block.servo or block.stream) and "local_face_pack_v1" in entry.capabilities and
                block.anim_version and block.anim_version != entry.anim_version):
            # An offline/failed resource update must not break speech. Optional
            # visuals stay local; never fall back to transmitting drawing code.
            # Keep servo commands and stream termination independent of assets too.
            from dataclasses import replace
            block = replace(block, anim=(), anim_version="")
        uses_phonemes = any(a.phoneme_id for a in block.anim)
        uses_frames = any(a.anim_id for a in block.anim)
        if ((block.anim_id and "anim_id_v1" not in entry.capabilities) or
                (uses_phonemes and "phoneme_id_v1" not in entry.capabilities) or
                (uses_frames and "anim_frame_id_v1" not in entry.capabilities) or
                ((block.anim_id or uses_phonemes or uses_frames) and block.anim_version and
                 block.anim_version != entry.anim_version)):
            logger.warning("[pb TX] local expression unavailable device_id=%s id=%s version=%s device_version=%s",
                           device_id, block.anim_id, block.anim_version, entry.anim_version)
            return False
        payload = block.to_wire(level=level, sr=sr, fmt=fmt, ch=ch)
        if voice_key and block.audio is not None and block.type in (PbType.START, PbType.SINGLE):
            payload["voice_key"] = voice_key
        entry.media_wire.capabilities = entry.capabilities.intersection(MEDIA_CAPABILITIES)
        payload, binary_frame = entry.media_wire.downlink(payload, list(block.binaries))
        if binary_frame is not None:
            wire = binary_frame
            logger.debug("[pb binary TX] device_id=%s epoch=%d idx=%d bytes=%d",
                         device_id, block.epoch, block.idx, len(binary_frame))
        else:
            wire = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                    if "pb_compact_v1" in entry.media_wire.capabilities else device_pb_json_msg(payload))
            _log_pb_tx_wire(device_id, payload, wire, pcm_bytes=sum(len(b) for b in block.binaries))
        sent = await self._enqueue_dl(entry, wire,
                                      None if binary_frame is not None else list(block.binaries), generation=generation)
        entry.last_pb_send_mono = time.monotonic()
        elapsed = (time.monotonic() - t0) * 1000
        if elapsed > 50:
            logger.warning(
                "[_do_send] %s req=%s idx=%d type=%s send_ms=%.0f",
                device_id, block.req, block.idx, block.type.wire, elapsed,
            )
        return sent

    async def _enqueue_dl(
        self,
        entry: _DeviceEntry,
        wire: str | bytes,
        binaries: list[bytes] | None = None,
        pcm: bytes | None = None,
        *,
        generation: int | None = None,
    ):
        """将 pb 帧排入 downlink 队列，阻塞到发送完成。"""
        async with self._queue_lock:
            q = entry.dl_queue
            if q is None:
                q = entry.dl_queue = asyncio.Queue()
            if entry.dl_task is None or entry.dl_task.done():
                # worker 显式携带队列对象：换代时 entry.dl_queue 会被整体替换，
                # 旧 worker 只消费自己绑定的旧队列，不因 entry 字段变化而错乱
                entry.dl_task = asyncio.create_task(self._dl_worker(entry, q))
        bins = list(binaries or [])
        if pcm and (not bins or bins[0] is not pcm):
            bins = [pcm] + bins
        job = _PbDownlinkJob(wire=wire, binaries=bins, generation=generation)
        await q.put(job)
        await job.done.wait()
        return job.ok_json and job.ok_bins

    async def _dl_worker(self, entry: _DeviceEntry, q: asyncio.Queue | None = None):
        """单设备 downlink worker：从队列取 job，打包帧 + 校验 + 串行 ws.send。

        只消费创建时绑定的队列（换代后 entry.dl_queue 被替换，旧 worker 排空即退）；
        发送前校验 job 代数，过期 job 丢弃，绝不对新代连接发送旧代内容。
        """
        q = q if q is not None else entry.dl_queue
        while True:
            job: _PbDownlinkJob | None = await q.get()
            try:
                if job is None:
                    break
                if job.generation is not None and job.generation != entry.generation:
                    logger.debug(
                        "[pb TX] 代数过期 job 丢弃 device_id=%s gen=%d cur=%d",
                        entry.device_id, job.generation, entry.generation,
                    )
                    continue
                ws = entry.ws
                if ws is None:
                    continue
                expect_lens = [] if isinstance(job.wire, bytes) else _expected_pb_bin_lens(job.wire)
                got_lens = [len(b) for b in job.binaries]
                if expect_lens and expect_lens != got_lens:
                    logger.error(
                        "[pb TX] binary 长度与 JSON 声明不一致 device_id=%s expect=%s got=%s",
                        entry.device_id, expect_lens, got_lens,
                    )
                if job.binaries and got_lens and got_lens[0] > PB_MAX_PCM_BIN_BYTES:
                    logger.error(
                        "[pb TX] 首包 binary %d bytes 超过 PCM 建议上限 %d device_id=%s",
                        got_lens[0], PB_MAX_PCM_BIN_BYTES, entry.device_id,
                    )
                try:
                    frame = job.wire if isinstance(job.wire, bytes) else pack_ws_downlink_frame(job.wire, job.binaries)
                except ValueError as exc:
                    logger.error("[pb TX] pack failed device_id=%s err=%s", entry.device_id, exc)
                    continue
                async with WsUtils.get_ws_send_lock(ws):
                    ok = await WsUtils.safe_send_once(
                        ws, frame, timeout=WsUtils.send_timeout_for_message(frame, base=SAFE_SEND_TIMEOUT)
                    )
                job.ok_json = ok
                job.ok_bins = ok
                if not ok:
                    logger.warning("[pb TX] 下发失败 device_id=%s", entry.device_id)
                if PB_CHUNK_GAP_SEC > 0 and job.binaries:
                    await asyncio.sleep(PB_CHUNK_GAP_SEC)
            except Exception:
                logger.exception("[pb TX] worker 异常 device_id=%s", entry.device_id)
            finally:
                if job is not None:
                    job.done.set()
                with contextlib.suppress(ValueError):
                    q.task_done()

    # ======================================================================
    # PbSeq 队列生命周期
    # ======================================================================

    async def _retire_workers(self, entry: _DeviceEntry) -> None:
        """退役 entry 的全部 worker（seq/dl）：先同步摘除并取消（持锁、无 await），
        再在锁外**有界**等待退出。整体 ≤ ``_RETIRE_TIMEOUT``，超时只告警不阻塞——
        滞留的旧代任务由 generation 门控保证无害（无法发帧、退出时不写共享状态）。

        曾因「持 _queue_lock 无限 await seq_task」导致连接接管永久卡死，此处是硬约束：
        绝不在持有 _queue_lock/_lock 期间等待任何任务结束。
        """
        tasks: list[asyncio.Task] = []
        for future in list(entry.rom_waiters.values()):
            if not future.done():
                future.set_result({"ok": False, "error": "设备已断开或重连"})
        entry.rom_waiters.clear()
        async with self._queue_lock:
            entry.stopped = True
            for queued in entry.queue:
                queued.finish(PlaybackStatus.DISCONNECTED, reason="device_worker_retired")
            entry.queue.clear()
            if entry.sending_seq is not None:
                entry.sending_seq.finish(PlaybackStatus.DISCONNECTED, reason="device_worker_retired")
            if entry.seq_task is not None:
                if entry.seq_task.done():
                    entry.seq_task = None
                else:
                    task = entry.seq_task
                    tasks.append(task)
                    entry.seq_task = None
                    task.cancel()
            if entry.dl_task is not None:
                if entry.dl_task.done():
                    entry.dl_task = None
                else:
                    task = entry.dl_task
                    tasks.append(task)
                    entry.dl_task = None
                    task.cancel()
            entry.dl_queue = None
            while not entry.ack_queue.empty():
                try:
                    entry.ack_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            entry.event.set()
        if tasks:
            try:
                _done, pending = await asyncio.wait(tasks, timeout=_RETIRE_TIMEOUT)
            except asyncio.CancelledError:
                return  # 调用方自身被取消（如进程退出），worker 已摘除，无需善后
            for t in pending:
                logger.warning(
                    "[DeviceWsService] worker 退役超时(%.1fs) device_id=%s task=%s —— 已摘除，由代数门控隔离",
                    _RETIRE_TIMEOUT, entry.device_id, t.get_name(),
                )

    async def _cleanup_loop(self):
        """定期清理空闲超时的设备队列协程。"""
        try:
            while True:
                await asyncio.sleep(_IDLE_TIMEOUT / 2)
                now = time.time()
                to_stop: list[_DeviceEntry] = []
                async with self._queue_lock:
                    for entry in self._devices.values():
                        if entry.seq_task and not entry.seq_task.done() and (now - entry.last_seen_ts) > _IDLE_TIMEOUT:
                            to_stop.append(entry)
                for entry in to_stop:
                    await self._retire_workers(entry)
                    logger.info("[DeviceWsService] Cleanup idle device: %s", entry.device_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("[DeviceWsService] Cleanup error: %s", e)

    # ======================================================================
    # 设备查询
    # ======================================================================

    def snapshot(self) -> list:
        """返回所有设备的快照列表（按 last_seen 降序）。"""
        items = [self._snapshot_entry(e) for e in self._devices.values()]
        items.sort(key=lambda d: float(d.get("last_seen_ts") or 0.0), reverse=True)
        return items

    async def touch(self, device_id: str, status: str | None = None) -> None:
        """刷新设备 last_seen 时间戳。"""
        if not device_id:
            return
        async with self._lock:
            entry = self._devices.get(device_id)
            if entry is None:
                return
            entry.last_seen_ts = time.time()
            if status:
                entry.last_status = status
            entry.event_count += 1

    def note_convo(self, device_id: str) -> None:
        """记录一轮对话结束时刻（对话轮发布链路打点，与心跳 touch 区分）。"""
        if not device_id:
            return
        entry = self._devices.get(str(device_id).strip())
        if entry is None:
            return
        entry.convo_ts = time.time()

    def last_convo_ts(self, device_id: str) -> float:
        """最后一轮对话结束时刻（wall-clock）；设备未注册 / 无对话记录返回 0.0。"""
        if not device_id:
            return 0.0
        entry = self._devices.get(str(device_id).strip())
        return entry.convo_ts if entry is not None else 0.0

    async def record_pb_ack(self, device_id: str, ack: dict[str, Any]) -> None:
        """记录设备最近一次 pb_ack。"""
        if ack.get("ack_type") in {"pb_started", "pb_audio_end", "pb_cancelled", "pb_error"}:
            logger.info("[pb playback] device_id=%s event=%s req=%s epoch=%s idx=%s played_ms=%s queue_wait_ms=%s device_ms=%s",
                        device_id, ack.get("ack_type"), ack.get("req"), ack.get("epoch"), ack.get("idx"),
                        ack.get("played_ms"), ack.get("queue_wait_ms"), ack.get("device_ms"))
        if not device_id or not isinstance(ack, dict):
            return
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        await pb_ack_gate.notify(device_id, ack)
        async with self._lock:
            entry = self._devices.get(device_id)
            if entry is None:
                logger.warning("[pb_ack] 设备未在注册表，忽略 device_id=%s", device_id)
                return
            entry.last_pb_ack = dict(ack)
            entry.last_pb_ack_ts = time.time()
            entry.last_pb_ack_mono = time.monotonic()
            entry.echo_guard.acknowledge(ack)
            if entry.conversation_session is not None:
                entry.conversation_session.offer_ack(ack)

    async def pb_ack_llm_context(self, device_id: str | None) -> str | None:
        """返回设备最近一次 pb_ack 的 JSON（供 LLM 上下文注入）。"""
        if not device_id:
            return None
        async with self._lock:
            entry = self._devices.get(device_id)
            if not entry or not isinstance(entry.last_pb_ack, dict):
                return None
            return json.dumps(entry.last_pb_ack, ensure_ascii=False)

    @staticmethod
    def _snapshot_entry(entry: _DeviceEntry) -> dict:
        return {
            "device_id": entry.device_id,
            "first_seen_ts": entry.first_seen_ts,
            "last_seen_ts": entry.last_seen_ts,
            "online": entry.online,
            "conversation_provider": entry.conversation_provider,
            "last_status": entry.last_status,
            "event_count": entry.event_count,
            "last_pb_ack": entry.last_pb_ack,
            "last_pb_ack_ts": entry.last_pb_ack_ts,
            "capabilities": sorted(entry.capabilities),
            "anim_version": entry.anim_version,
        }

    # ======================================================================
    # WS Handler
    # ======================================================================

    async def handle_asr_chat(self, websocket, device_id: str | None) -> None:
        """设备媒体连接只接外部实时会话，不回退到本地模型组合。"""
        if not device_id:
            await websocket.close(code=1008, reason="device_id required")
            return
        from deskbot_server.service.conversation_service import conversation_provider
        from deskbot_server.service.moss_live_service import handle_moss_connection

        await handle_moss_connection(self, websocket, device_id, provider=conversation_provider(device_id))

    def _accept_camera_frame(self, device_id: str | None, media: bytes, *, source: str) -> None:
        """缓存最新帧并投递耗时业务；调用方的 WS 接收循环不等待处理完成。"""
        frame = bytes(media)
        if device_id:
            self._camera_frame_cache[device_id] = (time.monotonic(), frame)
        entry = self._devices.get(device_id)
        if entry is not None and entry.conversation_provider in {"moss-live", "flecket"}:
            if entry.conversation_session is not None:
                entry.conversation_session.offer_camera(frame)
            # Web preview is independent of upstream camera opt-in and face inference.
            spawn(CameraFaceService().try_emit_video_frame(device_id, frame, meta={"source": source}))
            spawn(CameraFaceService().process(device_id, frame, frame_source=source, log_channel=f"/{source}"))
            if self._bus_service is not None:
                spawn(self._broadcast_camera_frame(device_id, frame))
            return
        spawn(
            CameraFaceService().process(
                device_id,
                frame,
                frame_source=source,
                log_channel=f"/{source}",
            )
        )
        if self._bus_service is not None and device_id:
            spawn(self._broadcast_camera_frame(device_id, frame))

    async def _broadcast_camera_frame(self, device_id: str, frame: bytes) -> None:
        import base64

        await self._bus_service.broadcast(
            device_id,
            {
                "type": "camera_frame",
                "device_id": device_id,
                "data": base64.b64encode(frame).decode("ascii"),
            },
        )

    async def handle_camera_uplink(self, websocket, device_id: str | None) -> None:
        """独立相机上行；不注册为设备主连接，避免抢占 ``/asr_chat``。"""
        peer = WsUtils.peer_str(websocket)
        ready = _json_msg({"type": "ready", "channel": "camera", "device_id": device_id})
        if not await WsUtils.safe_send(websocket, ready):
            return
        logger.info("[/camera_uplink] ready device_id=%s peer=%s", device_id, peer)
        try:
            async for message in websocket:
                if not isinstance(message, (bytes, bytearray)):
                    continue
                frame = parse_packed_frame(bytes(message))
                if frame is None or frame.doc.get("type") != "camera_frame" or not frame.bin:
                    logger.warning(
                        "[/camera_uplink] invalid frame device_id=%s bytes=%d",
                        device_id,
                        len(message),
                    )
                    continue
                self._accept_camera_frame(device_id, frame.bin, source="camera_uplink")
                if device_id:
                    await self.touch(device_id)
        except ConnectionClosed as closed:
            logger.info("[/camera_uplink] closed device_id=%s peer=%s: %s", device_id, peer, closed)
