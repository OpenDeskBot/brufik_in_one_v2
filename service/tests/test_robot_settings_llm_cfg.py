"""机器人设置页 LLM 配置（config-info / config / test 试聊）测试。

覆盖：ark 配置信息（掩码 key，读该设备 llm_param["ark"]，不回退 config.yaml / .env）、
保存语义（仅写 device 表 llm_param["ark"]，不写 .env / config.yaml）、本地模型只读端点、
试聊临时 config（表单覆盖 > 设备 llm_param > 内置默认；本地免 Key）、三个 API 端点。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
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
def temp_db(monkeypatch):
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine
    from deskbot_server.service.user_service import UserService
    from deskbot_server.utils.singleton import SingletonMeta

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        reset_engine()
        init_engine(db_path)
        init_database()
        SingletonMeta.reset_instance(UserService)
        yield db_path
        reset_engine()
        SingletonMeta.reset_instance(UserService)


@pytest.fixture()
def device(temp_db):
    """绑定一台测试设备（llm-cfg@example.com / deskbot_llm）。"""
    from tests.device_bind_helpers import bind_device_online
    from deskbot_server.service.user_service import UserService

    user = UserService().register("llm-cfg@example.com", "password1234")
    return bind_device_online(user.id, "deskbot_llm")


def _llm_svc(tmp_path: Path, cfg: dict | None = None) -> RobotCapabilityService:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg or MINIMAL_LLM_CFG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return RobotCapabilityService(config_path=p)


# ---------- config-info ----------


def test_llm_config_info_ark_empty_without_device_param(tmp_path):
    svc = _llm_svc(tmp_path)
    info = svc.llm_config_info("ark", "deskbot_nope")  # 设备不存在 → 空值（不回退 config.yaml）
    assert info["provider"] == "ark"
    assert info["readonly"] is False
    assert info["api_key"] == "" and info["api_key_set"] is False
    assert info["model_name"] == ""
    assert info["base_url"] == ""


def test_llm_config_info_ark_masks_device_param(tmp_path, device):
    from deskbot_server.dao.device_mapper import update_llm_param
    from deskbot_server.infrastructure.asr.doubao import _mask_secret

    update_llm_param(
        "deskbot_llm",
        json.dumps({"ark": {"api_key": "ark-secret-key-123", "model_name": "ep-dev", "base_url": ""}}),
    )
    svc = _llm_svc(tmp_path)
    info = svc.llm_config_info("ark", "deskbot_llm")
    assert info["api_key"] == _mask_secret("ark-secret-key-123")
    assert info["api_key_set"] is True
    assert info["model_name"] == "ep-dev"
    assert info["base_url"] == ""


def test_llm_config_info_local_readonly_no_device_needed(tmp_path):
    svc = _llm_svc(tmp_path)
    info = svc.llm_config_info("minicpm")
    assert info["readonly"] is True
    assert info["base_url"] == "http://127.0.0.1:9105/v1"
    assert info["model_name"] == "minicpm5-1b"
    info = svc.llm_config_info("qwen", None)
    assert info["readonly"] is True
    assert info["base_url"] == "http://127.0.0.1:9106/v1"
    assert info["model_name"] == "qwen3.8-2b"


def test_llm_config_info_unknown_provider_rejected(tmp_path):
    svc = _llm_svc(tmp_path)
    with pytest.raises(CapabilityError):
        svc.llm_config_info("bogus", "deskbot_llm")


# ---------- save_device_llm_config ----------


def test_save_llm_config_writes_device_llm_param_only(tmp_path, device):
    from deskbot_server.config import load_config
    from deskbot_server.dao.device_mapper import get_llm_param

    svc = _llm_svc(tmp_path)
    before = load_config(svc._config_path)

    info = svc.save_device_llm_config(
        "deskbot_llm",
        "ark",
        {"api_key": "ark-new-key", "model_name": "ep-2", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    )

    param = get_llm_param("deskbot_llm")
    assert param["ark"] == {
        "api_key": "ark-new-key",
        "model_name": "ep-2",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    }
    assert load_config(svc._config_path) == before  # config.yaml 不被改写
    # config-info 回填：掩码 + 新值
    assert info["model_name"] == "ep-2"
    assert info["api_key_set"] is True


def test_save_llm_config_masked_key_keeps_existing_and_preserves_other_keys(tmp_path, device):
    from deskbot_server.dao.device_mapper import get_llm_param, update_llm_param
    from deskbot_server.infrastructure.asr.doubao import _mask_secret

    update_llm_param(
        "deskbot_llm",
        json.dumps({"context_window": 8192, "ark": {"api_key": "ark-real-key", "model_name": "ep-1"}}),
    )
    svc = _llm_svc(tmp_path)

    svc.save_device_llm_config(
        "deskbot_llm", "ark",
        {"api_key": _mask_secret("ark-real-key"), "model_name": "ep-2", "base_url": ""},
    )

    param = get_llm_param("deskbot_llm")
    assert param["ark"]["api_key"] == "ark-real-key"  # 掩码不覆盖
    assert param["ark"]["model_name"] == "ep-2"  # 非掩码空字段回填已有后才落新值
    assert param["context_window"] == 8192  # 顶层键（context_window 等）原样保留


def test_save_llm_config_empty_payload_keeps_existing(tmp_path, device):
    """空值按回填链保留已有（payload > 设备 llm_param）；清除走 clear_device_llm_override。"""
    from deskbot_server.dao.device_mapper import get_llm_param, update_llm_param

    update_llm_param("deskbot_llm", json.dumps({"ark": {"api_key": "sk-1", "model_name": "ep-1"}}))
    svc = _llm_svc(tmp_path)

    svc.save_device_llm_config("deskbot_llm", "ark", {"api_key": "", "model_name": "", "base_url": ""})

    assert get_llm_param("deskbot_llm") == {"ark": {"api_key": "sk-1", "model_name": "ep-1"}}  # 空值保留已有


def test_save_llm_config_empty_payload_without_existing_noop(tmp_path, device):
    from deskbot_server.dao.device_mapper import get_llm_param

    svc = _llm_svc(tmp_path)
    svc.save_device_llm_config("deskbot_llm", "ark", {"api_key": "", "model_name": "", "base_url": ""})
    assert get_llm_param("deskbot_llm") == {}  # 无已有值全空 → 不落键（保持 NULL）


def test_save_llm_config_local_provider_rejected(tmp_path, device):
    svc = _llm_svc(tmp_path)
    with pytest.raises(CapabilityError, match="本地模型无可保存字段"):
        svc.save_device_llm_config("deskbot_llm", "minicpm", {"api_key": "x"})


def test_save_llm_config_requires_device(tmp_path):
    svc = _llm_svc(tmp_path)
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.save_device_llm_config(None, "ark", {"api_key": "x"})


# ---------- llm_test（试聊） ----------


def _fake_chat(captured: dict):
    async def fake(messages, **kwargs):
        captured["messages"] = messages
        captured["cfg"] = kwargs.get("config")
        return "收到，我在。", {"model": "test-model", "usage": {"total_tokens": 9}}

    return fake


def test_llm_test_ark_overrides_beat_device_param(tmp_path, device, monkeypatch):
    from deskbot_server.dao.device_mapper import update_llm_param

    update_llm_param(
        "deskbot_llm",
        json.dumps({"ark": {"api_key": "dev-key", "model_name": "ep-dev", "base_url": ""}}),
    )
    captured: dict = {}
    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", _fake_chat(captured))
    svc = _llm_svc(tmp_path)
    result = asyncio.run(
        svc.llm_test(
            "ark", "你好", device_id="deskbot_llm",
            overrides={"api_key": "form-key", "model_name": "ep-form", "base_url": "https://ark.example/api/v3"},
        )
    )
    assert result["ok"] is True
    cfg = captured["cfg"]
    assert cfg.api_key == "form-key"  # 表单覆盖 > 设备 llm_param
    assert cfg.model == "ep-form"
    assert cfg.protocol == "ark_responses"
    assert captured["messages"][0]["content"] == "你好"


def test_llm_test_ark_falls_back_to_device_param(tmp_path, device, monkeypatch):
    from deskbot_server.dao.device_mapper import update_llm_param

    update_llm_param(
        "deskbot_llm",
        json.dumps({"ark": {"api_key": "dev-key", "model_name": "ep-dev", "base_url": ""}}),
    )
    captured: dict = {}
    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", _fake_chat(captured))
    svc = _llm_svc(tmp_path)
    result = asyncio.run(svc.llm_test("ark", "", device_id="deskbot_llm"))
    assert result["ok"] is True
    assert captured["messages"][0]["content"] == "你好，请用一句话简短回复。"  # 空文本 → 默认
    cfg = captured["cfg"]
    assert cfg.api_key == "dev-key"  # 回填设备 llm_param
    assert cfg.model == "ep-dev"
    assert cfg.api_base == "https://ark.cn-beijing.volces.com/api/v3"  # 空 base_url → 内置默认


def test_llm_test_ark_masked_override_keeps_device_key(tmp_path, device, monkeypatch):
    from deskbot_server.dao.device_mapper import update_llm_param
    from deskbot_server.infrastructure.asr.doubao import _mask_secret

    update_llm_param("deskbot_llm", json.dumps({"ark": {"api_key": "dev-key", "model_name": "ep-dev"}}))
    captured: dict = {}
    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", _fake_chat(captured))
    svc = _llm_svc(tmp_path)
    result = asyncio.run(
        svc.llm_test("ark", "你好", device_id="deskbot_llm",
                     overrides={"api_key": _mask_secret("dev-key"), "model_name": ""})
    )
    assert result["ok"] is True
    assert captured["cfg"].api_key == "dev-key"  # 掩码占位 → 回落设备值


def test_llm_test_ark_requires_device(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", _fake_chat(captured))
    svc = _llm_svc(tmp_path)
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        asyncio.run(svc.llm_test("ark", "你好"))


def test_llm_test_ark_missing_model_clear_error(tmp_path, device):
    svc = _llm_svc(tmp_path)
    with pytest.raises(CapabilityError, match="模型 ID"):
        asyncio.run(svc.llm_test("ark", "你好", device_id="deskbot_llm"))


def test_llm_test_local_no_api_key_needed(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", _fake_chat(captured))
    svc = _llm_svc(tmp_path)
    result = asyncio.run(svc.llm_test("minicpm", "你好"))
    assert result["ok"] is True
    cfg = captured["cfg"]
    assert cfg.api_key == ""  # 本地免 Key（is_local_llm_url 豁免）
    assert cfg.api_base == "http://127.0.0.1:9105/v1"
    assert cfg.model == "minicpm5-1b"


def test_llm_test_failure_returns_ok_false(tmp_path, monkeypatch):
    async def fail(messages, **kwargs):
        raise ConnectionError("无法连接")

    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", fail)
    svc = _llm_svc(tmp_path)
    result = asyncio.run(svc.llm_test("qwen", "你好"))
    assert result["ok"] is False
    assert "无法连接" in result["error"]


def test_llm_test_unknown_provider_rejected(tmp_path):
    svc = _llm_svc(tmp_path)
    with pytest.raises(CapabilityError):
        asyncio.run(svc.llm_test("bogus", "你好"))


# ---------- API 端点 ----------


def _login_select_device(device_id: str):
    """登录并选择当前设备，返回 test client。"""
    from deskbot_server.web.app import create_app

    client = create_app().test_client()
    client.post("/login", data={"email": "llm-cfg@example.com", "password": "password1234"})
    resp = client.post("/app/api/devices/select", json={"device_id": device_id})
    assert resp.status_code == 200
    return client


def test_api_llm_config_info_endpoint(temp_db, device):
    client = _login_select_device("deskbot_llm")

    resp = client.get("/api/robot-settings/llm/config-info?provider=ark")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["readonly"] is False
    assert payload["api_key_set"] is False  # 未配置设备 llm_param → 空（不回退 config）

    resp = client.get("/api/robot-settings/llm/config-info?provider=qwen")
    assert resp.get_json()["readonly"] is True

    resp = client.get("/api/robot-settings/llm/config-info?provider=bogus")
    assert resp.status_code == 400


def test_api_llm_config_save_endpoint_writes_device_llm_param(temp_db, device):
    from deskbot_server.dao.device_mapper import get_llm_param

    client = _login_select_device("deskbot_llm")

    resp = client.post(
        "/api/robot-settings/llm/config",
        json={"provider": "ark", "api_key": "ark-api-1", "model_name": "ep-api", "base_url": ""},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["model_name"] == "ep-api"
    assert get_llm_param("deskbot_llm")["ark"] == {"api_key": "ark-api-1", "model_name": "ep-api"}


def test_api_llm_config_save_requires_current_device(temp_db, device):
    from deskbot_server.web.app import create_app

    client = create_app().test_client()
    client.post("/login", data={"email": "llm-cfg@example.com", "password": "password1234"})

    resp = client.post(
        "/api/robot-settings/llm/config",
        json={"provider": "ark", "api_key": "x", "model_name": "ep-x"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_api_llm_test_endpoint_local_and_ark(temp_db, device, monkeypatch):
    from deskbot_server.dao.device_mapper import update_llm_param

    async def fake(messages, **kwargs):
        return "试聊成功", {"model": "m", "usage": None}

    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", fake)
    update_llm_param("deskbot_llm", json.dumps({"ark": {"api_key": "sk-dev", "model_name": "ep-dev"}}))
    client = _login_select_device("deskbot_llm")

    # 本地引擎：免 key、不依赖设备参数
    resp = client.post("/api/robot-settings/llm/test", json={"provider": "qwen", "text": "你好"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    # ark：回落设备 llm_param（表单空覆盖不丢设备值）
    resp = client.post("/api/robot-settings/llm/test", json={"provider": "ark", "text": ""})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["reply"] == "试聊成功"

    # 未知 provider → 400
    resp = client.post("/api/robot-settings/llm/test", json={"provider": "bogus", "text": "hi"})
    assert resp.status_code == 400


def test_api_llm_test_ark_requires_current_device(temp_db, device, monkeypatch):
    from deskbot_server.web.app import create_app

    async def fake(messages, **kwargs):
        return "试聊成功", {"model": "m", "usage": None}

    monkeypatch.setattr("deskbot_server.service.robot_capability.chat_acompletion", fake)
    client = create_app().test_client()
    client.post("/login", data={"email": "llm-cfg@example.com", "password": "password1234"})

    resp = client.post("/api/robot-settings/llm/test", json={"provider": "ark", "text": "你好"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
