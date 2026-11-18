"""Regression coverage for live Agent tools, without network or hardware."""

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from deskbot_server.model.system_event import SystemEvent, current_time_context
from deskbot_server.service import scheduled_task_service as schedules
from deskbot_server.service import system_event_service as events
from deskbot_server.service.agent_tools import AgentTools
from deskbot_server.service.application import face_snapshot_cache as faces
from deskbot_server.service.application import voice_snapshot_cache as voices
from deskbot_server.service.application.device_media import DeviceMedia
from tests.test_flecket import client


@pytest.fixture
def perception(monkeypatch):
    face = SimpleNamespace(is_configured=lambda: True, runtime=SimpleNamespace(mode="insightface"),
                           register_face_embedding=Mock(return_value={"id": 17, "name": "小明", "embedding": [123]}))
    voice = SimpleNamespace(enabled=lambda: True, runtime=SimpleNamespace(sample_max_age_s=60),
                            register_voice_embedding=Mock(return_value={"id": 18, "name": "小明", "embedding": [456]}))
    monkeypatch.setattr(events, "CameraFaceService", lambda: face)
    monkeypatch.setattr(events, "VoiceprintService", lambda: voice)
    monkeypatch.setattr("deskbot_server.service.camera_face_service.CameraFaceService", lambda: face)
    monkeypatch.setattr("deskbot_server.service.voiceprint_service.VoiceprintService", lambda: voice)
    yield face, voice
    for device in ("robot", "other"):
        faces.clear_device(device)
        voices.clear_device(device)


def agent(device_id="robot"):
    bridge = SimpleNamespace(_current=lambda generation: generation == 0, client=SimpleNamespace(_turn_id="turn-a"),
                             hub=SimpleNamespace(_devices={}))
    return AgentTools(device_id, bridge)


async def execute(value, name, args=None):
    return await value.execute(name, args or {}, call_id="c", response_id="r", generation=0)


def test_live_tool_catalog_removes_clock_and_registers_perception():
    names = {tool["name"] for tool in agent().schemas()}
    assert "get_current_time" not in names
    assert {"register_face", "register_voiceprint", "recognize_face", "recognize_voiceprint"} <= names
    assert "rom_volume" not in names and "show_image" not in names


def test_dynamic_clock_includes_date_weekday_and_timezone():
    first = current_time_context(now=datetime(2026, 11, 18, 15, 59, tzinfo=UTC))
    second = current_time_context(now=datetime(2026, 11, 18, 16, 1, tzinfo=UTC))
    assert "2026-11-18T23:59:00+08:00 星期三" in first
    assert "2026-11-19T00:01:00+08:00 星期四" in second


def test_clock_refreshes_on_event_and_tool_continuation(monkeypatch):
    clock = Mock(side_effect=["clock-first", "clock-continuation"])
    monkeypatch.setattr("deskbot_server.client.flecket_live.current_time_context", clock)

    async def run():
        value = client()
        value._session_instructions = "原有人设与规则"
        event = SystemEvent.create("perception_snapshot", "event", "perception", {})
        assert "clock-first" in value._event_instructions(event)
        value._remember_event(event, 0)
        value.agent_tool_handler = AsyncMock(return_value={"ok": True})
        await value._complete_tools({"id": "r", "status": "completed", "metadata": {"system_event_id": "event"},
                                     "output": [{"type": "function_call", "call_id": "c", "name": "media_list", "arguments": "{}"}]})
        instructions = value.ws.sent[-1]["response"]["instructions"]
        assert "clock-continuation" in instructions and "clock-first" not in instructions
        assert instructions.startswith("原有人设与规则")
        assert value._session_instructions == "原有人设与规则"
        await value.close()
    asyncio.run(run())


def test_quiet_lookup_continues_without_audio_and_following_action_stays_quiet():
    async def run():
        value = client()
        value.agent_tools = [{"name": "media_list"}, {"name": "play_music"}]
        value.agent_tool_handler = AsyncMock(side_effect=[
            {"ok": True, "assets": [{"id": "song"}], "speak": False},
            {"ok": True, "status": "started", "speak": False},
        ])
        await value._complete_tools({"id": "lookup", "status": "completed", "metadata": {"input_generation": 0}, "output": [
            {"type": "function_call", "call_id": "lookup", "name": "media_list", "arguments": '{"speak":false}'},
        ]})
        continuation = value.ws.sent[-1]["response"]
        assert continuation["output_modalities"] == ["text"]
        assert continuation["metadata"]["deskbot_quiet_tools"] is True
        await value._complete_tools({"id": "play", "status": "completed", "metadata": continuation["metadata"], "output": [
            {"type": "function_call", "call_id": "play", "name": "play_music", "arguments": '{"asset_id":"song"}'},
        ]})
        assert value.agent_tool_handler.call_args.args == ("play_music", {"asset_id": "song", "speak": False})
        assert sum(e["type"] == "response.create" for e in value.ws.sent) == 1
        await value.close()
    asyncio.run(run())


