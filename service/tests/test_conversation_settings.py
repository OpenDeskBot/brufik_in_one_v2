from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, text

from deskbot_server.dao import device_mapper, user_mapper
from deskbot_server.dao.conversation_mapper import get_conversation
from deskbot_server.model.realtime import DEFAULT_VOICE
from deskbot_server.service.conversation_service import conversation_provider, conversation_status, save_conversation
from tests.test_controller_auth import _login
from tests.test_controller_auth import api_client as api_client


@pytest.fixture
def device(monkeypatch):
    monkeypatch.delenv("MOSS_API_KEY", raising=False)
    user_mapper.create("u", "conversation@example.com", "unused", False)
    device_mapper.insert("pk", "robot", "u", "robot")
    device_mapper.update_llm_provider("robot", "realtime")
    device_mapper.update_tts_provider("robot", "macos")
    return "robot"


def test_default_switch_and_preserve_local_settings(device):
    async def run():
        assert conversation_provider(device) == "local"
        with pytest.raises(ValueError):
            await save_conversation(device, {"provider": "moss-live"})
        status = await save_conversation(
            device,
            {
                "provider": "moss-live",
                "api_key": "not-returned",
                "voice": DEFAULT_VOICE,
                "camera": True,
                "prompt": "简洁回答",
            },
        )
        assert status["configured"] and status["camera"] and status["provider"] == "moss-live"
        assert "not-returned" not in json.dumps(status)
        await save_conversation(device, {"provider": "local", "api_key": ""})
        assert conversation_provider(device) == "local"
        before = device_mapper.get_by_device_id(device)
        assert before.llm_provider == "realtime" and before.tts_provider == "macos"
        assert json.loads(get_conversation(device)["conversation_param"])["api_key"] == "not-returned"
        await save_conversation(device, {"provider": "moss-live", "api_key": "********"})
        assert conversation_status(device)["configured"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"provider": []},
        {"provider": "other"},
        {"provider": "moss-live", "camera": "yes"},
        {"provider": "local", "voice": "unknown"},
        {"provider": "local", "prompt": ""},
    ],
)
def test_invalid_settings_leave_state_unchanged(device, body):
    async def run():
        with pytest.raises(ValueError):
            await save_conversation(device, body)
        assert conversation_provider(device) == "local"

    asyncio.run(run())


def test_environment_key_is_server_only(device, monkeypatch):
    monkeypatch.setenv("MOSS_API_KEY", "env-secret")
    asyncio.run(save_conversation(device, {"provider": "moss-live"}))
    assert conversation_status(device)["configured"]
    assert "env-secret" not in str(conversation_status(device))
    assert "env-secret" not in get_conversation(device)["conversation_param"]


def test_migration_keeps_existing_local_models_and_is_idempotent(tmp_path):
    from deskbot_server.db.init_db import _migrate_devices_schema

    engine = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    with engine.begin() as c:
        c.execute(text("CREATE TABLE devices (device_id TEXT, llm_provider TEXT, tts_provider TEXT)"))
        c.execute(text("INSERT INTO devices VALUES ('old','realtime','macos')"))
    _migrate_devices_schema(engine)
    _migrate_devices_schema(engine)
    with engine.connect() as c:
        r = c.execute(
            text("SELECT conversation_provider,conversation_param,llm_provider,tts_provider FROM devices")
        ).one()
        assert tuple(r) == ("local", None, "realtime", "macos")
    engine.dispose()


def test_api_requires_owner_and_never_returns_key(api_client, monkeypatch):
    monkeypatch.delenv("MOSS_API_KEY", raising=False)
    url = "/api/robot-settings/conversation"
    assert api_client.post(url, json={"provider": "local"}).status_code == 401
    _login(api_client, current_device_id="device-other")
    assert api_client.post(url, json={"provider": "local"}).status_code == 400
    assert conversation_provider("device-other") == "local"
    assert api_client.post(url + "?device_id=device-other", json={"provider": "local"}).status_code == 400
    api_client.cookies.clear()
    _login(api_client, current_device_id="device-owner")
    response = api_client.post(url, json={"provider": "moss-live", "api_key": "private-test-key"})
    assert response.status_code == 200, response.text
    assert "private-test-key" not in response.text
    assert response.json()["conversation"]["configured"]
    assert api_client.post(url, json={"provider": "local"}).status_code == 200


