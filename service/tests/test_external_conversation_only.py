"""External-only production routing, startup and retired API regressions."""

import asyncio
import builtins
from unittest.mock import AsyncMock

import pytest

from deskbot_server.dao import device_mapper, user_mapper
from deskbot_server.dao.conversation_mapper import get_conversation, set_conversation
from deskbot_server.db.engine import init_engine
from deskbot_server.service.conversation_service import conversation_provider, conversation_status
from deskbot_server.service.device_ws_service import DeviceWsService
from tests.test_controller_auth import _login
from tests.test_controller_auth import api_client as api_client


@pytest.mark.parametrize("stored", ["local", "", "unknown", "flecket", "moss-live"])
def test_device_route_never_builds_a_local_pipeline(monkeypatch, stored):
    user_mapper.create("u", "external-only@example.com", "unused", False)
    device_mapper.insert("pk", "robot", "u", "robot")
    set_conversation("robot", stored, "{}")
    bridge = AsyncMock()
    monkeypatch.setattr("deskbot_server.service.moss_live_service.handle_moss_connection", bridge)
    hub = DeviceWsService()
    websocket = object()
    asyncio.run(hub.handle_asr_chat(websocket, "robot"))
    expected = "moss-live" if stored == "moss-live" else "flecket"
    bridge.assert_awaited_once_with(hub, websocket, "robot", provider=expected)
    assert not hasattr(hub, "_pipeline")
    assert not hasattr(hub, "_audio_cfg")


def test_missing_device_id_is_rejected_before_upstream(monkeypatch):
    bridge = AsyncMock()
    monkeypatch.setattr("deskbot_server.service.moss_live_service.handle_moss_connection", bridge)
    websocket = AsyncMock()
    asyncio.run(DeviceWsService().handle_asr_chat(websocket, None))
    websocket.close.assert_awaited_once_with(code=1008, reason="device_id required")
    bridge.assert_not_awaited()


def test_migration_preserves_external_profiles_and_credentials():
    from deskbot_server.db.init_db import _migrate_devices_schema

    user_mapper.create("u", "migration-only@example.com", "unused", False)
    for provider in ("local", "flecket", "moss-live"):
        device_mapper.insert("pk-" + provider, provider, "u", provider)
        set_conversation(provider, provider, '{"flecket":{"api_key":"preserve-me"}}')
    _migrate_devices_schema(init_engine())
    _migrate_devices_schema(init_engine())
    for provider in ("local", "flecket", "moss-live"):
        row = get_conversation(provider)
        assert row["conversation_provider"] == ("flecket" if provider == "local" else provider)
        assert row["conversation_param"] == '{"flecket":{"api_key":"preserve-me"}}'
    assert conversation_provider("missing") == "flecket"
    assert set(conversation_status("missing")["profiles"]) == {"flecket", "moss-live"}


def test_runtime_does_not_import_or_bind_local_voice_services(tmp_path, monkeypatch):
    import deskbot_server.config as config
    from deskbot_server.main import build_runtime

    monkeypatch.setattr(config, "_RUNTIME_CONFIG", None)
    monkeypatch.setattr(config, "_RUNTIME_PATH", None)

    cfg = tmp_path / "external.yaml"
    cfg.write_text("camera_face:\n  mode: none\nvoiceprint:\n  mode: none\n")
    monkeypatch.setenv("DESKBOT_SERVER_CONFIG", str(cfg))
    original_import = builtins.__import__
    forbidden = (
        "deskbot_server.infrastructure.bootstrap",
        "deskbot_server.service.application.chat_",
        "deskbot_server.service.application.ws_chat_turn",
        "deskbot_server.service.application.scheduled_task_scheduler",
        "deskbot_server.service.application.quest_proactive",
        "deskbot_server.service.application.social_proactive",
        "deskbot_server.service.pipeline.audio",
        "deskbot_server.service.vad_service",
    )

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(forbidden), f"retired local pipeline imported: {name}"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    runtime = build_runtime()
    assert not hasattr(runtime, "chat")
    assert not hasattr(runtime, "audio_cfg")
    from deskbot_server.service.application.external_reminder_scheduler import ExternalReminderScheduler
    assert isinstance(runtime.scheduler, ExternalReminderScheduler)
    asyncio.run(runtime.device_ws.shutdown())


def test_settings_expose_only_external_conversations(api_client):
    _login(api_client, current_device_id="device-owner")
    data = api_client.get("/api/robot-settings").json()
    assert data["conversation"]["provider"] == "flecket"
    assert set(data["conversation"]["profiles"]) == {"flecket", "moss-live"}
    assert set(data["capabilities"]) == {"face", "voiceprint"}
    assert api_client.post("/api/robot-settings/conversation", json={"provider": "local"}).status_code == 400
    for capability in ("asr", "llm", "tts", "decision"):
        assert api_client.post(f"/api/robot-settings/{capability}", json={"provider": "realtime"}).status_code == 404
    assert api_client.put("/api/devices/device-owner/llm", json={"provider": "realtime"}).status_code == 404


@pytest.mark.parametrize("method,path", [
    ("GET", "asr/config-info"), ("GET", "asr/default-audio"),
    ("POST", "asr/config"), ("POST", "asr/test"), ("POST", "asr/clear-device-override"),
    ("GET", "llm/config-info"), ("POST", "llm/config"), ("POST", "llm/test"),
    ("POST", "llm/clear-device-override"),
    ("GET", "tts/config-info"), ("GET", "tts/test-info"), ("POST", "tts/config"),
    ("POST", "tts/test"), ("POST", "tts/clear-device-override"),
])
def test_retired_model_configuration_apis(api_client, method, path):
    _login(api_client, current_device_id="device-owner")
    response = api_client.request(method, "/api/robot-settings/" + path)
    assert response.status_code == 404


@pytest.mark.parametrize("url", ["/api/device_tts", "/api/scene_playbook/run"])
def test_retired_local_playback_reports_gone(api_client, url):
    assert api_client.post(url, json={"device_id": "device-owner", "text": "hello"}).status_code == 401
    _login(api_client, current_device_id="device-owner")
    assert api_client.post(url, json={"device_id": "device-owner", "text": "hello"}).status_code == 410