def test_perception_tools_keep_face_and_speaker_separate_and_hide_vectors(perception):
    faces.update_device_faces("robot", [{"face_id": 2, "person_name": "小红", "identity_score": .9, "embedding": [123]}])
    faces.update_device_faces("other", [{"face_id": 9, "person_name": "其他设备"}])
    seq = voices.begin_identification("robot", "turn-a")
    voices.finish_identification("robot", seq, state="found", name="小明", score=.91)

    async def run():
        value = agent()
        face = await execute(value, "recognize_face")
        voice = await execute(value, "recognize_voiceprint", {"speak": False})
        assert face["camera"]["people"][0]["name"] == "小红"
        assert voice["speaker"]["name"] == "小明" and voice["speak"] is False
        assert "embedding" not in json.dumps([face, voice])
        assert "其他设备" not in json.dumps([face, voice], ensure_ascii=False)
    asyncio.run(run())


def test_face_enrollment_requires_selection_and_freshness(perception, monkeypatch):
    face_service, _ = perception
    faces.update_device_faces("robot", [{"face_id": 1, "embedding": [1.] * 512}, {"face_id": 2, "embedding": [2.] * 512}])

    async def run():
        value = agent()
        ambiguous = await execute(value, "register_face", {"name": "小明"})
        assert not ambiguous["ok"] and "face_id" in ambiguous["error"]
        face_service.register_face_embedding.assert_not_called()
        selected = await execute(value, "register_face", {"name": "小明", "face_id": 2})
        assert selected["status"] == "registered" and selected["face_id"] == 2
        assert "embedding" not in selected
        assert face_service.register_face_embedding.call_args.args[1] == [2.] * 512
        monkeypatch.setattr(faces.time, "time", lambda: time_now + 4)
        assert faces.list_device_faces("robot", max_age_s=3) == {}
        from deskbot_server.service.application.face_registration import register_face_for_device
        with pytest.raises(ValueError, match="无人脸"):
            register_face_for_device("robot", "小明", face_id=2, max_age_s=3)
    time_now = time.time()
    asyncio.run(run())


def test_voice_enrollment_never_uses_previous_turn_sample(perception):
    _, voice_service = perception
    seq = voices.begin_identification("robot", "turn-a")
    voices.finish_identification("robot", seq, state="unknown")
    voices.store_voice_sample("robot", "old-turn", [1.] * 256)

    async def run():
        value = agent()
        rejected = await execute(value, "register_voiceprint", {"name": "小明"})
        assert not rejected["ok"]
        voice_service.register_voice_embedding.assert_not_called()
        voices.store_voice_sample("robot", "turn-a", [2.] * 256)
        saved = await execute(value, "register_voiceprint", {"name": "小明"})
        assert saved["status"] == "registered" and saved["profile_id"] == 18
        assert "embedding" not in saved
        assert voice_service.register_voice_embedding.call_args.args[1] == [2.] * 256
    asyncio.run(run())


def test_enrollment_reports_saved_biometrics_when_memory_link_fails(perception, monkeypatch):
    _, voice_service = perception
    seq = voices.begin_identification("robot", "turn-a")
    voices.finish_identification("robot", seq, state="unknown")
    voices.store_voice_sample("robot", "turn-a", [2.] * 256)
    monkeypatch.setattr("deskbot_server.service.memory_service.ensure_person", Mock(side_effect=OSError("disk unavailable")))

    async def run():
        result = await execute(agent(), "register_voiceprint", {"name": "小明"})
        assert result["ok"] and result["status"] == "registered"
        assert result["memory_status"] == "pending" and result["person_id"] is None
        voice_service.register_voice_embedding.assert_called_once()
    asyncio.run(run())


def test_perception_wait_does_not_enroll_after_interruption(perception, monkeypatch):
    face_service, _ = perception
    faces.update_device_faces("robot", [{"face_id": 1, "embedding": [1.] * 512}])

    async def run():
        value = agent()
        current = iter([True, False])
        value.bridge._current = lambda generation: next(current)
        result = await execute(value, "register_face", {"name": "小明"})
        assert result["status"] == "cancelled"
        face_service.register_face_embedding.assert_not_called()
    asyncio.run(run())


