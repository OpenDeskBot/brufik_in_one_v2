"""剧本任务引擎测试：剧本管理 / 三态状态机 / complete_task / 工具契约。

用临时目录当剧本文件目录、临时 sqlite 当实例库，不依赖真实数据。
"""

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


def _svc():
    from deskbot_server.service.quest_service import QuestService

    return QuestService()


def _bind_device(device_id: str, quest_id: str | None = "demo") -> None:
    """造 devices 行并绑定剧本（复用 register + mapper 直插，不依赖设备在线）。"""
    from deskbot_server.dao.device_mapper import insert as insert_device, update_quest_id
    from deskbot_server.db.models import _new_id
    from deskbot_server.service.user_service import UserService

    user = UserService().register(f"quest-{device_id}@example.com", "password1234")
    insert_device(_new_id(), device_id, user.id, device_id)
    if quest_id:
        update_quest_id(device_id, quest_id)


def _redirect_data_dir(monkeypatch, base: Path) -> None:
    """把 per-user 记录文件的 data 根目录指到 base（镜像 test_user_social_store.py）。"""
    from deskbot_server.utils import device_data as dd

    d = base / "data"
    d.mkdir()
    monkeypatch.setattr(dd, "DATA_DIR", d)


def _demo_playbook(name: str = "demo") -> dict:
    """四段剧本：问候(once 入口)→了解姓名(once)；日常(daily 入口)；爱好(long_term 入口)。"""
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
                "prompt": "知道用户的名字，自然地问，记住并下次称呼",
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
                "prompt": "收集用户至少3项兴趣爱好",
                "next_task_ids": [],
                "pos": {"x": 460, "y": 300, "width": 200, "height": 96},
            },
        ],
    }


# ── 剧本管理 ──────────────────────────────────────────────────


def test_playbook_create_list_get_delete(env):
    svc = _svc()
    assert svc.list_playbooks() == []
    pb = svc.create_playbook("demo")
    assert pb == {"name": "demo", "tasks": []}
    assert svc.list_playbooks() == ["demo"]
    svc.save_playbook("demo", _demo_playbook())
    got = svc.get_playbook("demo")
    assert len(got["tasks"]) == 4
    svc.delete_playbook("demo")
    assert svc.list_playbooks() == []
    assert svc.get_playbook("demo") is None


def test_playbook_create_invalid_name(env):
    from deskbot_server.service.quest_service import QuestError

    with pytest.raises(QuestError):
        _svc().create_playbook("bad name/../x")
    with pytest.raises(QuestError):
        _svc().create_playbook("Bad-UPPER")


def test_validate_duplicate_id(env):
    from deskbot_server.service.quest_service import QuestError

    pb = _demo_playbook()
    pb["tasks"][1]["id"] = "g_greet"  # 与第一个重复
    with pytest.raises(QuestError, match="重复"):
        _svc().save_playbook("demo", pb)


def test_validate_unknown_ref(env):
    from deskbot_server.service.quest_service import QuestError

    pb = _demo_playbook()
    pb["tasks"][0]["next_task_ids"] = ["ghost"]
    with pytest.raises(QuestError, match="不存在"):
        _svc().save_playbook("demo", pb)


def test_validate_cycle_and_self_ref(env):
    from deskbot_server.service.quest_service import QuestError

    pb = _demo_playbook()
    pb["tasks"][1]["next_task_ids"] = ["g_greet"]  # greet→learn_name→greet 成环
    with pytest.raises(QuestError, match="成环"):
        _svc().save_playbook("demo", pb)
    pb2 = _demo_playbook()
    pb2["tasks"][0]["next_task_ids"] = ["g_greet"]  # 自引用
    with pytest.raises(QuestError, match="自身"):
        _svc().save_playbook("demo", pb2)


def test_validate_type_and_prompt(env):
    from deskbot_server.service.quest_service import QuestError

    pb = _demo_playbook()
    pb["tasks"][0]["type"] = "weekly"
    with pytest.raises(QuestError, match="type"):
        _svc().save_playbook("demo", pb)
    pb2 = _demo_playbook()
    pb2["tasks"][0]["prompt"] = "   "
    with pytest.raises(QuestError, match="prompt"):
        _svc().save_playbook("demo", pb2)


