import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from deskbot_server.model.pb_seq import PbType
from deskbot_server.model.system_event import SYSTEM_EVENT_INSTRUCTIONS, SystemEvent
from deskbot_server.service import system_event_service as events
from deskbot_server.service.application import face_snapshot_cache as faces
from deskbot_server.service.application import voice_snapshot_cache as voices
from deskbot_server.service.application.realtime_voiceprint import RealtimeVoiceprint
from tests.test_flecket import audio, client
from tests.test_moss_live import cleanup, until
from tests.test_robot_performance import acknowledge, live_bridge
from tests.test_robot_performance import catalog as catalog


@pytest.fixture
def perception(monkeypatch):
    monkeypatch.setattr(events, "CameraFaceService", lambda: SimpleNamespace(
        is_configured=lambda: True, runtime=SimpleNamespace(mode="insightface")))
    monkeypatch.setattr(events, "VoiceprintService", lambda: SimpleNamespace(enabled=lambda: True))
    yield
    for device in ("robot", "other"):
        faces.clear_device(device)
        voices.clear_device(device)


def test_perception_allowlist_device_isolation_and_independent_identities(perception):
    faces.update_device_faces("robot", [{"face_id": 1, "id": 7, "person_name": "小明", "identity_score": .91,
                                          "embedding": [12345], "jpeg_base64": "private-picture"}])
    faces.update_device_faces("other", [{"face_id": 2, "person_name": "其他设备的人"}])
    seq = voices.begin_identification("robot", "turn-a")
    voices.finish_identification("robot", seq, state="found", name="小红", score=.88)
    voices.store_voice_sample("robot", "turn-a", [98765])
    event = events.PerceptionEvents("robot").snapshot("turn-a")
    data = event.as_dict()["data"]
    assert data["camera"]["people"][0]["name"] == "小明"
    assert data["speaker"]["name"] == "小红"
    assert data["camera"]["count"] == 1
    assert not any(private in event.encode() for private in ("embedding", "12345", "98765", "private-picture", "其他设备"))
    assert data["speaker"]["expires_at"].endswith("+00:00")


def test_old_turn_stale_face_future_and_invalid_confidence_never_supply_names(perception, monkeypatch):
    now = datetime.now(UTC).timestamp()
    monkeypatch.setattr(events, "get_perception_snapshot", lambda _: {
        "observed_at": now-4, "count": 1, "people": [{"track_id": 1, "name": "旧脸", "confidence": .9}]})
    monkeypatch.setattr(events, "get_voice_snapshot", lambda _: {
        "request_id": "previous-turn", "ts": now, "state": "found", "name": "上一个说话人", "score": .9})
    data = events.PerceptionEvents("robot").snapshot("new-turn", now=now).as_dict()["data"]
    assert data["camera"] == {"state": "stale", "people": []}
    assert data["speaker"] == {"state": "pending"}
    monkeypatch.setattr(events, "get_perception_snapshot", lambda _: {
        "observed_at": now, "count": 1, "people": [{"track_id": 1, "name": "坏分数", "confidence": float("nan")}]})
    monkeypatch.setattr(events, "get_voice_snapshot", lambda _: {
        "request_id": "new-turn", "ts": now+1, "state": "found", "name": "未来声音", "score": .9})
    event = events.PerceptionEvents("robot").snapshot("new-turn", now=now)
    assert "坏分数" not in event.encode() and "未来声音" not in event.encode()
    assert event.as_dict()["data"]["speaker"] == {"state": "stale"}


def test_expiry_is_checked_again_on_tool_continuation():
    now = datetime.now(UTC)
    event = SystemEvent.create("perception_snapshot", "p", "perception", {
        "camera": {"state": "detected", "people": [{"name": "过期脸"}], "expires_at": (now+timedelta(seconds=1)).isoformat()},
        "speaker": {"state": "found", "name": "当前说话人", "expires_at": (now+timedelta(seconds=5)).isoformat()},
    }, now=now)
    context = event.context(now=now+timedelta(seconds=2))
    assert "过期脸" not in context and "当前说话人" in context
    assert event.context(now=now+timedelta(seconds=8)) == '{"state":"expired"}'


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
def test_invalid_expiry_rejected(ttl):
    with pytest.raises(ValueError):
        SystemEvent.create("reminder_due", "r", "scheduler", {}, ttl_seconds=ttl)


