"""机器人设置（ASR/LLM/TTS 能力热切换）测试。

用临时 config.yaml + fake adapter 隔离，不加载真实 FunASR / 豆包 adapter。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from deskbot_server.service.robot_capability import (
    ASR_CANDIDATES,
    LLM_CANDIDATES,
    LLM_ENGINE_BASE_URL,
    TTS_CANDIDATES,
    CapabilityError,
    RobotCapabilityService,
)

MINIMAL_CONFIG = {
    "asr": {
        "external_url": "http://127.0.0.1:9102",
        "text_filter": {"min_text_len": 2, "min_chinese_ratio": 0.0},
    },
    "llm": {
        "protocol": "ark_responses",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model_name": "ep-test",
    },
    "tts": {"provider": "doubao", "sample_rate": 24000},
}


@pytest.fixture()
def temp_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture()
def svc(temp_cfg: Path) -> RobotCapabilityService:
    return RobotCapabilityService(config_path=temp_cfg)


@pytest.fixture()
def clean_llm_env(monkeypatch):
    """隔离宿主环境的 LLM env 覆盖，保证 resolve_system_llm_config 走 config.yaml。"""
    for name in ("LLM_PROTOCOL", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "ARK_API_KEY", "VOLCENGINE_API_KEY", "DOUBAO_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY", "ASR_PROVIDER", "TTS_PROVIDER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def fake_asr_build(monkeypatch):
    """fake build_asr_adapter：记录构造，可配置抛异常。"""
    from deskbot_server.ports.asr import AsrPort

    state = {"built": [], "fail": None}

    class FakeAsr(AsrPort):
        def __init__(self, provider: str):
            self.provider = provider

        async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
            return ""

        def is_valid_text(self, text: str) -> bool:
            return bool(text)

    def build(settings):
        if state["fail"]:
            raise state["fail"]
        adapter = FakeAsr(settings.asr.provider)
        state["built"].append(adapter)
        return adapter

    monkeypatch.setattr("deskbot_server.service.robot_capability.build_asr_adapter", build)
    return state


@pytest.fixture()
def bound_asr():
    """给 AsrService 单例绑一个初始 fake，测试后复位。"""
    from deskbot_server.service.asr_service import AsrService

    class _Initial:
        async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
            return "initial"

        def is_valid_text(self, text: str) -> bool:
            return True

    initial = _Initial()
    AsrService().bind(initial)
    yield initial
    AsrService().bind(_Initial())


# ---------- 1. 能力目录 ----------

def test_capability_catalogs_structure():
    assert [c.id for c in ASR_CANDIDATES] == ["funasr", "doubao"]
    assert [c.id for c in LLM_CANDIDATES] == ["ark", "local"]
    assert [c.id for c in TTS_CANDIDATES] == ["doubao"]

    ark = LLM_CANDIDATES[0]
    local = LLM_CANDIDATES[1]
    assert ark.experimental is False
    assert local.experimental is True
    assert local.requires_service == "llm-engine"
    assert ASR_CANDIDATES[0].requires_service == "funasr"
    for cap in (*ASR_CANDIDATES, *LLM_CANDIDATES, *TTS_CANDIDATES):
        assert cap.id and cap.name and cap.description


# ---------- 2. ASR 设备级配置 ----------

@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile

    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        reset_engine()
        init_engine(db_path)
        init_database()  # 含 _migrate_devices_schema（asr_provider 列）
        yield db_path


@pytest.fixture()
def device(temp_db):
    """绑定一台测试设备。"""
    from tests.device_bind_helpers import bind_device_online
    from deskbot_server.service.user_service import UserService

    user = UserService().register("asr-device@example.com", "password1234")
    return bind_device_online(user.id, "deskbot_asr")


def test_apply_asr_writes_device_table(svc, device, clean_llm_env):
    """ASR 为设备级：apply_asr 写 device 表，动态解析即时生效（不落 config）。"""
    from deskbot_server.dao.device_mapper import get_asr_provider
    from deskbot_server.config import load_config

    assert get_asr_provider("deskbot_asr") == "funasr"  # 默认

    svc.apply_asr("doubao", "deskbot_asr")

    assert get_asr_provider("deskbot_asr") == "doubao"
    assert "provider" not in load_config(svc._config_path).get("asr", {})  # config 无 provider

    status = svc.get_status("deskbot_asr")
    assert status["capabilities"]["asr"]["current"] == "doubao"


def test_apply_asr_requires_device(svc):
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.apply_asr("doubao", None)


def test_apply_asr_unknown_provider_rejected(svc, device):
    from deskbot_server.dao.device_mapper import get_asr_provider

    with pytest.raises(CapabilityError, match="未知的 ASR 能力"):
        svc.apply_asr("bogus", "deskbot_asr")
    assert get_asr_provider("deskbot_asr") == "funasr"


def test_clear_device_asr_override_resets_to_funasr(svc, device):
    from deskbot_server.dao.device_mapper import get_asr_provider

    svc.apply_asr("doubao", "deskbot_asr")
    assert get_asr_provider("deskbot_asr") == "doubao"

    svc.clear_device_asr_override("deskbot_asr")
    assert get_asr_provider("deskbot_asr") == "funasr"
    assert svc.get_status("deskbot_asr")["capabilities"]["asr"]["current"] == "funasr"


def test_asr_status_default_when_no_device(svc, temp_db):
    """匿名/未选择设备 → 默认 funasr。"""
    assert svc.get_status(None)["capabilities"]["asr"]["current"] == "funasr"


# ---------- 3. LLM 切换 ----------

def test_apply_llm_local_snapshots_ark(svc, clean_llm_env):
    from deskbot_server.config import load_config

    svc.apply_llm("local")

    llm = load_config(svc._config_path)["llm"]
    assert llm["protocol"] == "openai"
    assert llm["base_url"] == LLM_ENGINE_BASE_URL
    assert llm["model_name"] == "cactus-needle-2"
    assert llm["ark_base_url"] == "https://ark.cn-beijing.volces.com/api/v3"  # 快照
    assert llm["ark_model_name"] == "ep-test"

    status = svc.get_status()
    assert status["capabilities"]["llm"]["current"] == "local"
    assert status["capabilities"]["llm"]["effective"]["base_url"] == LLM_ENGINE_BASE_URL


def test_apply_llm_ark_restores_from_snapshot(svc, clean_llm_env):
    from deskbot_server.config import load_config

    svc.apply_llm("local")
    svc.apply_llm("ark")

    llm = load_config(svc._config_path)["llm"]
    assert llm["protocol"] == "ark_responses"
    assert llm["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert llm["model_name"] == "ep-test"
    assert "ark_base_url" not in llm
    assert "ark_model_name" not in llm


def test_apply_llm_clears_env_override_keeps_api_key(svc, clean_llm_env, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROTOCOL=openai\nLLM_MODEL=env-model\nLLM_BASE_URL=https://env.example.com/v1\nARK_API_KEY=ark-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("deskbot_server.utils.paths.ENV_FILE", env_file)
    monkeypatch.setenv("LLM_PROTOCOL", "openai")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("ARK_API_KEY", "ark-secret")

    svc.apply_llm("local")

    # 协议类覆盖被清除，密钥保留
    assert not os.environ.get("LLM_PROTOCOL")
    assert not os.environ.get("LLM_MODEL")
    assert not os.environ.get("LLM_BASE_URL")
    assert os.environ.get("ARK_API_KEY") == "ark-secret"
    text = env_file.read_text(encoding="utf-8")
    assert "LLM_PROTOCOL" not in text
    assert "ARK_API_KEY" in text

    # 系统级生效为 local 配置（同一份 config 真源）
    from deskbot_server.infrastructure.llm.runtime import resolve_system_llm_config

    resolved = resolve_system_llm_config(svc._load_cfg())
    assert resolved.api_base == LLM_ENGINE_BASE_URL


# ---------- 4. 本地 URL 判定 / key 豁免 / 流式禁用 ----------

def test_is_local_llm_url():
    from deskbot_server.infrastructure.llm.runtime import is_local_llm_url

    assert is_local_llm_url("http://127.0.0.1:9104/v1")
    assert is_local_llm_url("http://localhost:9104")
    assert is_local_llm_url("http://10.0.0.5/v1")
    assert is_local_llm_url("http://192.168.1.10/chat")
    assert not is_local_llm_url("https://ark.cn-beijing.volces.com/api/v3")
    assert not is_local_llm_url("")
    assert not is_local_llm_url(None)


def test_validate_api_key_exempts_local_url():
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, _validate_api_key

    _validate_api_key(
        ResolvedLlmConfig(model="m", api_key="", api_base="http://127.0.0.1:9104/v1", protocol="openai", source="system", display_name="x")
    )  # 本地：空 key 不抛

    with pytest.raises(ValueError, match="API Key"):
        _validate_api_key(
            ResolvedLlmConfig(model="m", api_key="", api_base="https://ark.cn-beijing.volces.com/api/v3", protocol="ark", source="system", display_name="x")
        )


def test_local_llm_forces_non_stream(monkeypatch):
    from deskbot_server.infrastructure.llm import runtime as llm_runtime

    captured = {"stream": 0, "plain": 0}

    def fake_stream(*args, **kwargs):
        captured["stream"] += 1
        return '{"ok":true}', None

    def fake_plain(*args, **kwargs):
        captured["plain"] += 1
        return '{"ok":true}', None

    monkeypatch.setattr(llm_runtime, "_request_chat_completion_stream", fake_stream)
    monkeypatch.setattr(llm_runtime, "_request_chat_completion", fake_plain)

    cfg = llm_runtime.ResolvedLlmConfig(
        model="cactus-needle-2", api_key="", api_base="http://127.0.0.1:9104/v1", protocol="openai", source="system", display_name="local"
    )

    import asyncio

    asyncio.run(llm_runtime.chat_acompletion([{"role": "user", "content": "hi"}], config=cfg, stream=True))

    assert captured["stream"] == 0  # 本地强制非流式
    assert captured["plain"] == 1


# ---------- 5. 设备级 LLM 覆盖 ----------

def test_device_override_status_and_clear(svc, clean_llm_env, monkeypatch, tmp_path, temp_db):
    monkeypatch.setattr("deskbot_server.utils.device_data.DATA_DIR", tmp_path)
    from deskbot_server.dao.llm_config_store import add_llm_model, set_active_llm_model

    add_llm_model("deskbot_x", name="测试模型", model_name="test-model", protocol="openai", base_url="", api_key="sk-x")
    set_active_llm_model("deskbot_x", None)
    status = svc.get_status("deskbot_x")
    assert status["capabilities"]["llm"]["device_override"]["active"] is False

    model = add_llm_model("deskbot_x", name="覆盖模型", model_name="override-model", protocol="openai", base_url="", api_key="sk-y")
    set_active_llm_model("deskbot_x", model["id"])

    status = svc.get_status("deskbot_x")
    llm = status["capabilities"]["llm"]
    assert llm["device_override"]["active"] is True
    assert llm["device_override"]["model"]["name"] == "覆盖模型"
    assert llm["current"] == "device"

    svc.clear_device_llm_override("deskbot_x")

    status = svc.get_status("deskbot_x")
    assert status["capabilities"]["llm"]["device_override"]["active"] is False
    assert status["capabilities"]["llm"]["current"] == "ark"


def test_clear_device_override_requires_device(svc):
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.clear_device_llm_override(None)


# ---------- 6. API 层 ----------

@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile

    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        reset_engine()
        init_engine(db_path)
        init_database()
        yield db_path


def test_api_robot_settings_endpoints(temp_db):
    from tests._auth_compat import create_user
    from deskbot_server.web.app import create_app

    create_user("robot-settings@example.com", "password1234")
    app = create_app()
    client = app.test_client()

    # 未登录 → 401
    assert client.get("/api/robot-settings").status_code == 401

    client.post("/login", data={"email": "robot-settings@example.com", "password": "password1234"})

    resp = client.get("/api/robot-settings")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["capabilities"]["asr"]["current"] in ("funasr", "doubao")
    assert payload["capabilities"]["tts"]["current"] == "doubao"
    assert payload["capabilities"]["llm"]["current"] in ("ark", "local", "device", "custom")

    # 非法 provider → 400
    bad = client.post("/api/robot-settings/asr", json={"provider": "bogus"})
    assert bad.status_code == 400
    assert bad.get_json()["ok"] is False

    # 未选择设备时清除覆盖 → 400
    clear = client.post("/api/robot-settings/llm/clear-device-override")
    assert clear.status_code == 400


def test_robot_settings_page_renders(temp_db):
    from tests._auth_compat import create_user
    from deskbot_server.web.app import create_app

    create_user("robot-settings-page@example.com", "password1234")
    app = create_app()
    client = app.test_client()

    assert client.get("/robot-settings").status_code == 302  # 匿名跳登录

    client.post("/login", data={"email": "robot-settings-page@example.com", "password": "password1234"})
    resp = client.get("/robot-settings")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "机器人设置" in html
    assert "语音识别 ASR" in html
    assert "大脑 LLM" in html
    assert "语音合成 TTS" in html
    assert "/api/robot-settings" in html