def test_task_crud_and_edges(env):
    svc = _svc()
    svc.create_playbook("demo")
    t = svc.add_task("demo", {"id": "g_a", "prompt": "A"})
    assert t["type"] == "once"  # 默认类型
    assert t["prompt"] == "A"
    assert t["next_task_ids"] == []
    assert t["pos"]["x"] == 120  # 默认摆放
    svc.add_task("demo", {"id": "g_b", "prompt": "B", "type": "daily"})
    edge = svc.set_edge("demo", "g_a", "g_b")
    assert edge == {"from": "g_a", "to": "g_b"}
    svc.set_edge("demo", "g_a", "g_b")  # 重复连 = 幂等 no-op
    pb = svc.get_playbook("demo")
    assert pb["tasks"][0]["next_task_ids"] == ["g_b"]
    # 删除 g_b 后引用被清理
    svc.delete_task("demo", "g_b")
    assert svc.get_playbook("demo")["tasks"][0]["next_task_ids"] == []
    # 重新添加并移除连线
    svc.add_task("demo", {"id": "g_b", "prompt": "B", "type": "daily"})
    svc.set_edge("demo", "g_a", "g_b")
    svc.remove_edge("demo", "g_a", "g_b")
    assert svc.get_playbook("demo")["tasks"][0]["next_task_ids"] == []
    # 非法连线
    from deskbot_server.service.quest_service import QuestError

    with pytest.raises(QuestError, match="不存在"):
        svc.set_edge("demo", "g_a", "ghost")
    with pytest.raises(QuestError, match="自身"):
        svc.set_edge("demo", "g_a", "g_a")


