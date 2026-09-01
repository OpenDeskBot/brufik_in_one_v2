"""机器人设置页 LLM 配置（config-info / config / test 试聊）测试。

覆盖：ark 配置信息（掩码 key / 快照键读取）、保存语义（.env 密钥 + config.yaml
主键/快照键）、本地模型只读端点、试聊临时 config（覆盖字段优先 / 本地免 Key）、
三个 API 端点。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from deskbot_server.service.robot_capability import CapabilityError, RobotCapabilityService

MINIMAL_LLM_CFG = {
    "llm": {
        "protocol": "ark_responses",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model_name": "ep-test",
    }
}


@pytest.fixture()
def tmp_env(monkeypatch, tmp_path):
    """隔离 .env：update_env_keys / read_env_file 读 tts.env_store.ENV_FILE。"""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", env_file)
    return env_file


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


def _llm_svc(tmp_path: Path, cfg: dict | None = None) -> RobotCapabilityService:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg or MINIMAL_LLM_CFG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return RobotCapabilityService(config_path=p)


@pytest.fixture()
def clean_env(monkeypatch):
    for name in ("ARK_API_KEY", "LLM_API_KEY", "VOLCENGINE_API_KEY", "DOUBAO_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# ---------- config-info ----------


def test_llm_config_info_ark_masked_key_and_defaults(tmp_path, clean_env, tmp_env):
    svc = _llm_svc(tmp_path)
    info = svc.llm_config_info("ark")
    assert info["provider"] == "ark"
    assert info["readonly"] is False
    assert info["api_key"] == "" and info["api_key_set"] is False  # 无 key
    assert info["model_name"] == "ep-test"
    assert info["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"


def test_llm_config_info_ark_masks_env_key(tmp_path, clean_env, tmp_env, monkeypatch):
    from deskbot_server.infrastructure.tts.doubao import _mask_secret

    monkeypatch.setenv("ARK_API_KEY", "ark-secret-key-123")
    svc = _llm_svc(tmp_path)
    info = svc.llm_config_info("ark")
    assert info["api_key"] == _mask_secret("ark-secret-key-123")
    assert info["api_key_set"] is True


def test_llm_config_info_ark_uses_snapshot_when_local_active(tmp_path, clean_env, tmp_env):
    """当前生效为本地模型时，ark 配置信息读快照键（apply_llm 切走时保存）。"""
    svc = _llm_svc(
        tmp_path,
        {
            "llm": {
                "protocol": "openai",
                "base_url": "http://127.0.0.1:9106/v1",
                "model_name": "qwen3.8-2b",
                "ark_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "ark_model_name": "ep-snap",
            }
        },
    )
    info = svc.llm_config_info("ark")
    assert info["model_name"] == "ep-snap"  # 快照键，而非当前 qwen 的 model_name
    assert info["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"


def test_llm_config_info_local_readonly(tmp_path, clean_env, tmp_env):
    svc = _llm_svc(tmp_path)
    info = svc.llm_config_info("minicpm")
    assert info["readonly"] is True
    assert info["base_url"] == "http://127.0.0.1:9105/v1"
    assert info["model_name"] == "minicpm5-1b"
    info = svc.llm_config_info("qwen")
    assert info["readonly"] is True
    assert info["base_url"] == "http://127.0.0.1:9106/v1"
    assert info["model_name"] == "qwen3.8-2b"


def test_llm_config_info_unknown_provider_rejected(tmp_path, clean_env, tmp_env):
    svc = _llm_svc(tmp_path)
    with pytest.raises(CapabilityError):
        svc.llm_config_info("bogus")


# ---------- save_llm_config ----------


def test_save_llm_config_writes_env_and_main_keys(tmp_path, clean_env, tmp_env):
    from deskbot_server.config import load_config

    svc = _llm_svc(tmp_path)
    svc.save_llm_config(
        "ark", {"api_key": "ark-new-key", "model_name": "ep-2", "base_url": "https://ark.cn-beijing.volces.com/api/v3"}
    )
    # API Key → .env
    assert "ARK_API_KEY=ark-new-key" in tmp_env.read_text(encoding="utf-8")
    # 当前协议为 ark → 主键写入 + 快照键清理
    llm = load_config(svc._config_path)["llm"]
    assert llm["model_name"] == "ep-2"
    assert llm["base_url"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert "ark_model_name" not in llm
    assert "ark_base_url" not in llm
    # config-info 回填：掩码 + 新值
    info = svc.llm_config_info("ark")
    assert info["model_name"] == "ep-2"
    assert info["api_key_set"] is True


def test_save_llm_config_masked_api_key_keeps_existing(tmp_path, clean_env, tmp_env):
    from deskbot_server.infrastructure.tts.doubao import _mask_secret

    svc = _llm_svc(tmp_path)
    svc.save_llm_config("ark", {"api_key": "ark-real-key", "model_name": "ep-1"})
    svc.save_llm_config("ark", {"api_key": _mask_secret("ark-real-key"), "model_name": "ep-2"})
    env_text = tmp_env.read_text(encoding="utf-8")
    assert "ARK_API_KEY=ark-real-key" in env_text  # 掩码不覆盖
    assert "ARK_API_KEY=" + _mask_secret("ark-real-key") not in env_text


def test_save_llm_config_writes_snapshot_when_local_active(tmp_path, clean_env, tmp_env):
    """当前生效为本地模型时，保存 ark 配置写快照键，不污染本地主键。"""
    from deskbot_server.config import load_config

    svc = _llm_svc(
        tmp_path,
        {
            "llm": {
                "protocol": "openai",
                "base_url": "http://127.0.0.1:9106/v1",
                "model_name": "qwen3.8-2b",
            }
        },
    )
    svc.save_llm_config("ark", {"api_key": "ark-key", "model_name": "ep-9", "base_url": ""})
    llm = load_config(svc._config_path)["llm"]
    assert llm["protocol"] == "openai"  # 当前本地配置不动
    assert llm["model_name"] == "qwen3.8-2b"
    assert llm["ark_model_name"] == "ep-9"  # 预存快照，切回 ark 时生效
    info = svc.llm_config_info("ark")
    assert info["model_name"] == "ep-9"


def test_save_llm_config_local_provider_noop(tmp_path, clean_env, tmp_env):
    """本地模型无可保存字段：payload 忽略，config 与 .env 均不被写。"""
    from deskbot_server.config import load_config

    svc = _llm_svc(tmp_path)
    before = load_config(svc._config_path)
    svc.save_llm_config("minicpm", {"api_key": "x", "model_name": "y"})
    assert load_config(svc._config_path) == before
    assert tmp_env.read_text(encoding="utf-8") == ""


# ---------- llm_test（试聊） ----------


def _fake_chat(captured: dict):
    async def fake(messages, **kwargs):
        captured["messages"] = messages
        captured["cfg"] = kwargs.get("config")
        return "收到，我在。", {"model": "test-model", "usage": {"total_tokens": 9}}

    return fake


def test_llm_test_ark_overrides_beat_config(tmp_path, clean_env, tmp_env, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.chat_acompletion", _fake_chat(captured)
    )
    monkeypatch.setenv("ARK_API_KEY", "env-key")
    svc = _llm_svc(tmp_path)
    result = asyncio.run(
        svc.llm_test(
            "ark", "你好",
            overrides={"api_key": "form-key", "model_name": "ep-form", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
        )
    )
    assert result["ok"] is True
    assert result["reply"] == "收到，我在。"
    cfg = captured["cfg"]
    assert cfg.api_key == "form-key"  # 表单覆盖 > env
    assert cfg.model == "ep-form"
    assert cfg.protocol == "ark_responses"
    assert captured["messages"][0]["content"] == "你好"


def test_llm_test_ark_env_key_fallback_and_config_model(tmp_path, clean_env, tmp_env, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.chat_acompletion", _fake_chat(captured)
    )
    monkeypatch.setenv("ARK_API_KEY", "env-key")
    svc = _llm_svc(tmp_path)  # config: ep-test / 官方 base_url
    result = asyncio.run(svc.llm_test("ark", ""))  # 空文本 → 默认文本
    assert result["ok"] is True
    assert captured["messages"][0]["content"] == "你好，请用一句话简短回复。"
    assert captured["cfg"].api_key == "env-key"
    assert captured["cfg"].model == "ep-test"


def test_llm_test_local_no_api_key_needed(tmp_path, clean_env, tmp_env, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.chat_acompletion", _fake_chat(captured)
    )
    svc = _llm_svc(tmp_path)
    result = asyncio.run(svc.llm_test("minicpm", "你好"))
    assert result["ok"] is True
    cfg = captured["cfg"]
    assert cfg.api_key == ""  # 本地免 Key（is_local_llm_url 豁免）
    assert cfg.api_base == "http://127.0.0.1:9105/v1"
    assert cfg.model == "minicpm5-1b"


def test_llm_test_failure_returns_ok_false(tmp_path, clean_env, tmp_env, monkeypatch):
    async def fail(messages, **kwargs):
        raise ConnectionError("无法连接")

    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", fail)
    svc = _llm_svc(tmp_path)
    result = asyncio.run(svc.llm_test("qwen", "你好"))
    assert result["ok"] is False
    assert "无法连接" in result["error"]


def test_llm_test_unknown_provider_rejected(tmp_path, clean_env, tmp_env):
    svc = _llm_svc(tmp_path)
    with pytest.raises(CapabilityError):
        asyncio.run(svc.llm_test("bogus", "你好"))


# ---------- API 端点 ----------


def _login_client(email: str):
    from deskbot_server.web.app import create_app

    client = create_app().test_client()
    client.post("/login", data={"email": email, "password": "password1234"})
    return client


def test_api_llm_config_info_endpoint(temp_db, monkeypatch, tmp_path):
    from tests._auth_compat import create_user

    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    create_user("llm-cfg-info@example.com", "password1234")
    client = _login_client("llm-cfg-info@example.com")

    resp = client.get("/api/robot-settings/llm/config-info?provider=ark")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["readonly"] is False

    resp = client.get("/api/robot-settings/llm/config-info?provider=qwen")
    assert resp.get_json()["readonly"] is True

    resp = client.get("/api/robot-settings/llm/config-info?provider=bogus")
    assert resp.status_code == 400


def test_api_llm_config_save_endpoint(temp_db, monkeypatch, tmp_path):
    from deskbot_server.config import load_config
    from tests._auth_compat import create_user

    # 隔离默认 config.yaml：API 端点走真实默认路径，monkeypatch 到临时文件
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(MINIMAL_LLM_CFG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr("deskbot_server.config.DEFAULT_CONFIG_PATH", str(cfg_file))

    env_file = tmp_path / ".env"
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", env_file)
    create_user("llm-cfg-save@example.com", "password1234")
    client = _login_client("llm-cfg-save@example.com")

    resp = client.post(
        "/api/robot-settings/llm/config",
        json={"provider": "ark", "api_key": "ark-api-1", "model_name": "ep-api", "base_url": ""},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["model_name"] == "ep-api"
    assert "ARK_API_KEY=ark-api-1" in env_file.read_text(encoding="utf-8")
    llm = load_config(None)["llm"]
    assert llm["model_name"] == "ep-api"  # 当前协议 ark → 主键
    assert llm["protocol"] == "ark_responses"


def test_api_llm_test_endpoint(temp_db, monkeypatch, tmp_path):
    from tests._auth_compat import create_user

    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    create_user("llm-test@example.com", "password1234")
    client = _login_client("llm-test@example.com")

    async def fake(messages, **kwargs):
        return "试聊成功", {"model": "m", "usage": None}

    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", fake)
    resp = client.post("/api/robot-settings/llm/test", json={"provider": "qwen", "text": "你好"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["reply"] == "试聊成功"
    assert payload["model"] == "m"

    # 未知 provider → 400
    resp = client.post("/api/robot-settings/llm/test", json={"provider": "bogus", "text": "hi"})
    assert resp.status_code == 400
