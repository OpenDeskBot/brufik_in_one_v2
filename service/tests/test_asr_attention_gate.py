"""ASR 通过后的闲聊过滤门控：judge 矩阵单测 + ``_run_asr_turn`` 集成。

判定规则（用户拍板）：
- 相机画面里有人（5s 内检测到人脸）→ 放行；
- 声纹认出认识的人（found）→ 放行；
- 画面无人 + 声纹明确不认识（unknown）→ 忽略（疑似闲聊，不进 LLM/TTS）；
- 声纹无结论（引擎关/identifying/degraded/无快照）→ 放行（防机器人变哑）。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from deskbot_server.service.application import asr_attention_gate as gate
from deskbot_server.service.application import face_snapshot_cache as fsc
from deskbot_server.service.application.voice_snapshot_cache import (
    STATE_DEGRADED,
    STATE_FOUND,
    STATE_IDENTIFYING,
    STATE_UNKNOWN,
    begin_identification,
    finish_identification,
)
from deskbot_server.service.device_ws_service import DeviceWsService

_FACE = {"face_id": 1, "landmarks": [], "person_name": "小明", "identity_score": 0.9}


def _set_voice(device_id: str, state: str, *, name: str | None = None) -> None:
    seq = begin_identification(device_id)
    finish_identification(device_id, seq, state=state, name=name)


def _judge(device_id: str, *, engine_on: bool = True, snapshot=None, now: float | None = None) -> gate.AttentionVerdict:
    """engine_on=True 时 snapshot 缺省按缓存现读（真实流程同源）。"""
    if snapshot is None and engine_on:
        from deskbot_server.service.application.voice_snapshot_cache import get_voice_snapshot

        snapshot = get_voice_snapshot(device_id)
    return gate.judge_speaker_round(
        device_id=device_id,
        vpr_engine_on=engine_on,
        voice_snapshot=snapshot,
        now=now,
    )


# ─────────────────────── judge 矩阵 ───────────────────────


def test_face_present_engages_regardless_of_voice():
    fsc.update_device_faces("dev-j-1", [_FACE])          # 有人脸帧
    for state in (STATE_FOUND, STATE_UNKNOWN, STATE_DEGRADED, None):
        v = _judge("dev-j-1", engine_on=state is not None, snapshot={"state": state} if state else None)
        assert v.engage and v.reason == gate.ENGAGE_FACE_SEEN
    assert "faces=1" in v.note


def test_no_face_with_unknown_voice_is_ignored():
    fsc.update_device_faces("dev-j-2", [])               # 最近帧确认无人
    _set_voice("dev-j-2", STATE_UNKNOWN)
    v = _judge("dev-j-2")
    assert not v.engage
    assert v.reason == gate.IGNORE_NO_FACE_AND_UNKNOWN
    assert "voice_state=unknown" in v.note


def test_no_face_with_known_voice_engages():
    fsc.update_device_faces("dev-j-3", [])
    _set_voice("dev-j-3", STATE_FOUND, name="小明")
    v = _judge("dev-j-3")
    assert v.engage and v.reason == gate.ENGAGE_VOICE_KNOWN


def test_no_face_inconclusive_voice_engages():
    fsc.update_device_faces("dev-j-4", [])
    for state in (STATE_IDENTIFYING, STATE_DEGRADED):
        _set_voice("dev-j-4", state)
        v = _judge("dev-j-4")
        assert v.engage and v.reason == gate.ENGAGE_VOICE_INCONCLUSIVE, state
    # 引擎开着但本句没有快照（budget 超时尚未落盘）
    v = _judge("dev-j-4", snapshot=None)
    assert v.engage and v.reason == gate.ENGAGE_VOICE_INCONCLUSIVE


def test_engine_off_never_trusts_stale_found_snapshot():
    """引擎关闭时即便快照残留上一句 found 名，也不得当作本句认识（传 None）。"""
    fsc.update_device_faces("dev-j-5", [])
    _set_voice("dev-j-5", STATE_FOUND, name="小明")       # 残留上一句的判定
    v = _judge("dev-j-5", engine_on=False, snapshot=None)
    assert v.engage and v.reason == gate.ENGAGE_VOICE_INCONCLUSIVE
    assert "engine_off" in v.note


def test_engine_off_face_present_engages():
    fsc.update_device_faces("dev-j-6", [_FACE])
    v = _judge("dev-j-6", engine_on=False, snapshot=None)
    assert v.engage and v.reason == gate.ENGAGE_FACE_SEEN


def test_stale_face_snapshot_counts_as_no_face():
    """人脸快照超过 5s 窗口 → 画面无人（旧帧不算有人在场）。"""
    fsc.update_device_faces("dev-j-7", [_FACE])
    _set_voice("dev-j-7", STATE_UNKNOWN)
    v = _judge("dev-j-7", now=time.time() + 10.0)
    assert not v.engage
    assert "face_age_s=9." in v.note or "face_age_s=10." in v.note


def test_never_detected_face_counts_as_no_face():
    """相机从未检测过任何帧（无快照无 ts）→ 同样按画面无人处理。"""
    _set_voice("dev-j-8", STATE_UNKNOWN)
    v = _judge("dev-j-8")
    assert not v.engage and v.reason == gate.IGNORE_NO_FACE_AND_UNKNOWN
    assert "face_age_s=-" in v.note


def test_face_snapshot_ts_api():
    assert fsc.face_snapshot_ts("dev-j-ts") is None
    fsc.update_device_faces("dev-j-ts", [_FACE])
    ts1 = fsc.face_snapshot_ts("dev-j-ts")
    assert ts1 is not None and abs(ts1 - time.time()) < 1.0
    time.sleep(0.01)
    fsc.update_device_faces("dev-j-ts", [])               # 无人帧也打点
    ts2 = fsc.face_snapshot_ts("dev-j-ts")
    assert ts2 is not None and ts2 > ts1


# ─────────────────────── settle（检测在途短等） ───────────────────────


def test_settle_waits_for_inflight_face_detection():
    """最近窗口内有上行帧但快照未落窗（检测在途）→ 等检测落地后返回。"""
    dev = "dev-settle-1"

    async def _scenario() -> float:
        svc = _make_service()
        svc._camera_frame_cache[dev] = (time.monotonic(), b"fake-jpeg")
        t0 = time.monotonic()

        async def _later():
            await asyncio.sleep(0.1)
            fsc.update_device_faces(dev, [_FACE])   # 模拟 process 异步落地

        waiter = asyncio.create_task(_later())
        await gate.settle_face_detection(svc, dev)
        elapsed = time.monotonic() - t0
        await waiter
        return elapsed

    elapsed = asyncio.run(_scenario())
    assert elapsed < 0.8   # 等到了，无需耗尽 1s 上限


def test_settle_noop_without_fresh_frame():
    """无新鲜上行帧（人脸正常路径已处理完 / 无帧）→ 立即返回零等待。"""
    dev = "dev-settle-2"

    async def _scenario() -> float:
        svc = _make_service()
        t0 = time.monotonic()
        await gate.settle_face_detection(svc, dev)
        return time.monotonic() - t0

    assert asyncio.run(_scenario()) < 0.1


# ─────────────────────── _run_asr_turn 集成 ───────────────────────


def _make_service() -> DeviceWsService:
    DeviceWsService.reset_instance()
    svc = DeviceWsService()
    svc._bus_service = None
    return svc


def _patch_asr_ok():
    return [
        patch("deskbot_server.service.asr_service.AsrService.transcribe", new=AsyncMock(return_value="闲聊测试")),
        patch("deskbot_server.service.asr_service.AsrService.is_valid_text", new=AsyncMock(return_value=True)),
    ]


def _run_turn(dev: str, *, engine_on: bool) -> MagicMock:
    """执行一轮 _run_asr_turn，返回 run_ws_chat_turn mock（用于断言调用与否）。"""
    svc = _make_service()
    pipeline = MagicMock()
    ws = MagicMock()
    with patch("deskbot_server.service.voiceprint_service.VoiceprintService.enabled", return_value=engine_on), patch(
        "deskbot_server.service.voiceprint_service.VoiceprintService.identify", new=AsyncMock()
    ), patch(
        "deskbot_server.service.application.ws_chat_turn.run_ws_chat_turn", new=AsyncMock(return_value={"status": "ok"})
    ) as run_mock:
        with _patch_asr_ok()[0], _patch_asr_ok()[1]:
            asyncio.run(svc._run_asr_turn(ws, pipeline, b"\x00\x00" * 1600, device_id=dev))
    return run_mock


def test_turn_ignored_when_no_face_and_unknown_voice():
    dev = "dev-int-1"
    fsc.update_device_faces(dev, [])
    _set_voice(dev, STATE_UNKNOWN)
    run_mock = _run_turn(dev, engine_on=True)
    run_mock.assert_not_awaited()


def test_turn_proceeds_when_face_seen():
    dev = "dev-int-2"
    fsc.update_device_faces(dev, [_FACE])
    _set_voice(dev, STATE_UNKNOWN)
    run_mock = _run_turn(dev, engine_on=True)
    run_mock.assert_awaited_once()


def test_turn_proceeds_when_voice_known():
    dev = "dev-int-3"
    fsc.update_device_faces(dev, [])
    _set_voice(dev, STATE_FOUND, name="小明")
    run_mock = _run_turn(dev, engine_on=True)
    run_mock.assert_awaited_once()


def test_turn_proceeds_when_engine_off_no_face():
    """引擎未启用（无本句声纹结论）→ 放行，门控不得让机器人变哑。"""
    dev = "dev-int-4"
    fsc.update_device_faces(dev, [])
    run_mock = _run_turn(dev, engine_on=False)
    run_mock.assert_awaited_once()
