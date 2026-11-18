from __future__ import annotations

import asyncio
import io
import json
import shutil
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from deskbot_server.dao import device_mapper, scheduled_task_mapper
from deskbot_server.dao import media_asset_store as media_store
from deskbot_server.service import media_library, scheduled_task_service
from deskbot_server.service.agent_tools import AgentTools
from deskbot_server.service.application.external_reminder_scheduler import ExternalReminderScheduler
from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry
from tests.test_controller_auth import _login
from tests.test_controller_auth import api_client as api_client


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (40, 20), (255, 0, 0)).save(out, format="PNG")
    return out.getvalue()


def audio_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 3200)
    return out.getvalue()


def fake_agent(did="robot"):
    bridge = SimpleNamespace(_current=lambda gen: gen == 0, rom_command=AsyncMock(return_value={"ok": True, "volume": 45}),
                             media_tool=AsyncMock(return_value={"ok": True}))
    return AgentTools(did, bridge)


async def execute(agent, name, args, call_id="test-call"):
    return await agent.execute(name, args, call_id=call_id, response_id="r", generation=0)


def test_agent_memory_is_persistent_idempotent_and_device_scoped():
    async def run():
        agent = fake_agent()
        first = await execute(agent, "memory", {"action": "add", "text": "我喜欢爵士乐"})
        second = await execute(agent, "memory", {"action": "add", "text": "我喜欢爵士乐"})
        assert first["memory"]["id"] == second["memory"]["id"]
        mid = first["memory"]["id"]
        assert "爵士乐" in agent.instructions()
        other = fake_agent("other")
        assert (await execute(other, "memory", {"action": "get", "id": mid}))["ok"] is False
        assert (await execute(other, "memory", {"action": "delete", "id": mid}))["ok"] is False
        assert (await execute(agent, "memory", {"action": "update", "id": mid, "text": "我喜欢钢琴"}))["ok"]
        assert (await execute(agent, "memory", {"action": "list"}))["memories"][0]["text"] == "我喜欢钢琴"
        assert (await execute(agent, "memory", {"action": "delete", "id": mid}))["ok"]
        assert not (await execute(agent, "memory", {"action": "list"}))["memories"]
    asyncio.run(run())


