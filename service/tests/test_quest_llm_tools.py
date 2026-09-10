"""任务工具 complete_task 经 LLM tool runner（execute_llm_tools）执行测试。

playbook 不显式传入：由设备绑定（devices.quest_id）解析。
"""

from __future__ import annotations

import asyncio
import datetime as dt
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


def _redirect_data_dir(monkeypatch, base: Path) -> None:
    from deskbot_server.utils import device_data as dd

    d = base / "data"
    d.mkdir()
    monkeypatch.setattr(dd, "DATA_DIR", d)


def _bind_device(device_id: str, quest_id: str | None = "demo") -> None:
    from deskbot_server.dao.device_mapper import insert as insert_device, update_quest_id
    from deskbot_server.db.models import _new_id
    from deskbot_server.service.user_service import UserService

    user = UserService().register(f"quest-{device_id}@example.com", "password1234")
    insert_device(_new_id(), device_id, user.id, device_id)
    if quest_id:
        update_quest_id(device_id, quest_id)


def _demo_playbook(name: str = "demo") -> dict:
    """三段剧本：问候(once 入口)→了解姓名(once)；日常喝水(daily 入口)；爱好(long_term 入口)。"""
    return {
        "name": name,
        "tasks": [
            {
                "id": "g_greet",
                "type": "once",
                "prompt": "主动向用户问好",
                "next_task_ids": ["g_learn_name"],
                "pos": {"x": 120, "y": 120, "width": 200, "height": 96},
            },
            {
                "id": "g_learn_name",
                "type": "once",
                "prompt": "知道用户的名字",
                "next_task_ids": [],
                "pos": {"x": 460, "y": 120, "width": 200, "height": 96},
            },
            {
                "id": "g_daily",
                "type": "daily",
                "prompt": "饭点问用户吃饭了没",
                "next_task_ids": [],
                "pos": {"x": 120, "y": 300, "width": 200, "height": 96},
            },
            {
                "id": "g_habit",
                "type": "long_term",
                "prompt": "收集用户兴趣爱好",
                "next_task_ids": [],
                "pos": {"x": 460, "y": 300, "width": 200, "height": 96},
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


def test_complete_once_via_tool(env):
    _setup_bound(env)
    results = _run_tools(
        [{"tool": "complete_task", "task_id": "g_greet", "reason": "小明回应了问候"}], "dev1"
    )
    assert results[0]["ok"] is True
    assert results[0]["type"] == "once" and results[0]["status"] == "completed"
    assert results[0]["task"]["result"] == "小明回应了问候"
    # 后继 g_learn_name 被激活
    assert [x["task_id"] for x in results[0]["activated"]] == ["g_learn_name"]


def test_complete_daily_via_tool(env, monkeypatch):
    """日常任务经 runner：user/reason 键容错；写入该用户今日 done_list。"""
    import deskbot_server.dao.user_social_store as store

    _redirect_data_dir(monkeypatch, env)
    _setup_bound(env)
    monkeypatch.setattr(store, "_beijing_now", lambda: dt.datetime(2026, 9, 3, 12, 0, 0))
    # 模型常见键：user_name + result 也认
    r = _run_tools(
        [{"tool": "complete_task", "task_id": "g_daily", "user_name": "小明", "result": "小明中午吃了饺子"}], "dev1"
    )
    assert r[0]["ok"] is True and r[0]["type"] == "daily"
    assert r[0]["user"] == "小明" and r[0]["written"] is True and r[0]["deduped"] is False
    p = env / "data" / "dev1" / "done_list_小明_20260903.txt"
    assert p.is_file() and "[g_daily]" in p.read_text(encoding="utf-8")
    # 再调（键 user + text 变体）→ 服务端 deduped，不重复写
    r2 = _run_tools(
        [{"tool": "complete_task", "task_id": "g_daily", "user": "小明", "text": "又确认了一次"}], "dev1"
    )
    assert r2[0]["ok"] is True and r2[0]["deduped"] is True and r2[0]["written"] is False
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1


def test_complete_long_term_via_tool(env, monkeypatch):
    import deskbot_server.dao.user_social_store as store

    _redirect_data_dir(monkeypatch, env)
    _setup_bound(env)
    monkeypatch.setattr(store, "_beijing_now", lambda: dt.datetime(2026, 9, 3, 12, 0, 0))
    r = _run_tools(
        [{"tool": "complete_task", "task_id": "g_habit", "user": "小明", "reason": "小明喜欢乐高"}], "dev1"
    )
    assert r[0]["ok"] is True and r[0]["type"] == "long_term"
    assert r[0]["written"] is True
    p = env / "data" / "dev1" / "user_info_小明.txt"
    assert p.is_file() and "[g_habit]" in p.read_text(encoding="utf-8")


# ── 错误路径 ──────────────────────────────────────────────────


def test_quest_tools_unbound_error(env):
    _bind_device("dev1", None)
    results = _run_tools([{"tool": "complete_task", "task_id": "g_greet", "reason": "x"}], "dev1")
    assert results[0]["ok"] is False
    assert "未绑定" in results[0]["error"]


def test_quest_tools_missing_playbook_error(env):
    _bind_device("dev1", "ghost")
    results = _run_tools([{"tool": "complete_task", "task_id": "g_greet", "reason": "x"}], "dev1")
    assert results[0]["ok"] is False
    assert "未绑定" in results[0]["error"]


def test_quest_tools_bad_params(env):
    _setup_bound(env)
    # 缺 task_id
    r = _run_tools([{"tool": "complete_task", "reason": "x"}], "dev1")
    assert r[0]["ok"] is False and "task_id" in r[0]["error"]
    # 未激活任务不能完成
    r = _run_tools([{"tool": "complete_task", "task_id": "g_learn_name", "reason": "x"}], "dev1")
    assert r[0]["ok"] is False and "未激活" in r[0]["error"]
    # 缺完成原因
    r = _run_tools([{"tool": "complete_task", "task_id": "g_greet", "reason": ""}], "dev1")
    assert r[0]["ok"] is False and "原因" in r[0]["error"]
    # 日常任务缺 user → 服务端提示带 user 重试
    r = _run_tools([{"tool": "complete_task", "task_id": "g_daily", "reason": "x"}], "dev1")
    assert r[0]["ok"] is False and "user" in r[0]["error"]
    # 重复完成一次性任务
    _run_tools([{"tool": "complete_task", "task_id": "g_greet", "reason": "一次"}], "dev1")
    r = _run_tools([{"tool": "complete_task", "task_id": "g_greet", "reason": "again"}], "dev1")
    assert r[0]["ok"] is False and "不能重复完成" in r[0]["error"]
    # 未知工具名兜底
    r = _run_tools([{"tool": "update_task_result", "task_id": "g_greet", "reason": "x"}], "dev1")
    assert r[0]["ok"] is False and "未知工具" in r[0]["error"]
