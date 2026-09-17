"""devices.llm_provider / llm_param 列 + context_window 链路测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine
        from deskbot_server.service.user_service import UserService
        from deskbot_server.utils.singleton import SingletonMeta

        reset_engine()
        init_engine(db_path)
        init_database()
        SingletonMeta.reset_instance(UserService)
        yield db_path
        reset_engine()
        SingletonMeta.reset_instance(UserService)


def _bind_device(device_id: str = "dev_llm_cfg") -> None:
    from deskbot_server.dao.device_mapper import insert as insert_device
    from deskbot_server.db.models import _new_id
    from deskbot_server.service.user_service import UserService

    user = UserService().register(f"llmcfg-{device_id}@example.com", "password1234")
    insert_device(_new_id(), device_id, user.id, device_id)


def test_columns_exist_and_defaults(temp_db):
    from sqlalchemy import inspect

    from deskbot_server.db.engine import get_session

    bind = get_session().get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("devices")}
    assert {"llm_provider", "llm_param"} <= cols


def test_mapper_provider_param_roundtrip(temp_db):
    from deskbot_server.dao.device_mapper import get_llm_param, get_llm_provider, update_llm_param, update_llm_provider

    _bind_device()
    assert get_llm_provider("dev_llm_cfg") == ""
    assert get_llm_param("dev_llm_cfg") == {}

    update_llm_provider("dev_llm_cfg", "qwen")
    assert get_llm_provider("dev_llm_cfg") == "qwen"

    update_llm_param("dev_llm_cfg", '{"context_window": 16384, "x": 1}')
    param = get_llm_param("dev_llm_cfg")
    assert param == {"context_window": 16384, "x": 1}

    # 坏 JSON → {}
    update_llm_param("dev_llm_cfg", "{broken")
    assert get_llm_param("dev_llm_cfg") == {}
    # 清除
    update_llm_param("dev_llm_cfg", None)
    assert get_llm_param("dev_llm_cfg") == {}


def test_resolve_llm_config_context_window(temp_db):
    from deskbot_server.dao.device_mapper import update_llm_param
    from deskbot_server.infrastructure.llm.runtime import resolve_llm_config

    # 前半段用显式 cfg 隔离真实 config.yaml，只验证「设备覆盖 / 非法回落」语义
    no_win = {"llm": {"protocol": "openai", "model_name": "m"}}

    # 无设备 → 系统默认；config 未声明窗口 → None
    assert resolve_llm_config("dev_no_such", no_win).context_window is None

    _bind_device()
    # 设备存在但 llm_param 为空 → 沿用系统默认（此处未声明 → None）
    assert resolve_llm_config("dev_llm_cfg", no_win).context_window is None

    update_llm_param("dev_llm_cfg", '{"context_window": 16384}')
    assert resolve_llm_config("dev_llm_cfg", no_win).context_window == 16384

    # 非法值 → 不抛，回落系统默认
    update_llm_param("dev_llm_cfg", '{"context_window": "abc"}')
    assert resolve_llm_config("dev_llm_cfg", no_win).context_window is None
    update_llm_param("dev_llm_cfg", '{"context_window": -5}')
    assert resolve_llm_config("dev_llm_cfg", no_win).context_window is None

    # 接线验证（真实 config.yaml）：系统默认声明的窗口传导到未配置的设备。
    # 从 config 读取期望值而非硬编码，避免以后调窗口时这条测试变成假失败。
    from deskbot_server.config import load_config

    update_llm_param("dev_llm_cfg", None)
    declared = (load_config().get("llm") or {}).get("context_window")
    assert resolve_llm_config("dev_llm_cfg").context_window == declared


def test_history_token_budget_follows_context_window(temp_db):
    from deskbot_server.config import load_config
    from deskbot_server.dao.device_mapper import update_llm_param
    from deskbot_server.service.application.chat_flow import _history_token_budget

    # 无设备 → 回退 8192 的一半
    assert _history_token_budget(None) == 4096

    _bind_device()
    # 设备未配置 param → 用系统默认（config.yaml llm.context_window）的一半
    declared = int((load_config().get("llm") or {}).get("context_window") or 0)
    assert _history_token_budget("dev_llm_cfg") == (declared // 2 if declared else 4096)

    # 设备 llm_param 覆盖系统默认
    update_llm_param("dev_llm_cfg", '{"context_window": 16384}')
    assert _history_token_budget("dev_llm_cfg") == 8192


def test_resolve_llm_config_device_provider_branches(temp_db, monkeypatch):
    """设备级真源：llm_provider 白名单生效，llm_param["ark"] 承载云端密钥/模型，非法值回落系统默认。"""
    from deskbot_server.dao.device_mapper import update_llm_param, update_llm_provider
    from deskbot_server.infrastructure.llm.runtime import (
        ARK_OPENAI_BASE_URL,
        QWEN_LLM_BASE_URL,
        QWEN_LLM_MODEL,
        SILICONFLOW_BASE_URL,
        SILICONFLOW_DEFAULT_MODEL,
        resolve_llm_config,
    )

    _bind_device()

    # 本地 qwen：固定端点、免 key、source=device
    update_llm_provider("dev_llm_cfg", "qwen")
    cfg = resolve_llm_config("dev_llm_cfg")
    assert cfg.protocol == "openai"
    assert cfg.api_base == QWEN_LLM_BASE_URL
    assert cfg.model == QWEN_LLM_MODEL
    assert cfg.api_key == ""
    assert cfg.source == "device"

    # ark：llm_param["ark"] 生效（key / model）；base_url 缺省回落内置默认
    update_llm_provider("dev_llm_cfg", "ark")
    update_llm_param("dev_llm_cfg", '{"ark": {"api_key": "sk-test", "model_name": "ep-1"}}')
    cfg = resolve_llm_config("dev_llm_cfg")
    assert cfg.protocol == "ark_responses"
    assert cfg.api_key == "sk-test"
    assert cfg.model == "ep-1"
    assert cfg.api_base == ARK_OPENAI_BASE_URL
    assert cfg.source == "device"

    # ark 缺 model_name → ValueError（不回落 config.yaml，避免模型 ID 串位）
    update_llm_param("dev_llm_cfg", '{"ark": {"api_key": "sk-test"}}')
    with pytest.raises(ValueError):
        resolve_llm_config("dev_llm_cfg")

    # siliconflow：模型存 llm_param["siliconflow"]，密钥走服务端 .env（设备表里没有 key）
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf-env")
    update_llm_provider("dev_llm_cfg", "siliconflow")
    update_llm_param("dev_llm_cfg", '{"siliconflow": {"model_name": "Qwen/Qwen3-8B"}}')
    cfg = resolve_llm_config("dev_llm_cfg")
    assert cfg.protocol == "openai"
    assert cfg.api_base == SILICONFLOW_BASE_URL
    assert cfg.model == "Qwen/Qwen3-8B"  # org 前缀保留
    assert cfg.api_key == "sk-sf-env"  # 密钥来自 env 而非设备表
    assert cfg.source == "device"
    assert cfg.extra_body == {"enable_thinking": False}

    # 缺模型不抛（app_bp 的 PUT /api/devices/{id}/llm 不校验白名单）→ 回落预设默认
    update_llm_param("dev_llm_cfg", "{}")
    cfg = resolve_llm_config("dev_llm_cfg")
    assert cfg.model == SILICONFLOW_DEFAULT_MODEL
    assert cfg.extra_body == {"enable_thinking": False}

    # R1 蒸馏款不支持开关思考 → 不下发 enable_thinking
    update_llm_param("dev_llm_cfg", '{"siliconflow": {"model_name": "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"}}')
    assert resolve_llm_config("dev_llm_cfg").extra_body is None

    # 非法 / 空 provider（app_bp PUT 可写任意串）→ 白名单校验回落系统默认
    update_llm_provider("dev_llm_cfg", "openai")
    assert resolve_llm_config("dev_llm_cfg").source == "system"
    update_llm_provider("dev_llm_cfg", "")
    assert resolve_llm_config("dev_llm_cfg").source == "system"
