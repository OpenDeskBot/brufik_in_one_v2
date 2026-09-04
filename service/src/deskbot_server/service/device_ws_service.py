"""设备 WebSocket 服务：连接管理 + 消息队列 + 设备注册表（单例）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed

from deskbot_server.constants import PB_CHUNK_GAP_SEC, PB_MAX_PCM_BIN_BYTES, SAFE_SEND_TIMEOUT
from deskbot_server.model.pb_seq import PbBlock, PbSeq, PbType
from deskbot_server.pb.wire import device_pb_json_msg
from deskbot_server.service.application.asr_chat_uplink import pack_ws_downlink_frame, parse_packed_frame
from deskbot_server.service.application.boot_wake import deliver_boot_wake_scene
from deskbot_server.service.application.chat_service import ChatService
from deskbot_server.service.camera_face_service import CameraFaceService
from deskbot_server.service.pipeline.audio import AudioConfig, ConnectionSession
from deskbot_server.service.vad_service import VadService
from deskbot_server.utils.async_helpers import spawn
from deskbot_server.utils.singleton import SingletonMeta
from deskbot_server.utils.util import _json_msg, format_exc_detail
from deskbot_server.utils.ws_utils import WsUtils
from deskbot_server.ws.uplink_rate_stats import ensure_uplink_rate_stats_started, note_uplink_audio, remove_device

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
    wire: str
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
    每个设备同时只有一条 WebSocket 连接。

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
        self._pipeline: ChatService | None = None
        self._audio_cfg: AudioConfig | None = None
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
        entry = self._camera_frame_cache.get(str(device_id or "").strip())
        if entry is None:
            return None
        mono_ts, jpeg = entry
        if time.monotonic() - mono_ts > max_age_s:
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

    def bind(
        self,
        pipeline: ChatService,
        audio_cfg: AudioConfig,
        bus_service: Any | None = None,
    ) -> None:
        """注入全局依赖（应用启动时调用一次）。"""
        self._pipeline = pipeline
        self._audio_cfg = audio_cfg
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
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        async with self._lock:
            entries = list(self._devices.values())
        for entry in entries:
            await self._retire_workers(entry)
        logger.info("[DeviceWsService] Shutdown complete")

    # ======================================================================
    # 连接管理（一个设备同时只有一条连接）
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

        if claim_slot:
            # 队列 worker 换代：摘除旧任务并取消（纯同步，无 await，不等待其退出；
            # 旧任务即使滞留也由 generation 门控，无法向新连接发送或污染共享状态）
            async with self._queue_lock:
                entry.stopped = False
                old_seq = entry.seq_task
                if old_seq is not None and not old_seq.done():
                    old_seq.cancel()
                entry.seq_task = None
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
        self, device_id: str, pb_seq: PbSeq, *, wait: bool = False
    ) -> int:
        """发送 PbSeq 到设备。

        wait=True 时阻塞到设备播完（设备回 pb_end 后解除）。
        返回 0=失败（无连接或被丢弃），1=入队成功。
        """
        if not device_id:
            return 0

        async with self._queue_lock:
            entry = self._devices.get(device_id)
            if entry is None or entry.stopped:
                logger.warning("[send] 设备不可用 device_id=%s entry=%s stopped=%s", device_id, entry is not None, getattr(entry, 'stopped', None) if entry else None)
                if wait:
                    pb_seq._done.set()
                return 0
            n = self._enqueue(entry, pb_seq)
            if n > 0:
                entry.event.set()

        if wait:
            if n > 0:
                await pb_seq._done.wait()
            else:
                pb_seq._done.set()
        return n

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
                logger.debug("[_enqueue] %s evict queued req=%s level=%d action=%s", dev, old.req, old.level, old.action.wire)
            elif cmp == -1:
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
            logger.debug("[_enqueue] %s drop (lower than running) %s", dev, new_info)
            return 0
        if cmp == 1:
            q.append(new_seq)
            entry.ack_queue.put_nowait({"type": "pb_cancel"})
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
                    # 空闲超时：从 LiveService 获取待机 PbSeq 并直接入队
                    if self._live_service is not None:
                        live_seq = self._live_service.get_live_pb_seq(device_id)
                        if live_seq is not None:
                            async with self._queue_lock:
                                self._enqueue(entry, live_seq)
                            entry.event.set()
                    continue
                entry.sending_seq = pb_seq
                while not entry.ack_queue.empty():
                    try:
                        entry.ack_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
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
                                level=seq_level, sr=seq_sr, fmt=seq_fmt, ch=seq_ch,
                                generation=generation,
                            ):
                                return
                        is_last_window = batch_end >= n
                        if is_last_window:
                            t_pb_end_wait = time.monotonic()
                            logger.info(
                                "[pb TX] %s 末窗口已下发 req=%s last_idx=%d 等待 pb_end",
                                device_id, req, entries[batch_end - 1].idx,
                            )
                        ack_type = await self._wait_ack(entry, req, want_end=is_last_window)
                        if is_last_window and ack_type == "pb_end":
                            logger.info(
                                "[pb ACK] %s 收到 pb_end req=%s 末窗口到播毕 %.0fms",
                                device_id, req, (time.monotonic() - t_pb_end_wait) * 1000,
                            )
                        if ack_type == "pb_cancel":
                            cancel_block = PbBlock(type=PbType.CANCEL, req=req, idx=0)
                            await self._do_send_to_device(device_id, cancel_block, generation=generation)
                            break
                        i = batch_end
                finally:
                    entry.sending_seq = None
                    pb_seq._done.set()
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

    async def _dequeue(self, entry: _DeviceEntry, timeout: float = 0) -> PbSeq | None:
        while True:
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

    async def _wait_ack(self, entry: _DeviceEntry, req: str, *, want_end: bool = False) -> str | None:
        """等待匹配 req 的 pb_ack（消费 ack_queue）。

        - ``pb_cancel``（_enqueue 抢占哨兵，无 req）优先返回；
        - 非匹配 req 的 ack 一律跳过；
        - ``want_end=False``（非末窗口）：任意同 req ack 即放行（兼容旧行为）；
        - ``want_end=True``（末窗口）：必须等到 ``ack_type == "pb_end"``，
          期间 ``pb_chunk`` 等同 req ack 静默消费。
        """
        while True:
            ack = await entry.ack_queue.get()
            if ack.get("type") == "pb_cancel":
                return "pb_cancel"
            if ack.get("req") != req:
                continue
            ack_type = ack.get("ack_type") or ack.get("type")
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
        payload = block.to_wire(level=level, sr=sr, fmt=fmt, ch=ch)
        wire = device_pb_json_msg(payload)
        _log_pb_tx_wire(device_id, payload, wire, pcm_bytes=sum(len(b) for b in block.binaries))
        await self._enqueue_dl(entry, wire, list(block.binaries) if block.binaries else None, generation=generation)
        entry.last_pb_send_mono = time.monotonic()
        elapsed = (time.monotonic() - t0) * 1000
        if elapsed > 50:
            logger.warning(
                "[_do_send] %s req=%s idx=%d type=%s send_ms=%.0f",
                device_id, block.req, block.idx, block.type.wire, elapsed,
            )
        return True

    async def _enqueue_dl(
        self,
        entry: _DeviceEntry,
        wire: str,
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
                expect_lens = _expected_pb_bin_lens(job.wire)
                got_lens = [len(b) for b in job.binaries]
                if expect_lens and expect_lens != got_lens:
                    logger.error(
                        "[pb TX] binary 长度与 JSON 声明不一致 device_id=%s expect=%s got=%s",
                        entry.device_id, expect_lens, got_lens,
                    )
                if job.binaries:
                    if got_lens and got_lens[0] > PB_MAX_PCM_BIN_BYTES:
                        logger.error(
                            "[pb TX] 首包 binary %d bytes 超过 PCM 建议上限 %d device_id=%s",
                            got_lens[0], PB_MAX_PCM_BIN_BYTES, entry.device_id,
                        )
                try:
                    frame = pack_ws_downlink_frame(job.wire, job.binaries)
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
                try:
                    q.task_done()
                except ValueError:
                    pass

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
        async with self._queue_lock:
            entry.stopped = True
            if entry.seq_task is not None:
                if entry.seq_task.done():
                    entry.seq_task = None
                else:
                    tasks.append(entry.seq_task)
                    entry.seq_task = None
                    entry.seq_task.cancel()
            if entry.dl_task is not None:
                if entry.dl_task.done():
                    entry.dl_task = None
                else:
                    tasks.append(entry.dl_task)
                    entry.dl_task = None
                    entry.dl_task.cancel()
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
            "last_status": entry.last_status,
            "event_count": entry.event_count,
            "last_pb_ack": entry.last_pb_ack,
            "last_pb_ack_ts": entry.last_pb_ack_ts,
        }

    # ======================================================================
    # WS Handler
    # ======================================================================

    async def handle_asr_chat(self, websocket, device_id: str | None) -> None:
        """/asr_chat WS：新固件打包帧上行（音频 + 摄像头）；pb_ack 流控。"""
        pipeline = self._pipeline
        audio_cfg = self._audio_cfg
        if pipeline is None or audio_cfg is None:
            raise RuntimeError("DeviceWsService 尚未 bind，请先调用 bind() 注入依赖")

        try:
            session = VadService().create_connection_session(pipeline)
        except RuntimeError:
            session = ConnectionSession(pipeline, audio_cfg)
        peer = WsUtils.peer_str(websocket)

        ensure_uplink_rate_stats_started()
        if device_id:
            await self.register(device_id, websocket)
            logger.info("[/asr_chat] 接入 device_id=%s peer=%s", device_id, peer)
        else:
            logger.warning(
                "[/asr_chat] 接入缺失 device_id peer=%s —— 不会出现在 /api/devices 设备列表，"
                "请改用 ws://host:9000/asr_chat?device_id=<设备ID>",
                peer,
            )
        try:
            ready_json = _json_msg({"type": "ready", "device_id": device_id})
            ready_ok = await WsUtils.safe_send(websocket, ready_json)
            logger.info("[/asr_chat] ready device_id=%s peer=%s sent=%s", device_id, peer, ready_ok)
            if not ready_ok:
                return
            if device_id:
                await deliver_boot_wake_scene(self, device_id)

            async for message in websocket:
                try:
                    if isinstance(message, (bytes, bytearray)):
                        payload = bytes(message)
                        frame = parse_packed_frame(payload)
                        if frame is None:
                            logger.warning("[/asr_chat] 无法解析打包帧 device_id=%s bytes=%d", device_id, len(payload))
                            continue
                        data = frame.doc
                        attached_media = frame.bin
                        msg_type = data.get("type")

                        if msg_type == "audio":
                            if not attached_media:
                                logger.warning("[/asr_chat] audio 帧缺少 binary device_id=%s", device_id)
                                continue
                            codec = data.get("codec")
                            sr_raw = data.get("sr")
                            ch_raw = data.get("ch")
                            opus_frames_raw = data.get("opus_frames") or data.get("frames") or data.get("n_frames")
                            uplink_sr = int(sr_raw) if sr_raw is not None else audio_cfg.sample_rate
                            uplink_ch = int(ch_raw) if ch_raw is not None else audio_cfg.channels
                            note_uplink_audio(device_id)
                            await self.touch(device_id)
                            opus_frames = int(opus_frames_raw) if opus_frames_raw is not None else None
                            utterance, _, _ = await session.feed_audio(
                                attached_media, codec, sample_rate=uplink_sr, channels=uplink_ch, opus_frames=opus_frames
                            )
                            if utterance:
                                logger.info("[/asr_chat] VAD切句完成 device_id=%s pcm_bytes=%d -> 触发ASR", device_id, len(utterance))
                                spawn(self._run_asr_turn(
                                    websocket, pipeline, utterance,
                                    device_id=device_id,
                                    uplink_sr=session.rom_sr,
                                    uplink_ch=session.rom_ch,
                                    uplink_codec=session.rom_codec,
                                ))
                            continue

                        if msg_type == "camera_frame":
                            if attached_media:
                                if device_id:
                                    # 最近帧缓存：供该轮对话开始时按 request_id 留存画面
                                    self._camera_frame_cache[device_id] = (time.monotonic(), bytes(attached_media))
                                spawn(
                                    CameraFaceService().process(
                                        device_id, attached_media,
                                        frame_source="asr_chat", log_channel="/asr_chat",
                                    )
                                )
                                if self._bus_service is not None and device_id:
                                    import base64
                                    await self._bus_service.broadcast(device_id, {
                                        "type": "camera_frame",
                                        "device_id": device_id,
                                        "data": base64.b64encode(attached_media).decode("ascii"),
                                    })
                            continue

                        if msg_type == "boot_connect":
                            if device_id:
                                await deliver_boot_wake_scene(self, device_id)
                            continue

                        if msg_type == "pb_ack":
                            from deskbot_server.utils.util import _normalize_incoming_pb_ack

                            norm = _normalize_incoming_pb_ack(data)
                            if norm is not None and device_id:
                                async with self._lock:
                                    entry = self._devices.get(device_id)
                                    sending = entry.sending_seq if entry else None
                                    sending_req = sending.req if sending else None
                                    sending_level = sending.level if sending else None
                                logger.debug(
                                    "[pb_ack RX] device_id=%s ack_type=%s req=%s space=%s | sending_seq req=%s level=%s",
                                    device_id,
                                    norm.get("ack_type"),
                                    norm.get("req"),
                                    norm.get("space"),
                                    sending_req,
                                    sending_level,
                                )
                                await self.ack(device_id, norm)
                                await self.record_pb_ack(device_id, norm)
                            continue

                        logger.debug("[/asr_chat] 未知打包帧 type=%r device_id=%s", msg_type, device_id)
                        continue

                except Exception as exc:
                    logger.exception("处理客户端消息失败: %s", format_exc_detail(exc))
        except ConnectionClosed as closed:
            logger.info("WebSocket 已关闭: %s", closed)
        finally:
            # 调试：保存上行音频WAV
            try:
                session.save_debug_wav()
            except Exception:
                pass
            if device_id:
                remove_device(device_id)
                self._camera_frame_cache.pop(device_id, None)
                await self.unregister(device_id, websocket)

    async def _run_asr_turn(
        self,
        websocket,
        pipeline,
        pcm_segment: bytes,
        *,
        device_id: str | None = None,
        uplink_sr: int = 16000,
        uplink_ch: int = 1,
        uplink_codec: str = "opus",
    ) -> None:
        """VAD 切出一句后：ASR → LLM → TTS。"""
        from deskbot_server.service.application.capability_labels import asr_model_label
        from deskbot_server.service.application.convo_audio_store import ConvoAudioStore
        from deskbot_server.service.application.ws_chat_turn import publish_ws_chat_turn, run_ws_chat_turn
        from deskbot_server.service.asr_service import AsrService

        request_id = uuid.uuid4().hex[:16]
        sample_rate = uplink_sr or 16000
        # 声纹识别与 ASR 并行：每次 VAD 判定通过即对 utterance 做说话人识别（写快照，
        # 供本轮 LLM user 消息「声音识别」段读取）；任何装配异常都不阻塞 ASR 轮
        vpr_task = None
        vpr_wait_budget_s = 0.0
        try:
            from deskbot_server.service.voiceprint_service import VOICEPRINT_WAIT_BUDGET_S, VoiceprintService

            if VoiceprintService().enabled():
                vpr_task = asyncio.create_task(
                    VoiceprintService().identify(
                        device_id=device_id,
                        pcm_bytes=pcm_segment,
                        sample_rate=sample_rate,
                        request_id=request_id,
                    ),
                    name=f"voiceprint:{device_id}:{request_id}",
                )
                vpr_wait_budget_s = float(VOICEPRINT_WAIT_BUDGET_S)
        except Exception:
            logger.debug("[vpr] 声纹识别任务启动失败（忽略，不阻塞 ASR 轮）", exc_info=True)
            vpr_task = None
            vpr_wait_budget_s = 0.0
        seg_duration_ms = int(len(pcm_segment) / 2 / max(1, sample_rate) * 1000)
        logger.info("[ASR] 开始识别 device_id=%s req=%s pcm_bytes=%d audio_ms=%d sr=%d", device_id, request_id, len(pcm_segment), seg_duration_ms, sample_rate)
        t_asr_start = time.monotonic()

        try:
            text = await AsrService().transcribe(pcm_segment, sample_rate, device_id=device_id)
        except Exception:
            text = await pipeline.asr(pcm_segment, sample_rate=sample_rate, device_id=device_id)
        t_asr_text = time.monotonic()
        asr_ms = (t_asr_text - t_asr_start) * 1000

        if not text:
            logger.info(
                "[ASR] 结果为空 device_id=%s req=%s audio_ms=%d asr_ms=%.0f",
                device_id, request_id, seg_duration_ms, asr_ms,
            )
            return

        try:
            asr_ok = AsrService().is_valid_text(text, device_id=device_id)
        except Exception:
            asr_ok = pipeline.is_valid_asr_text(text, device_id=device_id)
        if not asr_ok:
            logger.info(
                "[ASR] 结果被过滤 device_id=%s req=%s audio_ms=%d asr_ms=%.0f text=%r",
                device_id, request_id, seg_duration_ms, asr_ms, text,
            )
            return

        logger.info(
            "[ASR] 识别成功 device_id=%s req=%s audio_ms=%d asr_ms=%.0f text=%r",
            device_id, request_id, seg_duration_ms, asr_ms, text,
        )

        # 先留存媒体（原声 / 最近帧），再发 asr_done：让用户气泡在 ASR 完成当下
        # 就能拿到音频与人脸图（live 广播即时渲染，不等 LLM/TTS 终态事件）
        # 采集门控：仅当后台有订阅者查看该设备实时对话时才留存（无人查看不采集）
        _bus = self._bus_service
        _watching = bool(_bus) and bool(getattr(_bus, "has_subscribers_sync", None) and _bus.has_subscribers_sync(device_id))
        audio_saved = False
        face_saved = False
        if _watching:
            audio_saved = ConvoAudioStore().put(device_id, request_id, "asr", pcm_segment, sample_rate=sample_rate)
            frame_jpeg = self.latest_camera_frame(device_id)
            face_saved = bool(frame_jpeg) and ConvoAudioStore().put_raw(device_id, request_id, "face", frame_jpeg)

        # asr_done stage：仅广播给页面订阅者，供前端先渲染用户气泡（不向设备下发）
        from deskbot_server.ws.stages import _emit_stage

        asr_model = asr_model_label(device_id)
        await _emit_stage(
            websocket, device_id, request_id, "asr_done",
            send_client=False,
            event_fields={
                "asr_text": text,
                "asr_ms": int(asr_ms),
                "asr_model": asr_model,
                "audio_asr": audio_saved,
                "face_img": face_saved,
                "source": "asr",
            },
            bus_service=self._bus_service,
        )

        # 等本句声纹判定落地再进对话轮（与 ASR 并行启动，通常已就绪；超时不阻塞，
        # 快照由后台任务补写，后续轮次/展示仍可读到）
        if vpr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(vpr_task), timeout=vpr_wait_budget_s)
            except Exception:
                pass

        # ── 对话注意力门控（闲聊过滤）：相机最近画面无人 + 本句声纹判定不是认识的
        # 人 → 疑似周围人在闲聊，跳过 LLM/TTS 流程。声纹只在引擎启用（起了识别
        # 任务）时采信；引擎无结论一律放行（避免故障期机器人「变哑」）。
        from deskbot_server.service.application.asr_attention_gate import decide_round
        from deskbot_server.service.application.voice_snapshot_cache import get_voice_snapshot

        vpr_engine_on = vpr_task is not None
        verdict = await decide_round(
            self,
            device_id,
            vpr_engine_on=vpr_engine_on,
            voice_snapshot=(get_voice_snapshot(device_id) if vpr_engine_on else None),
        )
        if not verdict.engage:
            logger.info(
                "[ASR] 忽略疑似闲聊 device_id=%s req=%s audio_ms=%d asr_ms=%.0f text=%r reason=%s note=%s",
                device_id, request_id, seg_duration_ms, asr_ms, text, verdict.reason, verdict.note,
            )
            # 静默不理会：只给页面订阅者留一条忽略标记（asr_done 已在上方发出）
            await _emit_stage(
                websocket,
                device_id,
                request_id,
                "asr_ignored",
                send_client=False,
                event_fields={
                    "asr_text": text,
                    "asr_ms": int(asr_ms),
                    "asr_model": asr_model,
                    "ignore_reason": verdict.reason,
                    "note": verdict.note,
                    "source": "asr",
                },
            )
            return

        try:
            flow = await run_ws_chat_turn(
                websocket,
                pipeline,
                text,
                request_id=request_id,
                device_ws=self,
                device_id=device_id,
                t_asr_start=t_asr_start,
                t_asr_text=t_asr_text,
                bus_service=self._bus_service,
            )
        except Exception as exc:
            logger.exception("[ASR] 对话轮次异常 device_id=%s req=%s", device_id, request_id)
            await publish_ws_chat_turn(
                self._bus_service,
                self,
                device_id,
                source="asr",
                asr_text=text,
                t_asr_start=t_asr_start,
                t_asr_text=t_asr_text,
                flow={"status": "error", "error": str(exc), "t_llm_end": t_asr_text, "t_tts_end": t_asr_text},
                request_id=request_id,
            )
            return
        await publish_ws_chat_turn(
            self._bus_service,
            self,
            device_id,
            source="asr",
            asr_text=text,
            t_asr_start=t_asr_start,
            t_asr_text=t_asr_text,
            flow=flow,
            request_id=request_id,
        )