def test_flecket_settings_are_independent_and_secret(device, monkeypatch):
    from deskbot_server.model.realtime import FLECKET_URL
    from deskbot_server.service.conversation_service import flecket_config, moss_config

    monkeypatch.delenv("FLECKET_API_KEY", raising=False)

    async def run():
        await save_conversation(device, {"provider": "moss-live", "api_key": "moss-secret", "voice": DEFAULT_VOICE})
        with pytest.raises(ValueError):
            await save_conversation(device, {"provider": "flecket"})
        result = await save_conversation(device, {"provider": "flecket", "api_key": "flecket-secret",
            "url": "http://127.0.0.1:9110/", "voice": "Tingting", "prompt": "简洁回答", "camera": True})
        assert result["provider"] == conversation_provider(device) == "flecket"
        assert result["url"] == FLECKET_URL and result["camera"]
        assert "flecket-secret" not in json.dumps(result) and "moss-secret" not in json.dumps(result)
        assert flecket_config(device).api_key == "flecket-secret"
        assert moss_config(device).api_key == "moss-secret"
        await save_conversation(device, {"provider": "moss-live", "api_key": ""})
        assert moss_config(device).voice == DEFAULT_VOICE
        await save_conversation(device, {"provider": "flecket", "api_key": "********"})
        assert flecket_config(device).voice == "Tingting"
        assert flecket_config(device).api_key == "flecket-secret"
        await save_conversation(device, {"provider": "local", "voice": DEFAULT_VOICE, "camera": False})
        assert flecket_config(device).camera is True
        assert moss_config(device).api_key == "moss-secret"
        await save_conversation(device, {"provider": "flecket"})
        before = get_conversation(device)
        with pytest.raises(ValueError):
            await save_conversation(device, {"provider": "flecket", "url": "ws://key:secret@host"})
        assert get_conversation(device) == before

    asyncio.run(run())


def test_flecket_environment_key_and_owner_api(api_client, monkeypatch):
    monkeypatch.setenv("FLECKET_API_KEY", "env-flecket-key")
    _login(api_client, current_device_id="device-owner")
    response = api_client.post("/api/robot-settings/conversation", json={"provider": "flecket"})
    assert response.status_code == 200, response.text
    assert response.json()["conversation"]["provider"] == "flecket"
    assert response.json()["conversation"]["configured"]
    assert "env-flecket-key" not in response.text
    assert "env-flecket-key" not in get_conversation("device-owner")["conversation_param"]
    api_client.cookies.clear()
    _login(api_client, current_device_id="device-other")
    response = api_client.post("/api/robot-settings/conversation", json={"provider": "flecket"})
    assert response.status_code == 400
    assert conversation_provider("device-other") == "local"


def test_flecket_barge_in_is_explicitly_configurable(device, monkeypatch):
    from deskbot_server.service.conversation_service import flecket_config
    monkeypatch.setenv("FLECKET_API_KEY", "test-key")

    async def run():
        status = await save_conversation(device, {"provider": "flecket"})
        assert status["allow_barge_in"] is False
        await save_conversation(device, {"provider": "flecket", "allow_barge_in": True})
        assert flecket_config(device).allow_barge_in is True
        with pytest.raises(ValueError):
            await save_conversation(device, {"provider": "flecket", "allow_barge_in": "true"})

    asyncio.run(run())


def test_saved_agent_defaults_and_custom_endpoint_roundtrip(device, monkeypatch):
    from deskbot_server.service import conversation_service as service

    endpoint = "wss://voice.example/v1/realtime?model=agent_saved"
    monkeypatch.setattr(service, "get_config", lambda: {"conversation": {"flecket": {
        "url": endpoint, "agents": [{"name": "Saved agent", "model": "agent_saved"}],
    }}})
    monkeypatch.setenv("FLECKET_API_KEY", "server-secret")

    async def run():
        status = await save_conversation(device, {"provider": "flecket", "prompt": "", "voice": ""})
        assert status["url"] == endpoint
        assert status["agents"] == [{"name": "Saved agent", "model": "agent_saved"}]
        assert not service.flecket_config(device).prompt
        assert "server-secret" not in json.dumps(status)
        custom = "https://other.example/v1?model=agent_other"
        status = await save_conversation(device, {"provider": "flecket", "url": custom})
        assert status["url"] == "wss://other.example/v1/realtime?model=agent_other"
        assert service.flecket_config(device).url == status["url"]

    asyncio.run(run())


def test_cloud_agent_environment_override_is_normalized(monkeypatch):
    from deskbot_server.config import merge_environment

    monkeypatch.setenv("FLECKET_URL", "https://cloud.example/v1?model=agent_cloud")
    config = merge_environment({"conversation": {"flecket": {"agents": [
        {"name": "Saved", "model": "agent_saved"},
    ]}}})
    assert config["conversation"]["flecket"]["url"] == "wss://cloud.example/v1/realtime?model=agent_cloud"
    assert config["conversation"]["flecket"]["agents"][0]["model"] == "agent_saved"


def test_agent_catalog_cannot_expose_credentials():
    from deskbot_server.config import validate_config

    with pytest.raises(ValueError):
        validate_config({"conversation": {"flecket": {"agents": [
            {"name": "Saved", "model": "agent_saved", "api_key": "private"},
        ]}}})