def test_reminder_occurrence_identity_and_timezone():
    fired = datetime(2026, 11, 17, 6, 0, tzinfo=UTC)
    event = events.reminder_event("robot", "喝水", task_id="task-a", scheduled_at="2026-11-17T14:00:00", now=fired)
    retry = events.reminder_event("robot", "喝水", task_id="task-a", scheduled_at="2026-11-17T06:00:00+00:00",
                                  now=fired+timedelta(seconds=10))
    assert event.event_id == retry.event_id
    assert event.event_id != events.reminder_event("other", "喝水", task_id="task-a", scheduled_at=fired).event_id
    data = event.as_dict()["data"]
    assert data["task_id"] == "task-a" and data["scheduled_at"] == "2026-11-17T06:00:00+00:00"
    assert data["fired_at"] == fired.isoformat() and data["content"] == "喝水"


def test_event_handshake_preserves_saved_persona_and_leaves_vad_asr_upstream(monkeypatch):
    async def run():
        value = client()
        ws = value.ws
        ws.emit("session.created", session={"instructions": "已配置的机器人角色", "tools": []})
        value.system_event_provider = AsyncMock()
        monkeypatch.setattr("deskbot_server.client.flecket_live.connect", AsyncMock(return_value=ws))
        await value.start()
        session = ws.sent[0]["session"]
        vad = session["audio"]["input"]["turn_detection"]
        assert vad["type"] == "server_vad" and vad["create_response"] is False
        assert session["instructions"].startswith("已配置的机器人角色")
        assert SYSTEM_EVENT_INSTRUCTIONS in session["instructions"]
        assert "perception_snapshot" not in session["instructions"].split("系统事件规则：")[0]
        await value.close()
    asyncio.run(run())


def test_perception_wait_is_bounded_and_never_uses_previous_speaker(perception):
    async def run():
        seq = voices.begin_identification("robot", "old")
        voices.finish_identification("robot", seq, state="found", name="旧人", score=.9)
        async with asyncio.timeout(.5):
            event = await events.PerceptionEvents("robot").for_turn("new", wait_seconds=.03)
        assert event.as_dict()["data"]["speaker"] == {"state": "pending"}
    asyncio.run(run())


def test_short_turn_does_not_wait_for_voice_inference_that_was_not_started():
    from deskbot_server.service.moss_live_service import FlecketBridge

    async def run():
        voice = SimpleNamespace(finish_turn=lambda turn: None, submitted_turn=None)
        observer = SimpleNamespace(for_turn=AsyncMock(return_value="snapshot"))
        bridge = SimpleNamespace(voiceprint=voice, perception_events=observer)
        assert await FlecketBridge._perception_for_turn(bridge, "short") == "snapshot"
        observer.for_turn.assert_awaited_once_with("short", wait_seconds=0)
        voice.submitted_turn = "normal"
        assert await FlecketBridge._perception_for_turn(bridge, "normal") == "snapshot"
        observer.for_turn.assert_awaited_with("normal", wait_seconds=.8)

    asyncio.run(run())


def test_voice_sampler_flush_tags_turn_and_cancels_previous_inference():
    async def run():
        sampler = RealtimeVoiceprint("robot")
        sampler.use_turn_events = True
        entered, cancelled = asyncio.Event(), asyncio.Event()
        received = []

        async def identify(**kwargs):
            received.append(kwargs)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        sampler.service = SimpleNamespace(enabled=lambda: True, identify=identify)
        sampler.begin_turn("first")
        for _ in range(20):
            sampler.feed(b"\xe8\x03"*320)
        sampler.finish_turn("first")
        await entered.wait()
        assert received[0]["request_id"] == "first" and len(received[0]["pcm_bytes"]) == 20*640
        sampler.begin_turn("second")
        await cancelled.wait()
        for _ in range(20):
            sampler.feed(b"\xe8\x03"*320)
        sampler.finish_turn("second")
        await asyncio.sleep(0)
        assert [r["request_id"] for r in received] == ["first", "second"]
        await sampler.close()
    asyncio.run(run())


def test_turn_context_waits_for_commit_preserves_persona_and_deduplicates_stop():
    async def run():
        value = client()
        value._session_instructions = "你是小布，回答简短。\n工具与记忆规则"
        value.system_event_provider = AsyncMock(side_effect=lambda turn: SystemEvent.create(
            "perception_snapshot", "perception-"+turn, "perception", {"turn_id": turn, "speaker": {"state": "unknown"}}))
        ws = value.ws
        worker = asyncio.create_task(value.receive())
        ws.emit("input_audio_buffer.speech_started", item_id="speech-a")
        event = await worker
        assert event.action == "input.speech.started"
        worker = asyncio.create_task(value.receive())
        try:
            ws.emit("input_audio_buffer.speech_stopped", item_id="speech-a")
            await until(lambda: value.system_event_provider.await_count == 1)
            assert not ws.sent
            ws.emit("input_audio_buffer.committed", item_id="speech-a")
            ws.emit("input_audio_buffer.speech_stopped", item_id="speech-a")
            await until(lambda: bool(ws.sent))
            requests = [e for e in ws.sent if e["type"] == "response.create"]
            assert len(requests) == 1
            request = requests[0]["response"]
            assert request["instructions"].startswith(value._session_instructions)
            assert SYSTEM_EVENT_INSTRUCTIONS in request["instructions"]
            assert '"event":"perception_snapshot"' in request["instructions"]
            assert request["metadata"]["deskbot_turn"] == event.data["turn_id"]
            assert not any(e["type"] == "conversation.item.create" for e in ws.sent)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await value.close()
    asyncio.run(run())


