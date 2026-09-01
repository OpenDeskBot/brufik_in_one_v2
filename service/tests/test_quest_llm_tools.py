"""任务工具（update_task_result / update_task_strategy / contribute_score）
经 LLM tool runner（execute_llm_tools）执行测试。

playbook 不显式传入：由设备绑定（devices.quest_id）解析。
"""

from __future__ import annotations

import asyncio
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
    """与 test_quest_service 同款三段剧本：起点问候 →(成功 10 分)→ 了解姓名。"""
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


def _run_tools(tools: list[dict], device_id: str) -> list[dict]:
    from deskbot_server.service.application.llm_tool_runner import execute_llm_tools

    return asyncio.run(execute_llm_tools(tools, device_id=device_id))


# ── 成功路径 ──────────────────────────────────────────────────


def test_update_task_result_via_tool(env):
    _setup_bound(env)
    results = _run_tools(
        [{"tool": "update_task_result", "task_id": "g_greet", "status": "success", "result": "用户回应了问候"}],
        "dev1",
    )
    assert results[0]["ok"] is True
    assert results[0]["task"]["status"] == "success"
    # 沿成功口传播：g_learn_name +10 分 → 达标自动激活
    assert results[0]["propagated"][0]["task_id"] == "g_learn_name"
    assert results[0]["activated"][0]["task_id"] == "g_learn_name"


def test_update_task_strategy_via_tool(env):
    svc = _setup_bound(env)
    results = _run_tools(
        [{"tool": "update_task_strategy", "task_id": "g_greet", "strategy": "问候要短"}], "dev1"
    )
    assert results[0]["ok"] is True
    assert svc.get_effective_strategy("dev1", "demo", "g_greet") == "问候要短"


def test_contribute_score_via_tool(env):
    _setup_bound(env)
    r = _run_tools([{"tool": "contribute_score", "task_id": "g_learn_name", "points": 8}], "dev1")
    assert r[0]["ok"] is True and r[0]["current_score"] == 8
    assert r[0]["status"] == "not_started"
    r = _run_tools([{"tool": "contribute_score", "task_id": "g_learn_name", "points": 2}], "dev1")
    assert r[0]["ok"] is True and r[0]["status"] == "running" and r[0]["activated"] is True


# ── 错误路径 ──────────────────────────────────────────────────


def test_quest_tools_unbound_error(env):
    _bind_device("dev1", None)
    results = _run_tools(
        [{"tool": "update_task_result", "task_id": "g_greet", "status": "success", "result": "x"}], "dev1"
    )
    assert results[0]["ok"] is False
    assert "未绑定" in results[0]["error"]


def test_quest_tools_missing_playbook_error(env):
    _bind_device("dev1", "ghost")
    results = _run_tools([{"tool": "contribute_score", "task_id": "g_greet", "points": 1}], "dev1")
    assert results[0]["ok"] is False
    assert "未绑定" in results[0]["error"]


def test_quest_tools_bad_params(env):
    _setup_bound(env)
    # 缺 task_id
    r = _run_tools([{"tool": "update_task_result", "status": "success", "result": "x"}], "dev1")
    assert r[0]["ok"] is False and "task_id" in r[0]["error"]
    # 非法 status
    r = _run_tools(
        [{"tool": "update_task_result", "task_id": "g_greet", "status": "done", "result": "x"}], "dev1"
    )
    assert r[0]["ok"] is False and "status" in r[0]["error"]
    # 未激活任务不能判定
    r = _run_tools(
        [{"tool": "update_task_result", "task_id": "g_learn_name", "status": "success", "result": "x"}], "dev1"
    )
    assert r[0]["ok"] is False and "未激活" in r[0]["error"]
    # 缺结果描述
    r = _run_tools(
        [{"tool": "update_task_result", "task_id": "g_greet", "status": "success", "result": ""}], "dev1"
    )
    assert r[0]["ok"] is False and "结果" in r[0]["error"]
