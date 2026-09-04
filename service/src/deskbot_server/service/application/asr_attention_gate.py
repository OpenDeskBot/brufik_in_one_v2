"""ASR 通过后的「对话注意力」门控（闲聊过滤）。

判定：相机最近画面无人 **且** 本句声纹判定不是认识的人 → 视为周围人在闲聊，
跳过后续 LLM/TTS 流程（不理会）。相机里有人 或 声纹是认识的人 → 正常应答。

- 人脸侧数据源 ``face_snapshot_cache``：最近一次检测帧的判定 + 完成时刻
  （``face_snapshot_ts``）。仅当该时刻落在 ``FACE_PRESENCE_WINDOW_S`` 窗口内且
  帧里有人脸才认为「相机画面里有人」——旧帧/停帧超过窗口即按无人处理。
- 声纹侧只在**本轮起了 identify 任务**（``vpr_engine_on=True``）时采信快照：
  引擎关闭时快照可能残留上一句的 found 名，不可用于本句（传 None 视为无结论）。
- 引擎无结论（关闭 / identifying 竞态 / degraded / 无快照）→ **放行**：
  避免引擎故障或未配置期间机器人对画面外的真实说话人「变哑」。

本门控只挂在语音轮（``device_ws_service._run_asr_turn``）；主动问答
（quest/social/scheduled）与 Web 文本轮不经此路径、不受影响。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from deskbot_server.service.application import face_snapshot_cache
from deskbot_server.service.application.voice_snapshot_cache import (
    STATE_DEGRADED,
    STATE_FOUND,
    STATE_IDENTIFYING,
    STATE_UNKNOWN,
)

logger = logging.getLogger("deskbot-server")

# 该秒数内有检测到人脸的帧，才判定「相机画面里有人」（真源；帧跟踪持续时
# 画面内的帧间隔远小于此值，故误过滤时调大可排查）
FACE_PRESENCE_WINDOW_S = 5.0

# 人脸检测在途时的轮询上限/步进（camera_frame 到达即 spawn process，外部
# /detect 需数百 ms；等一次避免首帧未落地就把本句误判为「画面无人」）
_FACE_SETTLE_MAX_WAIT_S = 1.0
_FACE_SETTLE_POLL_S = 0.05

# 结论 reason（engage 表示进入 LLM/TTS 流程）
ENGAGE_FACE_SEEN = "face_seen"
ENGAGE_VOICE_KNOWN = "voice_known"
ENGAGE_VOICE_INCONCLUSIVE = "voice_inconclusive"
IGNORE_NO_FACE_AND_UNKNOWN = "no_face_and_unknown_speaker"


@dataclass(frozen=True)
class AttentionVerdict:
    """门控结论。``engage=False`` 表示疑似闲聊、跳过本轮 LLM/TTS。"""

    engage: bool
    reason: str
    note: str


def judge_speaker_round(
    *,
    device_id: str | None,
    vpr_engine_on: bool,
    voice_snapshot: dict[str, Any] | None = None,
    face_window_s: float = FACE_PRESENCE_WINDOW_S,
    now: float | None = None,
) -> AttentionVerdict:
    """按当前人脸/声纹快照判定本句是否值得进入 LLM 轮（纯函数，只读缓存）。

    ``now`` 供测试注入（判定快照新鲜度的参考时刻）；缺省 ``time.time()``。
    """
    dev = str(device_id or "").strip()
    now = time.time() if now is None else float(now)

    faces = face_snapshot_cache.list_device_faces(dev)
    face_ts = face_snapshot_cache.face_snapshot_ts(dev)
    face_age_s: float | None = None
    if face_ts is not None:
        face_age_s = max(0.0, now - face_ts)
    face_count = len(faces)
    face_present = face_count > 0 and face_ts is not None and (now - face_ts) <= face_window_s

    voice_state: str | None = None
    if vpr_engine_on and isinstance(voice_snapshot, dict):
        voice_state = str(voice_snapshot.get("state") or "").strip() or None

    face_note = f"faces={face_count} face_age_s={'-' if face_age_s is None else round(face_age_s, 1)}"
    voice_note = f"voice_state={voice_state if vpr_engine_on else 'engine_off'}"

    if face_present:
        # 相机画面里有人 → 规则 2：走后续流程（无论声纹结论）
        return AttentionVerdict(True, ENGAGE_FACE_SEEN, f"{face_note} {voice_note}")
    if voice_state == STATE_FOUND:
        # 画面无人但声纹认出认识的人 → 规则 2
        return AttentionVerdict(True, ENGAGE_VOICE_KNOWN, f"{face_note} {voice_note}")
    if voice_state == STATE_UNKNOWN:
        # 画面无人 + 声音明确不是认识的人 → 疑似周围人闲聊，不理会（规则 1）
        return AttentionVerdict(False, IGNORE_NO_FACE_AND_UNKNOWN, f"{face_note} {voice_note}")
    # 引擎关闭 / identifying（结论未出）/ degraded（引擎不可用）/ 无快照：
    # 没有「不认识」的证据 → 放行，避免机器人无谓变哑
    return AttentionVerdict(True, ENGAGE_VOICE_INCONCLUSIVE, f"{face_note} {voice_note}")


async def settle_face_detection(
    device_ws: Any,
    device_id: str | None,
    *,
    face_window_s: float = FACE_PRESENCE_WINDOW_S,
) -> None:
    """若最近窗口内有上行帧但快照还没落到窗口内（检测在途），短轮询等它落地。

    零等待路径：没有新鲜帧 / 快照已新鲜。等不到（busy 丢帧、外部服务慢）按
    现有快照继续，最多等 ``_FACE_SETTLE_MAX_WAIT_S``。
    """
    if device_ws is None or not device_id:
        return
    try:
        has_fresh_frame = bool(device_ws.latest_camera_frame(device_id, max_age_s=face_window_s))
    except Exception:
        return
    if not has_fresh_frame:
        return
    deadline = time.monotonic() + _FACE_SETTLE_MAX_WAIT_S
    while True:
        try:
            ts = face_snapshot_cache.face_snapshot_ts(device_id)
        except Exception:
            return
        if ts is not None and (time.time() - ts) <= face_window_s:
            return  # 本帧判定已落地
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(_FACE_SETTLE_POLL_S)


async def decide_round(
    device_ws: Any,
    device_id: str | None,
    *,
    vpr_engine_on: bool,
    voice_snapshot: dict[str, Any] | None = None,
    face_window_s: float = FACE_PRESENCE_WINDOW_S,
    now: float | None = None,
) -> AttentionVerdict:
    """settle + judge 组合：语音轮在 vpr 结论落地后调用一次。"""
    await settle_face_detection(device_ws, device_id, face_window_s=face_window_s)
    return judge_speaker_round(
        device_id=device_id,
        vpr_engine_on=vpr_engine_on,
        voice_snapshot=voice_snapshot,
        face_window_s=face_window_s,
        now=now,
    )