def test_legacy_playbook_upgrade_on_read(env):
    """旧格式文件读侧升级：goal→prompt、on_success 取 id 去分、残余字段消失。"""
    import json as json_mod

    legacy = {
        "name": "legacy",
        "tasks": [
            {
                "id": "g_a",
                "title": "标题",
                "goal": "旧目标",
                "strategy": "旧策略",
                "activation_score": 100,
                "initial_status": "running",
                "success_condition": "用户回应",
                "failure_condition": "没回应",
                "on_success": [{"id": "g_b", "score": 10}],
                "on_failure": [{"id": "g_c", "score": 5}],
                "score_sources": {"conversation": True, "time": "08:00"},
                "pos": {"x": 1, "y": 2, "width": 210, "height": 120},
            },
            {"id": "g_b", "goal": "后继", "on_success": []},
        ],
    }
    svc = _svc()
    pb_dir = env / "playbooks"
    pb_dir.mkdir(parents=True, exist_ok=True)
    (pb_dir / "legacy.json").write_text(json_mod.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    got = svc.get_playbook("legacy")
    assert got is not None
    t0 = got["tasks"][0]
    assert t0["prompt"] == "旧目标"
    assert t0["next_task_ids"] == ["g_b"]
    assert t0["type"] == "once"
    for k in (
        "goal",
        "title",
        "strategy",
        "activation_score",
        "initial_status",
        "success_condition",
        "failure_condition",
        "on_success",
        "on_failure",
        "score_sources",
    ):
        assert k not in t0
    assert "created_at" in t0 and "updated_at" in t0
    # 保存后落盘为新形状
    svc.save_playbook("legacy", got)
    on_disk = json_mod.loads((pb_dir / "legacy.json").read_text(encoding="utf-8"))
    assert "goal" not in on_disk["tasks"][0]


# ── 实例与状态机 ──────────────────────────────────────────────


def test_ensure_instances_entry_running(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    r = svc.ensure_instances("dev1", "demo")
    assert r == {"created": 4, "activated": 3, "total": 4}
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    # 无入边入口（greet/daily/habit）直接 running；有前置的 learn_name 等激活
    assert by_id["g_greet"]["status"] == "running"
    assert by_id["g_daily"]["status"] == "running"
    assert by_id["g_habit"]["status"] == "running"
    assert by_id["g_learn_name"]["status"] == "not_started"
    # 幂等
    r2 = svc.ensure_instances("dev1", "demo")
    assert r2["created"] == 0


def test_reset_instances(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    svc.complete_task("dev1", "demo", "g_greet", user="小明", reason="小明回应了问候")
    svc.reset_instances("dev1", "demo")
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_learn_name"]["status"] == "not_started"
    assert by_id["g_greet"]["status"] == "running"  # 入口重建即 running


def test_complete_once_activates_nexts(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    r = svc.complete_task("dev1", "demo", "g_greet", user="小明", reason="小明回应了问候")
    assert r["type"] == "once" and r["status"] == "completed"
    assert r["task"]["status"] == "completed"
    assert r["task"]["finished_at"] is not None
    assert r["task"]["result"] == "小明回应了问候"
    assert r["activated"] == [{"task_id": "g_learn_name", "status": "running", "created": False}]
    name = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}["g_learn_name"]
    assert name["status"] == "running" and name["started_at"] is not None


def test_complete_once_auto_creates_missing_next_instance(env):
    from deskbot_server.dao import quest_mapper

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    # 删掉后继实例（模拟编辑器加边后未分配实例）
    quest_mapper.delete_instance("dev1", "demo", "g_learn_name")
    r = svc.complete_task("dev1", "demo", "g_greet", user="小明", reason="回应了")
    assert r["activated"] == [{"task_id": "g_learn_name", "status": "running", "created": True}]
    assert {i["task_id"] for i in svc.get_instances("dev1", "demo")} == {
        "g_greet",
        "g_learn_name",
        "g_daily",
        "g_habit",
    }


def test_complete_once_does_not_revive_completed_next(env):
    svc = _svc()
    pb = _demo_playbook()
    svc.save_playbook("demo", pb)
    svc.ensure_instances("dev1", "demo")
    # 先把后继手工置 completed（模拟已完成），complete 后不得复活、不得出现在 activated
    svc.set_state("dev1", "demo", "g_learn_name", "completed", "早就完成")
    r = svc.complete_task("dev1", "demo", "g_greet", user="小明", reason="回应了")
    assert r["activated"] == []
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_learn_name"]["status"] == "completed"


def test_complete_once_guards(env):
    from deskbot_server.service.quest_service import QuestError

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    # 未激活不能完成
    with pytest.raises(QuestError, match="未激活"):
        svc.complete_task("dev1", "demo", "g_learn_name", user="小明", reason="x")
    # 重复完成
    svc.complete_task("dev1", "demo", "g_greet", user="小明", reason="回应了")
    with pytest.raises(QuestError, match="不能重复完成"):
        svc.complete_task("dev1", "demo", "g_greet", user="小明", reason="再次")
    # 缺少 reason
    with pytest.raises(QuestError, match="原因"):
        svc.complete_task("dev1", "demo", "g_daily", user="小明", reason="  ")


def test_complete_daily_writes_done_list_and_dedupes(env, monkeypatch):
    """日常任务：记入该用户今日 done_list；同日同用户第二次 complete → deduped。"""
    import datetime as dt

    import deskbot_server.dao.user_social_store as store

    _redirect_data_dir(monkeypatch, env)
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    monkeypatch.setattr(store, "_beijing_now", lambda: dt.datetime(2026, 9, 3, 12, 0, 0))

    r1 = svc.complete_task("dev1", "demo", "g_daily", user="小明", reason="小明说中午吃了饺子")
    assert r1["type"] == "daily" and r1["status"] == "running"
    assert r1["written"] is True and r1["deduped"] is False
    p = env / "data" / "dev1" / "done_list_小明_20260903.txt"
    assert p.is_file()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines == ["2026-09-03 12:00:00 [g_daily] 小明说中午吃了饺子"]
    # 同日同任务再完成（换说法）→ deduped=True 不再写
    r2 = svc.complete_task("dev1", "demo", "g_daily", user="小明", reason="又确认了一次")
    assert r2["written"] is False and r2["deduped"] is True
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1
    # 实例保持 running
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_daily"]["status"] == "running"
    assert by_id["g_daily"]["finished_at"] is None
    # 换用户 → 各自记录
    r3 = svc.complete_task("dev1", "demo", "g_daily", user="小红", reason="小红吃了面条")
    assert r3["written"] is True and r3["deduped"] is False
    assert (env / "data" / "dev1" / "done_list_小红_20260903.txt").is_file()


def test_complete_long_term_appends_progress(env, monkeypatch):
    """长期任务：user_info 累计进展行；实例保持 running。"""
    import datetime as dt

    import deskbot_server.dao.user_social_store as store

    _redirect_data_dir(monkeypatch, env)
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    monkeypatch.setattr(store, "_beijing_now", lambda: dt.datetime(2026, 9, 3, 12, 0, 0))

    r1 = svc.complete_task("dev1", "demo", "g_habit", user="小明", reason="小明说他喜欢乐高和足球")
    assert r1["written"] is True and r1["status"] == "running"
    # 同日同任务再次取得新进展 → 追加（不是 deduped）
    r2 = svc.complete_task("dev1", "demo", "g_habit", user="小明", reason="小明还喜欢画画")
    assert r2["written"] is True and r2["deduped"] is False
    p = env / "data" / "dev1" / "user_info_小明.txt"
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all("[g_habit]" in ln for ln in lines)
    # 精确重复行 → 行级去重
    r3 = svc.complete_task("dev1", "demo", "g_habit", user="小明", reason="小明说他喜欢乐高和足球")
    assert r3["written"] is False and r3["deduped"] is True
    # 实例保持 running
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_habit"]["status"] == "running"


def test_complete_daily_long_term_require_user(env):
    from deskbot_server.service.quest_service import QuestError

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    with pytest.raises(QuestError, match="user"):
        svc.complete_task("dev1", "demo", "g_daily", user="", reason="x")
    with pytest.raises(QuestError, match="user"):
        svc.complete_task("dev1", "demo", "g_habit", user="a/b", reason="x")
    # 非法 user 名 → QuestError
    with pytest.raises(QuestError, match="user"):
        svc.complete_task("dev1", "demo", "g_daily", user="a/b", reason="x")


def test_delete_task_cleans_instances_and_refs(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    assert len(svc.get_instances("dev1", "demo")) == 4
    svc.delete_task("demo", "g_learn_name")
    assert len(svc.get_instances("dev1", "demo")) == 3
    assert svc.get_playbook("demo")["tasks"][0]["next_task_ids"] == []


def test_task_rename_syncs_refs_and_instances(env):
    from deskbot_server.service.quest_service import QuestError

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    svc.complete_task("dev1", "demo", "g_greet", user="小明", reason="回应了")  # 激活 g_learn_name
    t = svc.update_task("demo", "g_learn_name", {"id": "g_learn_user_name"})
    assert t["id"] == "g_learn_user_name"
    pb = svc.get_playbook("demo")
    assert pb["tasks"][0]["next_task_ids"] == ["g_learn_user_name"]
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert "g_learn_name" not in by_id
    assert by_id["g_learn_user_name"]["status"] == "running"
    # 改名冲突 / 非法 id
    with pytest.raises(QuestError, match="已存在"):
        svc.update_task("demo", "g_greet", {"id": "g_learn_user_name"})
    with pytest.raises(QuestError, match="非法"):
        svc.update_task("demo", "g_greet", {"id": "bad id"})


def test_update_task_fields_and_timestamps(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    t = svc.update_task("demo", "g_greet", {"type": "long_term", "prompt": "新的提示"})
    assert t["type"] == "long_term" and t["prompt"] == "新的提示"
    assert t["updated_at"] >= t["created_at"]
    got = svc.get_playbook("demo")["tasks"][0]
    assert got["type"] == "long_term"
    # 未知/旧键被忽略
    t2 = svc.update_task("demo", "g_greet", {"strategy": "x", "goal": "y", "activation_score": 1})
    assert "strategy" not in t2 and "goal" not in t2 and "activation_score" not in t2


def test_set_state_three_states(env):
    from deskbot_server.service.quest_service import QuestError

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    # 沙箱直接改状态：任意跳转，不传播
    r = svc.set_state("dev1", "demo", "g_greet", "completed", "手动完成")
    assert r["status"] == "completed" and r["finished_at"] is not None and r["result"] == "手动完成"
    # completed 改回 running：补 started_at，清终态字段
    r = svc.set_state("dev1", "demo", "g_greet", "running")
    assert r["status"] == "running" and r["finished_at"] is None and r["result"] is None
    assert r["started_at"] is not None
    # running → not_started：清空运行痕迹
    r = svc.set_state("dev1", "demo", "g_greet", "not_started")
    assert r["status"] == "not_started"
    assert r["started_at"] is None and r["finished_at"] is None and r["result"] is None
    # 不传播：g_learn_name 保持 not_started
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_learn_name"]["status"] == "not_started"
    # 非法状态
    with pytest.raises(QuestError, match="status"):
        svc.set_state("dev1", "demo", "g_greet", "done")


def test_get_current_tasks_order_and_tool_calls(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    _bind_device("dev1", "demo")
    svc.ensure_instances("dev1", "demo")
    # 入口 running：once(greet) → long_term(habit) → daily(daily)
    cur = svc.get_current_tasks("dev1")
    assert [t["task_id"] for t in cur] == ["g_greet", "g_habit", "g_daily"]
    t = cur[0]
    assert t["type"] == "once" and t["prompt"] == "主动向用户问好"
    assert "goal" not in t and "score" not in t and "ratio" not in t
    # 完成后 g_learn_name(once) 插入 once 组首位，daily 仍沉底
    svc.complete_task("dev1", "demo", "g_greet", user="小明", reason="回应了")
    cur = svc.get_current_tasks("dev1")
    assert [t["task_id"] for t in cur] == ["g_learn_name", "g_habit", "g_daily"]
    # 工具契约：单个 complete_task，available ids / tasks 带类型
    calls = svc.get_tool_calls("dev1")
    assert len(calls) == 1 and calls[0]["name"] == "complete_task"
    assert set(calls[0]["available_task_ids"]) == {"g_learn_name", "g_habit", "g_daily"}
    assert {x["task_id"] for x in calls[0]["tasks"]} == {"g_learn_name", "g_habit", "g_daily"}
    assert {x["type"] for x in calls[0]["tasks"]} == {"once", "long_term", "daily"}


def test_get_current_tasks_unbound_and_empty(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    # 设备行存在但未绑定 → 空
    _bind_device("dev1", None)
    assert svc.get_current_tasks("dev1") == []
    assert svc.get_tool_calls("dev1") == []
    # 设备行不存在 → 空
    assert svc.get_current_tasks("nobody") == []
    assert svc.get_tool_calls("nobody") == []


def test_get_current_tasks_missing_playbook(env):
    svc = _svc()
    _bind_device("dev1", "ghost")
    assert svc.get_current_tasks("dev1") == []
    assert svc.get_tool_calls("dev1") == []


def test_get_current_tasks_auto_init_and_isolation(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    _bind_device("dev1", "demo")
    # 无实例 → 自动初始化并补激活入口
    cur = svc.get_current_tasks("dev1")
    assert [t["task_id"] for t in cur] == ["g_greet", "g_habit", "g_daily"]
    assert len(svc.get_instances("dev1", "demo")) == 4
    svc.get_current_tasks("dev1")  # 幂等
    assert len(svc.get_instances("dev1", "demo")) == 4
    # 多剧本隔离
    svc.save_playbook("other", _demo_playbook("other"))
    _bind_device("dev2", "other")
    assert [t["task_id"] for t in svc.get_current_tasks("dev2")] == ["g_greet", "g_habit", "g_daily"]
    assert [t["task_id"] for t in svc.get_current_tasks("dev1")] == ["g_greet", "g_habit", "g_daily"]


def test_entry_reconcile_legacy_rows(env):
    """旧库遗留：入口任务 not_started 行经 get_current_tasks 补激活；completed 不被碰。"""
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    _bind_device("dev1", "demo")
    svc.ensure_instances("dev1", "demo")
    # 手工把入口打回 not_started（模拟旧库/手滑），runtime 路径会被补激活
    svc.set_state("dev1", "demo", "g_greet", "not_started")
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_greet"]["status"] == "not_started"  # sandbox 视图不被弹回
    cur = svc.get_current_tasks("dev1")
    assert "g_greet" in [t["task_id"] for t in cur]
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_greet"]["status"] == "running"
    # completed 的入口行不会被复活
    svc.set_state("dev1", "demo", "g_daily", "completed", "手动结束")
    cur = svc.get_current_tasks("dev1")
    assert "g_daily" not in [t["task_id"] for t in cur]
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_daily"]["status"] == "completed"


# ── 后台 Web API 冒烟（页面 + 编辑器全链路）──────────────────


def test_quest_bind_api(env):
    from deskbot_server.dao.device_mapper import get_by_device_id
    from deskbot_server.service.user_service import UserService
    from deskbot_server.web.app import create_app
    from tests.device_bind_helpers import bind_device_online

    svc = _svc()
    svc.create_playbook("demo")
    user = UserService().register("bind@example.com", "password1234")
    bind_device_online(user.id, "dev-bind", display_name="dev-bind")
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": "bind@example.com", "password": "password1234"})

    r = client.put("/app/api/devices/dev-bind/quest", json={"quest_id": "demo"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert r.get_json()["quest_id"] == "demo"
    assert get_by_device_id("dev-bind").quest_id == "demo"
    r = client.put("/app/api/devices/dev-bind/quest", json={"quest_id": "Bad Name"})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    r = client.put("/app/api/devices/dev-bind/quest", json={"quest_id": "ghost"})
    assert r.status_code == 404 and r.get_json()["ok"] is False
    r = client.put("/app/api/devices/dev-bind/quest", json={"quest_id": ""})
    assert r.status_code == 200 and r.get_json()["quest_id"] is None
    assert get_by_device_id("dev-bind").quest_id is None
    r = client.put("/app/api/devices/nonexistent/quest", json={"quest_id": "demo"})
    assert r.status_code == 403 and r.get_json()["ok"] is False


def test_quest_web_api_smoke(env):
    from deskbot_server.service.user_service import UserService
    from deskbot_server.web.app import create_app

    UserService().register("quest@example.com", "password1234")
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": "quest@example.com", "password": "password1234"})

    resp = client.get("/quest")
    assert resp.status_code == 200
    assert "questApp" in resp.text

    # 创建剧本 + 两个任务 + 后继连线
    assert client.post("/api/quest/playbooks", json={"name": "demo"}).get_json()["ok"] is True
    r = client.post("/api/quest/playbooks/demo/tasks", json={"id": "g_a", "prompt": "A"})
    assert r.get_json()["ok"] is True and r.get_json()["task"]["type"] == "once"
    r = client.post(
        "/api/quest/playbooks/demo/tasks", json={"id": "g_b", "prompt": "B", "type": "daily"}
    )
    assert r.get_json()["ok"] is True
    r = client.put("/api/quest/playbooks/demo/edges", json={"from": "g_a", "to": "g_b"})
    assert r.get_json()["ok"] is True and r.get_json()["edge"] == {"from": "g_a", "to": "g_b"}
    # 非法连线（目标不存在）→ 400
    bad = client.put("/api/quest/playbooks/demo/edges", json={"from": "g_a", "to": "ghost"})
    assert bad.status_code == 400 and bad.get_json()["ok"] is False
    # 删边（query 参数）→ 修复 args_get 的回归
    r = client.delete("/api/quest/playbooks/demo/edges?from=g_a&to=g_b")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.get("/api/quest/playbooks/demo").get_json()["playbook"]["tasks"][0][
        "next_task_ids"
    ] == []

    # 沙箱状态：入口 g_a running、g_b not_started
    client.put("/api/quest/playbooks/demo/edges", json={"from": "g_a", "to": "g_b"})
    r = client.get("/api/quest/playbooks/demo/state")
    data = r.get_json()
    assert data["ok"] is True
    assert data["instances"]["g_a"]["status"] == "running"
    assert data["instances"]["g_b"]["status"] == "not_started"

    # 模拟 set_state：g_b 直接置 running（沙箱任意跳转，不传播）
    r = client.post(
        "/api/quest/playbooks/demo/simulate/g_b",
        json={"action": "set_state", "status": "running"},
    )
    assert r.status_code == 200 and r.get_json()["result"]["status"] == "running"

    # 模拟 complete：g_a(once) 完成 → g_b 已 running 不动、不重复报错
    r = client.post(
        "/api/quest/playbooks/demo/simulate/g_a",
        json={"action": "complete", "reason": "用户回应了"},
    )
    data = r.get_json()
    assert data["ok"] is True and data["result"]["status"] == "completed"
    assert data["result"]["activated"] == []  # g_b 已是 running
    # 重复 complete → 400 错误提示
    r = client.post(
        "/api/quest/playbooks/demo/simulate/g_a",
        json={"action": "complete", "reason": "again"},
    )
    assert r.status_code == 400 and r.get_json()["ok"] is False
    # 非法 action → 400
    r = client.post("/api/quest/playbooks/demo/simulate/g_a", json={"action": "success"})
    assert r.status_code == 400 and r.get_json()["ok"] is False

    # 导出可下载
    r = client.get("/api/quest/playbooks/demo/export")
    assert r.status_code == 200 and r.get_json()["name"] == "demo"

    # 导入覆盖：空剧本允许；非法任务（缺 prompt）报错
    empty = client.post("/api/quest/playbooks/demo/import", json={"name": "demo", "tasks": []})
    assert empty.status_code == 200 and empty.get_json()["playbook"]["tasks"] == []
    bad = client.post("/api/quest/playbooks/demo/import", json={"name": "demo", "tasks": [{"id": "g_x"}]})
    assert bad.status_code == 400 and bad.get_json()["ok"] is False
    good = client.post("/api/quest/playbooks/demo/import", json=_demo_playbook())
    assert good.status_code == 200 and len(good.get_json()["playbook"]["tasks"]) == 4

    # 未登录访问 → 401
    anon = app.test_client()
    assert anon.get("/api/quest/playbooks").status_code == 401