def test_relative_reminder_keeps_seconds_and_cannot_cross_devices(monkeypatch):
    now = datetime(2026, 11, 17, 16, 22, 47, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(scheduled_task_service, "cst_now", lambda: now)

    async def run():
        task = await execute(fake_agent(), "schedule_task", {"action": "create", "task": "喝水", "task_kind": "once", "delay_seconds": 20})
        expected = now + timedelta(seconds=20)
        actual = datetime.fromisoformat(task["next_run_at"]).replace(tzinfo=expected.tzinfo)
        assert actual == expected
        denied = await execute(fake_agent("other"), "schedule_task", {"action": "delete", "id": task["id"]})
        assert denied["ok"] is False
    asyncio.run(run())


def test_memory_write_refreshes_agent_context_and_quiet_server_tool():
    async def run():
        agent = fake_agent()
        refresh = AsyncMock()
        agent.bridge.client = SimpleNamespace(refresh_agent_instructions=refresh)
        result = await execute(agent, "memory", {"action": "add", "text": "我喜欢钢琴", "speak": False})
        assert result["ok"] and result["speak"] is False
        refresh.assert_awaited_once()
        assert "我喜欢钢琴" in refresh.call_args.args[0]
        clock = await execute(agent, "get_current_time", {"speak": False})
        assert clock["ok"] and clock["speak"] is False
    asyncio.run(run())


def test_scheduler_recovers_running_reminders_after_restart():
    task = scheduled_task_service.create_scheduled_task("robot", "恢复提醒", delay_seconds=1)
    assert scheduled_task_mapper.claim(task["id"]) == 1
    assert scheduled_task_mapper.recover_running() == 1
    assert scheduled_task_mapper.get(task["id"]).status == "active"
    assert scheduled_task_mapper.recover_running() == 0


def test_media_upload_requires_login_and_device_ownership(api_client):
    assert api_client.get("/api/device-media").status_code == 401
    _login(api_client, current_device_id="device-owner")
    response = api_client.post("/api/device-media", data={"kind": "image", "title": "红色图片"},
                               files={"file": ("red.png", image_bytes(), "image/png")})
    assert response.status_code == 200, response.text
    asset = response.json()["asset"]
    assert (asset["width"], asset["height"]) == (284, 240)
    raw = media_store.asset_path("device-owner", asset["id"], "rgb565").read_bytes()
    assert len(raw) == 284 * 240 * 2
    assert b"\x00\xf8" in raw  # RGB565 little-endian red.
    assert api_client.get(f'/api/device-media/{asset["id"]}/file').headers["content-type"] == "image/png"
    assert api_client.get(f'/api/device-media/{asset["id"]}/file?device_id=device-other').status_code == 403
    assert api_client.post("/api/device-media?device_id=device-other", data={"kind": "image"},
                           files={"file": ("red.png", image_bytes(), "image/png")}).status_code == 403
    assert api_client.delete(f'/api/device-media/{asset["id"]}').status_code == 200
    assert api_client.get(f'/api/device-media/{asset["id"]}/file').status_code == 404


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is unavailable")
def test_music_upload_normalizes_to_device_audio():
    item = media_library.upload("robot", "music", "测试音乐", audio_bytes())
    assert item["duration_ms"] == 200
    with wave.open(str(media_store.asset_path("robot", item["id"], "wav")), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 16000, 3200)
    with pytest.raises(ValueError):
        media_store.get("someone-else", item["id"])


def test_invalid_media_and_paths_are_rejected():
    with pytest.raises(ValueError):
        media_library.upload("robot", "image", "bad", b"not an image")
    with pytest.raises(ValueError):
        media_store.asset_path("robot", "../global/web_secret_key", "json")


def test_rom_volume_requires_real_matching_receipt_and_persists(api_client, monkeypatch):
    async def run():
        DeviceWsService.reset_instance()
        hub = DeviceWsService()
        hub._devices["device-owner"] = _DeviceEntry(device_id="device-owner", ws=object(), generation=5, capabilities={"rom_tools_v1"})
        sent = []

        async def send(entry, wire, **kwargs):
            assert entry is hub._devices["device-owner"]
            did = entry.device_id
            command = json.loads(wire)
            sent.append(command)
            hub.accept_rom_result(did, {"id": "wrong", "ok": True, "status": "completed", "volume": 99})
            hub.accept_rom_result(did, {"id": command["id"], "ok": True, "status": "completed", "volume": 45})
            return True

        monkeypatch.setattr(hub, "_enqueue_dl", send)
        stale = await hub.rom_command("device-owner", "volume", {"action": "adjust", "value": -10}, expected_generation=4)
        assert stale["status"] == "cancelled" and not sent
        result = await hub.rom_command("device-owner", "volume", {"action": "adjust", "value": -10})
        assert result["ok"] and result["volume"] == 45
        assert device_mapper.get_by_device_id("device-owner").volume == 45
        assert not hub._devices["device-owner"].rom_waiters
        assert sent[0]["arguments"]["value"] == -10
        monkeypatch.setattr(hub, "_enqueue_dl", AsyncMock(return_value=True))
        timeout = await hub.rom_command("device-owner", "volume", {"action": "get"}, timeout=.01)
        assert not timeout["ok"]
    asyncio.run(run())


@pytest.mark.parametrize("played", [False, True])
def test_scheduler_uses_external_agent_and_only_completes_after_playback(monkeypatch, played):
    now = scheduled_task_service.cst_now()
    task = scheduled_task_service.create_scheduled_task("robot", "该喝水了", delay_seconds=1)
    monkeypatch.setattr(scheduled_task_service, "cst_now", lambda: now+timedelta(seconds=2))
    announce = AsyncMock(return_value={"ok": played})
    hub = SimpleNamespace(_devices={"robot": SimpleNamespace(conversation_session=SimpleNamespace(announce=announce))})
    monkeypatch.setattr("deskbot_server.service.application.external_reminder_scheduler.get_auto_reply", lambda did: True)

    async def run():
        scheduler = ExternalReminderScheduler(hub)
        await scheduler.tick()
        await asyncio.gather(*list(scheduler._workers))
        stored = scheduled_task_mapper.get(task["id"])
        assert stored.status == ("completed" if played else "active")
        announce.assert_awaited_once_with("该喝水了", task_id=task["id"], scheduled_at=task["next_run_at"])
        assert not scheduler._devices
    asyncio.run(run())