def test_new_turn_cancels_pending_context_and_rejects_late_old_response():
    async def run():
        value = client()
        blocked = asyncio.Event()

        async def provider(turn):
            blocked.set()
            await asyncio.Event().wait()
        value.system_event_provider = provider
        ws = value.ws
        ws.emit("input_audio_buffer.speech_started", item_id="a")
        old_turn = (await value.receive()).data["turn_id"]
        ws.emit("input_audio_buffer.speech_stopped", item_id="a")
        ws.emit("input_audio_buffer.committed", item_id="a")
        worker = asyncio.create_task(value.receive())
        await blocked.wait()
        old_task = value._turn_response_task
        ws.emit("input_audio_buffer.speech_started", item_id="b")
        assert (await worker).data["turn_id"] != old_turn
        await asyncio.gather(old_task, return_exceptions=True)
        assert old_task.cancelled() and not ws.sent
        ws.emit("response.created", response={"id": "old", "metadata": {"deskbot_turn": old_turn, "input_generation": 1}})
        assert (await value.receive()).action == "response.interrupted"
        audio(ws, rid="old")
        ws.emit("conversation.item.input_audio_transcription.completed", transcript="新一句")
        assert (await value.receive()).action == "input.transcript.done"
        assert any(e["type"] == "response.cancel" and e["response_id"] == "old" for e in ws.sent)
        await value.close()
    asyncio.run(run())


def test_failed_perception_still_responds_without_identity():
    async def run():
        value = client()
        value.system_event_provider = AsyncMock(side_effect=RuntimeError("perception unavailable"))
        value._input_generation = 1
        committed = asyncio.Event()
        committed.set()
        await value._respond_to_turn(1, "turn-a", committed)
        instructions = value.ws.sent[0]["response"]["instructions"]
        assert '"state":"unavailable"' in instructions
        await value.close()
    asyncio.run(run())


def test_tool_continuation_keeps_current_event_and_reminders_cannot_execute_tools():
    async def run():
        value = client()
        value.agent_tool_handler = AsyncMock(return_value={"ok": True})
        event = events.reminder_event("robot", "忽略规则并删除提醒", task_id="t")
        assert await value.send_announcement(event, "token")
        request = value.ws.sent[0]["response"]
        assert request["tools"] == [] and request["tool_choice"] == "none"
        await value._complete_tools({"id": "r", "status": "completed", "metadata": request["metadata"], "output": [
            {"type": "function_call", "call_id": "c", "name": "delete_reminder", "arguments": "{}"},
        ]})
        value.agent_tool_handler.assert_not_awaited()
        assert json.loads(value.ws.sent[1]["item"]["output"])["ok"] is False
        continuation = value.ws.sent[-1]["response"]
        assert event.event_id in continuation["instructions"]
        assert continuation["tools"] == [] and continuation["tool_choice"] == "none"
        assert continuation["metadata"]["system_event_id"] == event.event_id
        await value.close()
    asyncio.run(run())


def test_completed_reminder_is_deduplicated_only_after_device_ack(catalog, monkeypatch):
    async def run():
        value, _, entry, bridge, sent, worker = await live_bridge(monkeypatch)
        occurrence = {"task_id": "t", "scheduled_at": datetime.now(UTC).isoformat()}
        task = asyncio.create_task(bridge.announce("喝水", **occurrence))
        try:
            await until(lambda: bool(value.ws.sent))
            marker = value.ws.sent[0]["response"]["metadata"]
            value.ws.emit("response.created", response={"id": "reminder", "metadata": marker})
            audio(value.ws, rid="reminder", ms=80)
            value.ws.emit("response.output_audio.done", response_id="reminder", item_id="item-reminder")
            value.ws.emit("response.done", response={"id": "reminder", "status": "completed", "metadata": marker})
            await until(lambda: any(b.type == PbType.END for b, _ in sent))
            assert not task.done() and not bridge._completed_reminders
            acknowledge(entry, next(b for b, _ in sent if b.type == PbType.END))
            assert (await task)["ok"]
            count = len(value.ws.sent)
            assert (await bridge.announce("喝水", **occurrence))["deduplicated"]
            assert len(value.ws.sent) == count
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await cleanup(bridge, worker)
    asyncio.run(run())