def test_deferred_media_start_failure_is_visible_in_status(monkeypatch):
    from deskbot_server.service.moss_live_service import FlecketBridge

    async def run():
        bridge = object.__new__(FlecketBridge)
        bridge._current = lambda generation: True
        bridge.media = SimpleNamespace(show_image=AsyncMock(return_value={"ok": False, "error": "绘制确认超时"}), status={})
        await bridge._start_planned_media({"kind": "image", "asset_id": "image", "duration_ms": 5000,
                                          "generation": 0, "expires": time.monotonic() + 20})
        assert bridge.media.status == {"state": "failed", "error": "绘制确认超时"}
    asyncio.run(run())


def test_disabled_perception_reports_failure(perception):
    face, voice = perception
    face.runtime.mode = "none"
    voice.enabled = lambda: False

    async def run():
        assert not (await execute(agent(), "recognize_face"))["ok"]
        assert not (await execute(agent(), "register_voiceprint", {"name": "小明"}))["ok"]
    asyncio.run(run())


def test_stop_media_does_not_report_success_when_image_stop_times_out():
    async def run():
        bridge = SimpleNamespace(device_id="robot", generation=1, hub=SimpleNamespace(
            _devices={"robot": SimpleNamespace(capabilities={"rom_tools_v1"})},
            rom_command=AsyncMock(return_value={"ok": False, "error": "设备确认超时"})))
        result = await DeviceMedia(bridge).stop()
        assert not result["ok"] and result["status"] == "unconfirmed"
        assert result["music_stopped"] is True and result["image_stopped"] is False
    asyncio.run(run())


def test_reminder_relative_update_changes_persisted_time_and_rearms_completed_task(monkeypatch):
    now = datetime(2026, 11, 18, 12, 30, 47, tzinfo=schedules.CST)
    monkeypatch.setattr(schedules, "cst_now", lambda: now)
    task = schedules.create_scheduled_task("robot", "喝水", delay_seconds=30)
    schedules.finish_scheduled_task(task["id"], ok=True)
    result = schedules.execute_schedule_task_tool({"action": "update", "id": task["id"], "delay_minutes": 10}, device_id="robot")
    stored = schedules.get_scheduled_task(task["id"], device_id="robot")
    assert result["ok"] and stored["next_run_at"] == "2026-11-18 12:40:47"
    assert stored["status"] == "active" and stored["enabled"] and stored["task_kind"] == "once"
    assert not stored["executed_at"] and not stored["result_summary"]


@pytest.mark.parametrize("timing", [{"delay_minutes": 1, "delay_seconds": 20}, {"cron": "0 9 * * *", "delay_minutes": 1},
                                   {"delay_seconds": 0}, {"delay_seconds": -1}, {"delay_seconds": True}, {"delay_seconds": "bad"}])
def test_invalid_reminder_update_does_not_change_database(timing):
    task = schedules.create_scheduled_task("robot", "喝水", delay_seconds=30)
    with pytest.raises(ValueError):
        schedules.execute_schedule_task_tool({"action": "update", "id": task["id"], **timing}, device_id="robot")
    assert schedules.get_scheduled_task(task["id"], device_id="robot")["next_run_at"] == task["next_run_at"]


def test_reminder_list_can_reach_all_records_and_stays_device_scoped():
    for seconds in (10, 20, 30):
        schedules.create_scheduled_task("robot", str(seconds), delay_seconds=seconds)
    schedules.create_scheduled_task("other", "private", delay_seconds=10)
    first = schedules.execute_schedule_task_tool({"action": "list", "limit": 2}, device_id="robot")
    second = schedules.execute_schedule_task_tool({"action": "list", "limit": 2, "offset": 2}, device_id="robot")
    assert first["count"] == 2 and first["total"] == 3 and first["has_more"]
    assert second["count"] == 1 and not second["has_more"]
    assert len({t["id"] for t in first["tasks"] + second["tasks"]}) == 3


def test_running_reminder_cannot_be_rescheduled():
    task = schedules.create_scheduled_task("robot", "喝水", delay_seconds=30)
    from deskbot_server.dao import scheduled_task_mapper
    assert scheduled_task_mapper.claim(task["id"]) == 1
    with pytest.raises(ValueError, match="正在播报"):
        schedules.update_scheduled_task(task["id"], device_id="robot", delay_minutes=10)
