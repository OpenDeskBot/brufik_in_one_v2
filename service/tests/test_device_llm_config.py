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

    # 无设备 → 系统默认，context_window=None
    cfg = resolve_llm_config("dev_no_such")
    assert cfg.context_window is None

    _bind_device()
    # 设备存在但 llm_param 为空 → None
    assert resolve_llm_config("dev_llm_cfg").context_window is None

    update_llm_param("dev_llm_cfg", '{"context_window": 16384}')
    assert resolve_llm_config("dev_llm_cfg").context_window == 16384

    # 非法值 → None（不抛）
    update_llm_param("dev_llm_cfg", '{"context_window": "abc"}')
    assert resolve_llm_config("dev_llm_cfg").context_window is None
    update_llm_param("dev_llm_cfg", '{"context_window": -5}')
    assert resolve_llm_config("dev_llm_cfg").context_window is None


def test_history_token_budget_follows_context_window(temp_db):
    from deskbot_server.dao.device_mapper import update_llm_param
    from deskbot_server.service.application.chat_flow import _history_token_budget

    # 无设备 → 回退 8192 的一半
    assert _history_token_budget(None) == 4096

    _bind_device()
    # 默认无 param → 4096
    assert _history_token_budget("dev_llm_cfg") == 4096

    update_llm_param("dev_llm_cfg", '{"context_window": 16384}')
    assert _history_token_budget("dev_llm_cfg") == 8192
