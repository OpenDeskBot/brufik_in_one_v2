"""火山方舟 ark_responses 端到端集成测试（需网络；密钥经设备 llm_param 注入，宿主 env ARK_API_KEY/ARK_MODEL 仅作测试数据源）。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


def _ark_configured() -> bool:
    key = os.environ.get("ARK_API_KEY", "").strip()
    model = os.environ.get("ARK_MODEL", "").strip()
    return bool(key and model and not key.startswith("请替换"))


pytestmark = pytest.mark.skipif(not _ark_configured(), reason="ARK_API_KEY / ARK_MODEL 未配置")


@pytest.fixture()
def device_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monkeypatch.setenv("DESKBOT_DB_PATH", str(root / "test.db"))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(root / "test.db")
        init_database()
        yield "deskbot_ark_e2e"


def _configure_ark_device(device_id: str) -> None:
    """设备级 ark：写 llm_provider='ark' + llm_param['ark']（密钥存设备表，不用 llm_models.json）。"""
    from deskbot_server.dao.device_mapper import update_llm_param, update_llm_provider

    update_llm_provider(device_id, "ark")
    update_llm_param(
        device_id,
        json.dumps(
            {
                "ark": {
                    "api_key": os.environ["ARK_API_KEY"].strip(),
                    "model_name": os.environ["ARK_MODEL"].strip(),
                    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                }
            }
        ),
    )


def test_live_ark_responses_chat_completion():
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, chat_completion

    cfg = ResolvedLlmConfig(
        model=os.environ["ARK_MODEL"].strip(),
        api_key=os.environ["ARK_API_KEY"].strip(),
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="ark_responses",
        source="test",
        display_name="DeepSeek v4 Flash",
    )
    content, meta = chat_completion(
        [{"role": "user", "content": '只输出 JSON：{"tts":"集成测试通过"}'}], config=cfg, json_mode=True
    )
    parsed = json.loads(content)
    assert parsed["tts"] == "集成测试通过"
    assert meta["usage"]["total_tokens"] > 0


def test_live_ark_responses_stream_tts_prefetch():
    import asyncio

    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, chat_acompletion

    cfg = ResolvedLlmConfig(
        model=os.environ["ARK_MODEL"].strip(),
        api_key=os.environ["ARK_API_KEY"].strip(),
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        protocol="ark_responses",
        source="test",
        display_name="DeepSeek v4 Flash",
    )
    tts_chunks: list[str] = []

    async def on_tts(text: str) -> None:
        tts_chunks.append(text)

    async def _run():
        return await chat_acompletion(
            [{"role": "user", "content": '只输出 JSON：{"tts":"流式通过","tools":[]}'}],
            config=cfg,
            json_mode=True,
            on_tts_ready=on_tts,
        )

    content, meta = asyncio.run(_run())
    parsed = json.loads(content)
    assert parsed["tts"] == "流式通过"
    assert tts_chunks == ["流式通过"]
    assert meta["usage"]["total_tokens"] > 0


def test_api_device_ark_chat(device_env):
    from tests.device_bind_helpers import bind_device_online
    from tests._auth_compat import create_user
    from deskbot_server.infrastructure.llm.runtime import resolve_llm_config
    from deskbot_server.web.app import create_app

    device_id = device_env
    user = create_user("ark-e2e@example.com", "password1234")
    bind_device_online(user.id, device_id)
    _configure_ark_device(device_id)

    resolved = resolve_llm_config(device_id)
    assert resolved.protocol == "ark_responses"
    assert resolved.model == os.environ["ARK_MODEL"].strip()

    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": "ark-e2e@example.com", "password": "password1234"})

    # HTTP 侧用 /api/llm/chat（设备级解析同源）验证端到端对话
    tested = client.post(
        "/api/llm/chat",
        json={"device_id": device_id, "text": "你好，请用一句话介绍你自己。"},
    )
    assert tested.status_code == 200, tested.get_data(as_text=True)
    test_payload = tested.get_json()
    assert test_payload["ok"] is True
    assert len(test_payload.get("reply") or "") > 0


def test_openai_adapter_with_ark_device(device_env):
    import asyncio

    from tests.device_bind_helpers import bind_device_online
    from deskbot_server.auth.service import create_user
    from deskbot_server.config import load_config
    from deskbot_server.model.settings import AppSettings
    from deskbot_server.infrastructure.llm.openai_compat import OpenAiLlmAdapter

    device_id = device_env
    create_user("ark-adapter@example.com", "password1234")
    bind_device_online(create_user("ark-adapter2@example.com", "password1234").id, device_id)
    _configure_ark_device(device_id)

    settings = AppSettings.from_config(load_config())
    adapter = OpenAiLlmAdapter(settings)

    async def _run():
        return await adapter.complete("说三个字：测试通过", device_id=device_id)

    answer = asyncio.run(_run())
    parsed = json.loads(answer)
    assert isinstance(parsed.get("tts"), str)
    assert len(parsed["tts"]) > 0


def test_debug_llm_chat_with_ark_device(device_env):
    from tests.device_bind_helpers import bind_device_online
    from deskbot_server.auth.service import create_user
    from deskbot_server.web.app import create_app

    device_id = device_env
    user = create_user("ark-debug@example.com", "password1234")
    bind_device_online(user.id, device_id)
    _configure_ark_device(device_id)

    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": "ark-debug@example.com", "password": "password1234"})

    resp = client.post("/api/llm/chat", json={"text": "你好", "device_id": device_id})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload["ok"] is True
    assert len(payload.get("reply") or payload.get("raw") or "") > 0
