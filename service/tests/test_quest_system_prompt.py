"""剧情任务注入 system prompt 测试：llm_quest_tasks/tools_prompt_appendix
与 build_llm_system_prompt 的组装。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def env(monkeypatch):
    """临时剧本目录 + 临时数据库。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        monkeypatch.setenv("DESKBOT_DB_PATH", str(tmp / "test.db"))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine
        from deskbot_server.service import quest_service
        from deskbot_server.utils.singleton import SingletonMeta

        quest_service.configure_playbooks_dir(tmp / "playbooks")
        reset_engine()
        init_engine(tmp / "test.db")
        init_database()
        SingletonMeta.reset_instance(quest_service.QuestService)
        yield tmp
        reset_engine()


def _bind_device(device_id: str, quest_id: str | None = "demo") -> None:
    from deskbot_server.dao.device_mapper import insert as insert_device, update_quest_id
    from deskbot_server.db.models import _new_id
    from deskbot_server.service.user_service import UserService

    user = UserService().register(f"quest-{device_id}@example.com", "password1234")
    insert_device(_new_id(), device_id, user.id, device_id)
    if quest_id:
        update_quest_id(device_id, quest_id)


def _demo_playbook(name: str = "demo") -> dict:
    return {
        "name": name,
        "tasks": [
            {
                "id": "g_greet",
                "goal": "主动向用户问好",
                "strategy": "轻松自然地打招呼",
                "activation_score": 100,
                "initial_status": "running",
                "success_condition": "用户回应了问候",
                "failure_condition": "用户没有回应",
                "on_success": [{"id": "g_learn_name", "score": 10}],
                "on_failure": [],
                "score_sources": {"conversation": True, "time": "08:00"},
                "pos": {"x": 120, "y": 120, "width": 200, "height": 96},
            },
            {
                "id": "g_learn_name",
                "goal": "知道用户的名字",
                "strategy": "自然地问，记住并下次称呼",
                "activation_score": 10,
                "initial_status": "not_started",
                "success_condition": "用户明确告诉了我他的名字",
                "failure_condition": "用户表示不想说",
                "on_success": [],
                "on_failure": [{"id": "g_learn_name_soften", "score": 6}],
                "score_sources": {"conversation": True, "time": None},
                "pos": {"x": 460, "y": 120, "width": 200, "height": 96},
            },
            {
                "id": "g_learn_name_soften",
                "goal": "换个角度接近用户",
                "strategy": "不再问名字，聊用户感兴趣的日常",
                "activation_score": 6,
                "initial_status": "not_started",
                "success_condition": "用户愿意闲聊了",
                "failure_condition": "用户始终冷淡",
                "on_success": [],
                "on_failure": [],
                "score_sources": {"conversation": True, "time": None},
                "pos": {"x": 460, "y": 380, "width": 200, "height": 96},
            },
        ],
    }


def _setup_bound(env, device_id: str = "dev1", quest_id: str | None = "demo"):
    from deskbot_server.service.quest_service import QuestService

    svc = QuestService()
    svc.save_playbook("demo", _demo_playbook())
    _bind_device(device_id, quest_id)
    svc.ensure_instances(device_id, "demo")
    return svc


# ── appendix 空值分支 ─────────────────────────────────────────


def test_appendix_unbound_empty(env):
    from deskbot_server.infrastructure.llm.utils import (
        llm_quest_tasks_prompt_appendix,
        llm_quest_tools_prompt_appendix,
    )

    assert llm_quest_tasks_prompt_appendix(device_id=None) == ""
    assert llm_quest_tools_prompt_appendix(device_id=None) == ""
    _bind_device("dev1", None)
    assert llm_quest_tasks_prompt_appendix(device_id="dev1") == ""
    assert llm_quest_tools_prompt_appendix(device_id="dev1") == ""
    # 无 devices 行
    assert llm_quest_tasks_prompt_appendix(device_id="nobody") == ""


# ── 内容断言 ──────────────────────────────────────────────────


def test_tasks_appendix_contains_running(env):
    from deskbot_server.infrastructure.llm.utils import llm_quest_tasks_prompt_appendix

    _setup_bound(env)
    ax = llm_quest_tasks_prompt_appendix(device_id="dev1")
    assert "当前剧情任务" in ax
    assert "g_greet" in ax and "主动向用户问好" in ax
    assert "0/100" in ax  # 进度 current_score/activation_score


def test_tools_appendix_contains_contracts(env):
    from deskbot_server.infrastructure.llm.utils import llm_quest_tools_prompt_appendix

    svc = _setup_bound(env)
    ax = llm_quest_tools_prompt_appendix(device_id="dev1")
    assert "update_task_result" in ax
    assert "update_task_strategy" in ax
    assert "contribute_score" in ax
    assert "g_greet" in ax  # 可用任务 id
    # 全部置终态后 → 无可用任务 → 空串（不广告不可用工具）
    # 注：g_greet 成功会激活 g_learn_name（+10 达标），需连它也判定掉
    svc.update_task_result("dev1", "demo", "g_greet", "success", "用户回应了问候")
    svc.update_task_result("dev1", "demo", "g_learn_name", "success", "用户告诉了我名字")
    assert llm_quest_tools_prompt_appendix(device_id="dev1") == ""


def test_build_llm_system_prompt_injects_quest_sections(env):
    from deskbot_server.infrastructure.llm.utils import build_llm_system_prompt

    _setup_bound(env)
    sp = build_llm_system_prompt("你是助手", device_id="dev1")
    assert "当前剧情任务" in sp
    assert "update_task_result" in sp
    # 未绑定设备 → 不注入（基础内容不受影响）
    _bind_device("dev2", None)
    sp2 = build_llm_system_prompt("你是助手", device_id="dev2")
    assert "当前剧情任务" not in sp2
    assert sp2.startswith("你是助手")
