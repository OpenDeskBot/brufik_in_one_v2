"""在线设备 Live 待机序列生成（wander / sleep / gaze，PB_LEVEL_IDLE）。"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from deskbot_server.dao.device_mapper import get_live_mode
from deskbot_server.dao.face_expr_scenes_store import design_frames_to_pb_chain
from deskbot_server.pb.llm_plan import _resolve_servo_preset_steps, expand_llm_anims, expand_llm_moves
from deskbot_server.pb.shapes import PB_ACTION_APPEND, PB_LEVEL_IDLE, apply_pb_dispatch_fields
from deskbot_server.utils.singleton import SingletonMeta

logger = logging.getLogger("deskbot-server")

ENTER_SEC = 5.0
FACE_STALE_SEC = 0.7
FACE_TICK_MIN_SEC = 1.0
WANDER_MIN_CYCLES = 1
WANDER_MAX_CYCLES = 3
SLEEP_MIN_SEC = 30.0
SLEEP_MAX_SEC = 60.0

# ── 剧情主动推进（冷场触发）──
QUEST_ATTEMPT_IDLE_SEC = 60.0        # 距最后一轮对话超过此时长才尝试推进剧情
QUEST_CHECK_SEC = 5.0                # 人脸帧内检查节流
QUEST_NO_TASK_COOLDOWN_SEC = 60.0    # 无 running 任务 / 设备离线时空转冷却（避免高频查库）
_QUEST_DB_TS_CACHE_SEC = 30.0        # DB 会话表兜底时间戳的内存缓存时长

# ── 社交主动问候（与 quest 共用冷场判定/节流；SOCIAL_* 控制问候频率）──
SOCIAL_MIN_GAP_SEC = 300.0           # 相邻主动问候 attempt 的最小间隔（同在场面孔时）
SOCIAL_RETRY_GAP_SEC = 120.0         # 面孔集合变化（新面孔出现/返回）时的最短重试间隔
SOCIAL_NO_FACE_COOLDOWN_SEC = 60.0   # 无已识别已知用户 / 设备离线时空转冷却


class LiveState(str, Enum):
    SLEEP = "sleep"
    WANDER = "wander"
    GAZE = "gaze"


@dataclass
class _Dev:
    mode: LiveState = LiveState.WANDER
    last_face_send: float = 0.0
    last_happy: float = 0.0


# ---------------------------------------------------------------------------
# 舵机预设 + 序列构造辅助
# ---------------------------------------------------------------------------

def _hold(preset: str, hold_ms: int, *, device_id: str) -> dict[str, Any]:
    steps = _resolve_servo_preset_steps(preset, device_id=device_id)
    if not steps:
        return {"move": preset, "ms": hold_ms}
    last = steps[-1]
    return {
        "move": "__custom__",
        "ms": hold_ms,
        "x": int(last.get("x", 90)),
        "y": int(last.get("y", 90)),
        "xm": 0,
        "ym": 0,
    }


def wander_moves(device_id: str) -> list[dict[str, Any]]:
    return [
        {"move": "look_left", "ms": 1000},
        _hold("look_left", 2000, device_id=device_id),
        {"move": "look_right", "ms": 2000},
        _hold("look_right", 2000, device_id=device_id),
        {"move": "center", "ms": random.randint(1000, 3000)},
    ]


def sleep_moves(device_id: str, *, hold_ms: int) -> list[dict[str, Any]]:
    hold_ms = max(1000, int(hold_ms))
    return [
        {"move": "center", "ms": 800},
        {"move": "look_down", "ms": 1000},
        _hold("look_down", hold_ms, device_id=device_id),
    ]


def _scene_tail(scene: str, *, device_id: str, req: str, target_ms: int) -> list[dict[str, Any]]:
    frames = expand_llm_anims([{"anim": scene, "ms": max(500, int(target_ms))}], device_id=device_id)
    if not frames:
        return []
    design = [{"ms": int(fr["ms"]), "elements": fr["elements"]} for fr in frames]
    pairs = design_frames_to_pb_chain(design, runtime_req=req)
    if not pairs:
        return []
    msgs = [msg for msg, _ in pairs]
    apply_pb_dispatch_fields(msgs, action=PB_ACTION_APPEND, level=PB_LEVEL_IDLE)
    return msgs


# ---------------------------------------------------------------------------
# LiveService
# ---------------------------------------------------------------------------

class LiveService(metaclass=SingletonMeta):
    """生成待机 PbSeq（wander / sleep / gaze），PB 使用最低 level。"""

    def __init__(self) -> None:
        self._hub: Any = None
        self._devs: dict[str, _Dev] = {}
        # ── 剧情主动推进（_quest_runner 异步 attempt(device_id) -> bool）──
        self._quest_runner: Any = None
        self._quest_inflight: set[str] = set()
        self._quest_last_check: dict[str, float] = {}
        self._quest_next_ok: dict[str, float] = {}
        self._quest_db_ts: dict[str, tuple[float, float]] = {}
        # ── 社交主动问候（与 quest 共用冷场判定；节流状态独立）──
        self._social_runner: Any = None
        self._social_inflight: set[str] = set()
        self._social_last_attempt_m: dict[str, float] = {}
        self._social_last_names: dict[str, frozenset] = {}
        self._social_next_ok: dict[str, float] = {}

    def bind(self, hub: Any) -> None:
        self._hub = hub

    def bind_quest_runner(self, runner: Any) -> None:
        """注入剧本主动推进器（冷场 1 分钟且用户在时由 LiveService 调度调用）。"""
        self._quest_runner = runner

    def bind_social_runner(self, runner: Any) -> None:
        """注入社交主动问候推进器（冷场 1 分钟且识别到认识的人在面前时调度）。"""
        self._social_runner = runner

    @property
    def hub(self) -> Any:
        if self._hub is None:
            raise RuntimeError("LiveService 尚未 bind")
        return self._hub

    @staticmethod
    def active(device_id: str = "") -> bool:
        if not device_id:
            return True
        return get_live_mode(device_id)

    def _ensure(self, device_id: str) -> _Dev:
        st = self._devs.get(device_id)
        if st is None:
            st = _Dev()
            self._devs[device_id] = st
        return st

    def owns_face_tracking(self, device_id: str) -> bool:
        """当前是否由 LiveService 接管人脸跟随（gaze 模式）。"""
        if not self.active(device_id):
            return False
        st = self._devs.get(str(device_id or "").strip())
        return st is not None and st.mode == LiveState.GAZE

    # ── PbSeq 构造（供 _device_loop 空闲超时调用）──

    def get_live_pb_seq(self, device_id: str) -> Any | None:
        """随机返回一个 wander 或 sleep 的 PbSeq（level=0），供设备空闲时入队。"""
        from deskbot_server.model.pb_seq import PbAction, PbBlock, PbSeq, PbServo, PbType

        if not device_id or not self.active(device_id):
            return None

        # 随机选择 wander 或 sleep
        if random.random() < 0.5:
            moves = wander_moves(device_id)
            scene = "listening"
        else:
            dur = random.uniform(SLEEP_MIN_SEC, SLEEP_MAX_SEC)
            moves = sleep_moves(device_id, hold_ms=int(max(1000, (dur - 1.8) * 1000)))
            scene = "sleep"

        steps = expand_llm_moves(moves, device_id=device_id)
        if not steps:
            return None

        req_id = uuid.uuid4().hex[:16]
        total_ms = sum(max(1, int(s.get("ms") or 0)) for s in steps)
        servo = tuple(
            PbServo(xm=int(s["xm"]), ym=int(s["ym"]), x=int(s["x"]), y=int(s["y"]), ms=int(s["ms"]))
            for s in steps
        )
        servo_block = PbBlock(type=PbType.SINGLE, req=req_id, idx=0, chunk_ms=total_ms, servo=servo)
        tail = _scene_tail(scene, device_id=device_id, req=req_id, target_ms=total_ms)
        tail_entries = tuple(PbBlock.from_wire(f) for f in tail)
        entries = (servo_block,) + tail_entries
        pb_seq = PbSeq(req=req_id, entries=entries, level=0, action=PbAction.DEFAULT)
        logger.info(
            "[live] get_live_pb_seq device_id=%s req=%s blocks=%d scene=%s ms=%d",
            device_id, req_id, len(entries), scene, total_ms,
        )
        return pb_seq

    # ── gaze（由 camera_face_service 驱动）──

    async def on_face_tick(self, device_id: str, analysis: dict[str, Any]) -> None:
        dev = str(device_id or "").strip()
        if not dev or not self.active(dev) or not analysis.get("landmarks"):
            return
        # 用户在面前且长时间无对话 → 剧本主动推进（与 gaze 独立）
        await self._maybe_quest_attempt(dev)
        st = self._ensure(dev)
        if st.mode == LiveState.GAZE:
            await self._send_gaze(dev, analysis, st)
            return
        st.mode = LiveState.GAZE

    # ── 剧情主动推进（冷场 ≥1 分钟触发一轮任务尝试）──

    async def _maybe_quest_attempt(self, device_id: str) -> None:
        """人脸 tick 内节流判定；满足冷场条件时 spawn 一次主动尝试。

        社交问候优先于剧情推进：同一次 5s 节流内先试 social，social 已 spawn
        本 tick 就不再起 quest（两者共用 convo_ts 冷场时钟，quest 自然排队）。
        """
        if self._hub is None or (self._quest_runner is None and self._social_runner is None):
            return
        dev = str(device_id or "").strip()
        if not dev or dev in self._quest_inflight or dev in self._social_inflight:
            return
        now_m = time.monotonic()
        if now_m - self._quest_last_check.get(dev, 0.0) < QUEST_CHECK_SEC:
            return
        self._quest_last_check[dev] = now_m
        idle = await self._quiet_seconds(dev)
        if idle < QUEST_ATTEMPT_IDLE_SEC:
            return  # 最近 1 分钟内有对话轮

        await self._maybe_social_attempt(dev)
        if dev in self._social_inflight:
            return  # 本 tick 已 spawn 社交问候轮，不再起 quest
        if self._quest_runner is None:
            return
        if now_m < self._quest_next_ok.get(dev, 0.0):
            return  # quest 空转冷却（无任务/离线），不重复打扰
        self._quest_inflight.add(dev)
        asyncio.create_task(self._run_quest_attempt(dev), name=f"quest_attempt_{dev[:8]}")

    async def _run_quest_attempt(self, device_id: str) -> None:
        try:
            started = await self._quest_runner.attempt(device_id)
            if not started:
                # 无 running 任务 / 设备离线 → 冷却后复查
                self._quest_next_ok[device_id] = time.monotonic() + QUEST_NO_TASK_COOLDOWN_SEC
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[live] quest attempt 异常 device_id=%s", device_id)
        finally:
            self._quest_inflight.discard(device_id)

    # ── 社交主动问候（冷场 + 识别到认识的人时触发）──

    @staticmethod
    def _known_face_names(device_id: str) -> list[str]:
        """当前人脸快照中已匹配到档案的名字（升序去重，最多 4 个）。"""
        try:
            from deskbot_server.service.application.face_snapshot_cache import list_recognized_faces

            rows = list_recognized_faces(device_id, limit=4) or []
        except Exception:
            return []
        names: list[str] = []
        for row in rows:
            name = str(row.get("person_name") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    async def _maybe_social_attempt(self, device_id: str) -> None:
        """社交问候节流判定：冷却/无已识别已知用户跳过；spawn 一次问候尝试。"""
        runner = self._social_runner
        if runner is None:
            return
        dev = str(device_id or "").strip()
        if not dev or dev in self._social_inflight:
            return
        now_m = time.monotonic()
        if now_m < self._social_next_ok.get(dev, 0.0):
            return  # 空转冷却中
        names = self._known_face_names(dev)
        if not names:
            # 无已识别已知用户：空转冷却，避免每 5s 重复判脸打扰
            self._social_next_ok[dev] = now_m + SOCIAL_NO_FACE_COOLDOWN_SEC
            return
        changed = frozenset(names) != self._social_last_names.get(dev)
        gap = now_m - self._social_last_attempt_m.get(dev, 0.0)
        min_gap = SOCIAL_RETRY_GAP_SEC if changed else SOCIAL_MIN_GAP_SEC
        if gap < min_gap:
            return  # 间隔内不重复打扰（新面孔出现可提前重试，防识别闪烁）
        self._social_inflight.add(dev)
        asyncio.create_task(
            self._run_social_attempt(dev, names=names, t_attempt=now_m), name=f"social_attempt_{dev[:8]}"
        )

    async def _run_social_attempt(self, device_id: str, *, names: list[str], t_attempt: float) -> None:
        try:
            started = await self._social_runner.attempt(device_id)
            if started:
                # 已发起轮（开口或静默由 LLM 定）：更新 attempt 节流基线
                self._social_last_attempt_m[device_id] = t_attempt
                self._social_last_names[device_id] = frozenset(names)
            else:
                # 设备离线 / 无已识别已知用户 → 冷却后复查
                self._social_next_ok[device_id] = time.monotonic() + SOCIAL_NO_FACE_COOLDOWN_SEC
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[live] social attempt 异常 device_id=%s", device_id)
        finally:
            self._social_inflight.discard(device_id)

    async def _quiet_seconds(self, device_id: str) -> float:
        """距最后一轮对话的秒数（内存打点优先，进程冷启动时用会话表 updated_at 兜底）。

        从未有过任何对话 → 返回极大值（视为冷场已满足）。
        """
        hub = self._hub
        if hub is not None:
            mem = hub.last_convo_ts(device_id)
            if mem > 0:
                return time.time() - mem
        return time.time() - await self._db_last_convo_ts(device_id)

    async def _db_last_convo_ts(self, device_id: str) -> float:
        now = time.time()
        cached = self._quest_db_ts.get(device_id)
        if cached is not None and now - cached[1] < _QUEST_DB_TS_CACHE_SEC:
            return cached[0]
        try:
            from deskbot_server.dao.device_session_mapper import get_current_session
            from deskbot_server.utils.async_helpers import run_blocking

            sess = await run_blocking(get_current_session, device_id)
            ts = float((sess or {}).get("updated_at") or 0.0)
        except Exception:
            ts = 0.0
        self._quest_db_ts[device_id] = (ts, time.time())
        return ts

    def on_face_lost(self, device_id: str) -> None:
        st = self._devs.get(str(device_id or "").strip())
        if st is not None and st.mode == LiveState.GAZE:
            st.mode = LiveState.WANDER

    async def _send_gaze(self, device_id: str, analysis: dict[str, Any], st: _Dev) -> None:
        now = time.monotonic()
        if now - st.last_face_send < FACE_TICK_MIN_SEC:
            return
        from deskbot_server.service.application.interaction_feedback import _gaze_servo_step

        step = _gaze_servo_step(analysis, device_id=device_id)
        if step is None:
            return
        moves = [{"move": "__custom__", "ms": 500, "x": step["x"], "y": step["y"], "xm": 0, "ym": 0}]
        happy = (now - st.last_happy) >= 2.0
        n = await self._send(device_id, moves, "happy" if happy else None, "auto_live_face", "live 人脸注视")
        if n > 0:
            st.last_face_send = now
            if happy:
                st.last_happy = now

    async def _send(
        self, device_id: str, moves: list[dict[str, Any]], scene: str | None, source: str, summary: str
    ) -> int:
        """构造并发送 PbSeq（gaze 专用）。"""
        from deskbot_server.model.pb_seq import PbAction, PbBlock, PbSeq, PbServo, PbType

        steps = expand_llm_moves(moves, device_id=device_id)
        if not steps:
            return 0
        req_id = uuid.uuid4().hex[:16]
        total_ms = sum(max(1, int(s.get("ms") or 0)) for s in steps)
        servo = tuple(
            PbServo(xm=int(s["xm"]), ym=int(s["ym"]), x=int(s["x"]), y=int(s["y"]), ms=int(s["ms"]))
            for s in steps
        )
        servo_block = PbBlock(type=PbType.SINGLE, req=req_id, idx=0, chunk_ms=total_ms, servo=servo)
        tail = _scene_tail(scene, device_id=device_id, req=req_id, target_ms=total_ms) if scene else []
        hub = self.hub
        try:
            tail_entries = tuple(PbBlock.from_wire(f) for f in tail)
            entries = (servo_block,) + tail_entries
            pb_seq = PbSeq(req=req_id, entries=entries, level=0, action=PbAction.DEFAULT)
            n = await hub.send(device_id, pb_seq)
        except Exception:
            logger.exception("[live] send failed device_id=%s source=%s", device_id, source)
            return 0
        if n > 0:
            logger.debug(
                "[live] %s device_id=%s req=%s delivered=%d blocks=%d servo_steps=%d ms=%d summary=%s scene=%s",
                source,
                device_id,
                req_id,
                n,
                len(entries),
                len(steps),
                total_ms,
                summary,
                scene,
            )
        return n
