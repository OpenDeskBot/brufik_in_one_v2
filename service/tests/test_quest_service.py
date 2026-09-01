"""剧本任务引擎测试：剧本管理 / 状态机 / 分数流转 / 工具函数。

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


def _demo_playbook(name: str = "demo") -> dict:
    """三段剧本：起点问候 →(成功 10 分)→ 了解姓名 →(失败 6 分)→ 换角度。"""
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


# ── 剧本管理 ──────────────────────────────────────────────────


def test_playbook_create_list_get_delete(env):
    svc = _svc()
    assert svc.list_playbooks() == []
    pb = svc.create_playbook("demo")
    assert pb == {"name": "demo", "tasks": []}
    assert svc.list_playbooks() == ["demo"]
    svc.save_playbook("demo", _demo_playbook())
    got = svc.get_playbook("demo")
    assert len(got["tasks"]) == 3
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
    pb["tasks"][0]["on_success"] = [{"id": "ghost", "score": 1}]
    with pytest.raises(QuestError, match="不存在"):
        _svc().save_playbook("demo", pb)


def test_validate_cycle(env):
    from deskbot_server.service.quest_service import QuestError

    pb = _demo_playbook()
    pb["tasks"][2]["on_success"] = [{"id": "g_greet", "score": 1}]  # soften → greet 成环
    with pytest.raises(QuestError, match="成环"):
        _svc().save_playbook("demo", pb)


def test_task_crud_and_edges(env):
    svc = _svc()
    svc.create_playbook("demo")
    t = svc.add_task("demo", {"id": "g_a", "goal": "A"})
    assert t["activation_score"] == 1
    assert t["title"] == "notitle"  # 默认标题
    assert t["pos"]["x"] == 120  # 默认摆放
    svc.add_task("demo", {"id": "g_b", "goal": "B"})
    edge = svc.set_edge("demo", "g_a", "success", "g_b", 5)
    assert edge == {"from": "g_a", "port": "success", "to": "g_b", "score": 5}
    svc.set_edge("demo", "g_a", "success", "g_b", 8)  # 重复连 = 改分
    pb = svc.get_playbook("demo")
    assert pb["tasks"][0]["on_success"] == [{"id": "g_b", "score": 8}]
    # 删除 g_b 后引用被清理
    svc.delete_task("demo", "g_b")
    pb = svc.get_playbook("demo")
    assert pb["tasks"][0]["on_success"] == []
    # 失败口连线
    svc.add_task("demo", {"id": "g_b", "goal": "B"})
    svc.set_edge("demo", "g_a", "failed", "g_b", 3)
    svc.remove_edge("demo", "g_a", "failed", "g_b")
    assert svc.get_playbook("demo")["tasks"][0]["on_failure"] == []


# ── 实例与状态机 ──────────────────────────────────────────────


def test_ensure_instances_entry_running(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    r = svc.ensure_instances("dev1", "demo")
    assert r == {"created": 3, "activated": 1, "total": 3}
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_greet"]["status"] == "running"  # 起点任务
    assert by_id["g_greet"]["started_at"] is not None
    assert by_id["g_learn_name"]["status"] == "not_started"
    # 幂等
    r2 = svc.ensure_instances("dev1", "demo")
    assert r2["created"] == 0


def test_reset_instances(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    svc.contribute_score("dev1", "demo", "g_learn_name", 10)
    svc.reset_instances("dev1", "demo")
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_learn_name"]["status"] == "not_started"
    assert by_id["g_learn_name"]["current_score"] == 0


def test_contribute_score_activates(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    r = svc.contribute_score("dev1", "demo", "g_learn_name", 8)
    assert r["status"] == "not_started" and r["current_score"] == 8
    r = svc.contribute_score("dev1", "demo", "g_learn_name", 2)
    assert r["status"] == "running" and r["activated"] is True
    assert r["started_at"] is not None


def test_contribute_score_clamped(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    r = svc.contribute_score("dev1", "demo", "g_learn_name", 999)
    from deskbot_server.service.quest_service import MAX_SCORE_PER_CONTRIBUTE

    assert r["current_score"] == MAX_SCORE_PER_CONTRIBUTE  # 封顶


def test_update_task_result_success_propagates_and_activates(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    # g_greet 成功 → g_learn_name +10 分，正好达到激活线 10 → 自动激活
    r = svc.update_task_result("dev1", "demo", "g_greet", "success", "用户回应了问候")
    assert r["task"]["status"] == "success"
    assert r["task"]["finished_at"] is not None
    assert r["task"]["result"] == "用户回应了问候"
    assert r["propagated"] == [{"task_id": "g_learn_name", "current_score": 10, "status": "running"}]
    assert r["activated"][0]["task_id"] == "g_learn_name"
    name = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}["g_learn_name"]
    assert name["status"] == "running" and name["current_score"] == 10


def test_update_task_result_failure_propagates(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    # 先激活 g_learn_name，再让它失败 → soften +6 分 → 激活线 6 → 自动激活
    svc.contribute_score("dev1", "demo", "g_learn_name", 10)
    r = svc.update_task_result("dev1", "demo", "g_learn_name", "failed", "用户表示不想说")
    assert r["task"]["status"] == "failed"
    assert r["activated"][0]["task_id"] == "g_learn_name_soften"
    soften = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}["g_learn_name_soften"]
    assert soften["status"] == "running" and soften["current_score"] == 6


def test_update_task_result_guards(env):
    from deskbot_server.service.quest_service import QuestError

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    # 未激活不能判定
    with pytest.raises(QuestError, match="未激活"):
        svc.update_task_result("dev1", "demo", "g_learn_name", "success", "x")
    # 终态不能重复判定
    svc.update_task_result("dev1", "demo", "g_greet", "success", "用户回应了问候")
    with pytest.raises(QuestError, match="终态"):
        svc.update_task_result("dev1", "demo", "g_greet", "failed", "y")
    # 非法状态值 / 缺少结果
    with pytest.raises(QuestError, match="status"):
        svc.update_task_result("dev1", "demo", "g_learn_name", "done", "z")
    svc.contribute_score("dev1", "demo", "g_learn_name", 10)
    with pytest.raises(QuestError, match="结果"):
        svc.update_task_result("dev1", "demo", "g_learn_name", "success", "")


def test_update_task_strategy_and_effective(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    assert svc.get_effective_strategy("dev1", "demo", "g_greet") == "轻松自然地打招呼"
    r = svc.update_task_strategy("dev1", "demo", "g_greet", "用户喜欢简洁，问候要短")
    assert r["strategy"] == "用户喜欢简洁，问候要短"
    assert svc.get_effective_strategy("dev1", "demo", "g_greet") == "用户喜欢简洁，问候要短"


def test_delete_task_cleans_instances(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    assert len(svc.get_instances("dev1", "demo")) == 3
    svc.delete_task("demo", "g_learn_name_soften")
    assert len(svc.get_instances("dev1", "demo")) == 2
    # 引用被清理
    assert svc.get_playbook("demo")["tasks"][1]["on_failure"] == []


def test_task_title_and_rename(env):
    from deskbot_server.service.quest_service import QuestError

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    # title 字段
    t = svc.update_task("demo", "g_greet", {"title": "初次问候"})
    assert t["title"] == "初次问候"
    assert svc.get_playbook("demo")["tasks"][0]["title"] == "初次问候"
    # 改名：连线引用联动 + 实例改名且运行态保留
    svc.contribute_score("dev1", "demo", "g_learn_name", 10)  # 激活 g_learn_name
    t = svc.update_task("demo", "g_learn_name", {"id": "g_learn_user_name"})
    assert t["id"] == "g_learn_user_name"
    pb = svc.get_playbook("demo")
    assert pb["tasks"][0]["on_success"] == [{"id": "g_learn_user_name", "score": 10}]
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert "g_learn_name" not in by_id
    assert by_id["g_learn_user_name"]["status"] == "running"
    assert by_id["g_learn_user_name"]["current_score"] == 10
    # 改名冲突 / 非法 id
    with pytest.raises(QuestError, match="已存在"):
        svc.update_task("demo", "g_greet", {"id": "g_learn_user_name"})
    with pytest.raises(QuestError, match="非法"):
        svc.update_task("demo", "g_greet", {"id": "bad id"})


def test_set_state_direct_switch(env):
    from deskbot_server.service.quest_service import QuestError

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    # 沙箱直接改状态：任意跳转，不传播
    r = svc.set_state("dev1", "demo", "g_greet", "success", "手动设置")
    assert r["status"] == "success" and r["finished_at"] is not None and r["result"] == "手动设置"
    # 终态改回 running：补 started_at，清 finished_at/result
    r = svc.set_state("dev1", "demo", "g_greet", "running")
    assert r["status"] == "running" and r["finished_at"] is None and r["result"] is None
    assert r["started_at"] is not None
    # 不传播：g_learn_name 保持 not_started
    by_id = {i["task_id"]: i for i in svc.get_instances("dev1", "demo")}
    assert by_id["g_learn_name"]["status"] == "not_started"
    # 非法状态
    with pytest.raises(QuestError, match="status"):
        svc.set_state("dev1", "demo", "g_greet", "done")


def test_get_current_tasks_and_tool_calls(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    _bind_device("dev1", "demo")
    svc.ensure_instances("dev1", "demo")
    # 起点任务 running，其余 not_started
    cur = svc.get_current_tasks("dev1")
    assert [t["task_id"] for t in cur] == ["g_greet"]
    t = cur[0]
    assert t["goal"] == "主动向用户问好"
    assert t["strategy"] == "轻松自然地打招呼"
    assert t["activation_score"] == 100 and t["ratio"] == 0
    # 激活 g_learn_name 后：两个 running，按达成率降序
    svc.contribute_score("dev1", "demo", "g_learn_name", 10)
    cur = svc.get_current_tasks("dev1")
    assert {t["task_id"] for t in cur} == {"g_greet", "g_learn_name"}
    assert cur[0]["task_id"] == "g_learn_name"  # 达成率 1.0 排最前
    # 策略覆盖生效
    svc.update_task_strategy("dev1", "demo", "g_greet", "用户喜欢简洁")
    assert cur[0]["task_id"] == "g_learn_name"
    # 工具契约：三个工具，available_task_ids = 当前 running 任务
    calls = svc.get_tool_calls("dev1")
    assert [c["name"] for c in calls] == ["update_task_result", "update_task_strategy", "contribute_score"]
    assert set(calls[0]["available_task_ids"]) == {"g_greet", "g_learn_name"}
    # 无实例设备返回空
    assert svc.get_current_tasks("nobody") == []
    assert svc.get_tool_calls("nobody")[0]["available_task_ids"] == []


def test_get_current_tasks_unbound(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    # 设备行存在但未绑定 → 空
    _bind_device("dev1", None)
    assert svc.get_current_tasks("dev1") == []
    assert svc.get_tool_calls("dev1")[0]["available_task_ids"] == []
    # 设备行不存在 → 空
    assert svc.get_current_tasks("nobody") == []


def test_get_current_tasks_missing_playbook(env):
    svc = _svc()
    _bind_device("dev1", "ghost")
    # 绑定不存在的剧本 → 空且不抛
    assert svc.get_current_tasks("dev1") == []
    assert svc.get_tool_calls("dev1")[0]["available_task_ids"] == []


def test_get_current_tasks_auto_init_and_isolation(env):
    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    _bind_device("dev1", "demo")
    # 无实例 → 自动初始化：起点任务直接 running
    cur = svc.get_current_tasks("dev1")
    assert [t["task_id"] for t in cur] == ["g_greet"]
    assert cur[0]["playbook"] == "demo"
    assert len(svc.get_instances("dev1", "demo")) == 3  # 自动 ensure 全量实例
    # 幂等：再来一次不重复创建
    svc.get_current_tasks("dev1")
    assert len(svc.get_instances("dev1", "demo")) == 3
    # 多剧本隔离：dev2 绑 other，互不串
    svc.save_playbook("other", _demo_playbook("other"))
    _bind_device("dev2", "other")
    assert [t["task_id"] for t in svc.get_current_tasks("dev2")] == ["g_greet"]
    assert [t["task_id"] for t in svc.get_current_tasks("dev1")] == ["g_greet"]
    assert svc.get_tool_calls("dev1")[0]["available_task_ids"] == ["g_greet"]


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

    # 绑定剧本
    r = client.put("/app/api/devices/dev-bind/quest", json={"quest_id": "demo"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert r.get_json()["quest_id"] == "demo"
    assert get_by_device_id("dev-bind").quest_id == "demo"
    # 非法剧本名 → 400
    r = client.put("/app/api/devices/dev-bind/quest", json={"quest_id": "Bad Name"})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    # 不存在的剧本 → 404
    r = client.put("/app/api/devices/dev-bind/quest", json={"quest_id": "ghost"})
    assert r.status_code == 404 and r.get_json()["ok"] is False
    # 空串 = 解绑
    r = client.put("/app/api/devices/dev-bind/quest", json={"quest_id": ""})
    assert r.status_code == 200 and r.get_json()["quest_id"] is None
    assert get_by_device_id("dev-bind").quest_id is None
    # 不属于当前账号的设备 → 403
    r = client.put("/app/api/devices/nonexistent/quest", json={"quest_id": "demo"})
    assert r.status_code == 403 and r.get_json()["ok"] is False


def test_contribute_to_terminal_rejected(env):
    from deskbot_server.service.quest_service import QuestError

    svc = _svc()
    svc.save_playbook("demo", _demo_playbook())
    svc.ensure_instances("dev1", "demo")
    svc.update_task_result("dev1", "demo", "g_greet", "success", "用户回应了问候")
    with pytest.raises(QuestError, match="终态"):
        svc.contribute_score("dev1", "demo", "g_greet", 5)


# ── 后台 Web API 冒烟（页面 + 编辑器全链路）──────────────────


def test_quest_web_api_smoke(env):
    from deskbot_server.service.user_service import UserService
    from deskbot_server.web.app import create_app

    UserService().register("quest@example.com", "password1234")
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": "quest@example.com", "password": "password1234"})

    # 编辑器页面可渲染
    resp = client.get("/quest")
    assert resp.status_code == 200
    assert "questApp" in resp.text

    # 创建剧本 + 两个任务 + 成功口连线
    assert client.post("/api/quest/playbooks", json={"name": "demo"}).get_json()["ok"] is True
    r = client.post(
        "/api/quest/playbooks/demo/tasks",
        json={"id": "g_a", "goal": "A", "initial_status": "running", "activation_score": 100},
    )
    assert r.get_json()["ok"] is True
    r = client.post(
        "/api/quest/playbooks/demo/tasks",
        json={"id": "g_b", "goal": "B", "activation_score": 10},
    )
    assert r.get_json()["ok"] is True
    r = client.put(
        "/api/quest/playbooks/demo/edges",
        json={"from": "g_a", "port": "success", "to": "g_b", "score": 10},
    )
    assert r.get_json()["ok"] is True
    # 非法连线（目标不存在）→ 400
    bad = client.put(
        "/api/quest/playbooks/demo/edges",
        json={"from": "g_a", "port": "success", "to": "ghost", "score": 1},
    )
    assert bad.status_code == 400 and bad.get_json()["ok"] is False

    # 沙箱状态：起点任务 running、其余 not_started
    r = client.get("/api/quest/playbooks/demo/state")
    data = r.get_json()
    assert data["ok"] is True
    assert data["instances"]["g_a"]["status"] == "running"
    assert data["instances"]["g_b"]["status"] == "not_started"

    # 模拟：g_b 加分 9 → 未激活；再 +1 → running（分数达标自动激活）
    r = client.post("/api/quest/playbooks/demo/simulate/g_b", json={"action": "score", "points": 9})
    assert r.get_json()["result"]["status"] == "not_started"
    r = client.post("/api/quest/playbooks/demo/simulate/g_b", json={"action": "score", "points": 1})
    assert r.get_json()["result"]["status"] == "running"

    # 模拟：g_a 成功 → 沿成功口向 g_b 传播 10 分（已 running 继续累积）
    r = client.post(
        "/api/quest/playbooks/demo/simulate/g_a",
        json={"action": "success", "result": "用户回应了问候"},
    )
    data = r.get_json()
    assert data["result"]["task"]["status"] == "success"
    assert data["result"]["propagated"][0]["task_id"] == "g_b"
    assert data["result"]["propagated"][0]["current_score"] == 20
    r = client.get("/api/quest/playbooks/demo/state")
    assert r.get_json()["instances"]["g_b"]["current_score"] == 20

    # 导出可下载
    r = client.get("/api/quest/playbooks/demo/export")
    assert r.status_code == 200 and r.get_json()["name"] == "demo"

    # 导入覆盖：空剧本允许（新建即空），非法任务（缺 goal）报错
    empty = client.post("/api/quest/playbooks/demo/import", json={"name": "demo", "tasks": []})
    assert empty.status_code == 200 and empty.get_json()["playbook"]["tasks"] == []
    bad = client.post(
        "/api/quest/playbooks/demo/import", json={"name": "demo", "tasks": [{"id": "g_x"}]}
    )
    assert bad.status_code == 400 and bad.get_json()["ok"] is False
    good = client.post("/api/quest/playbooks/demo/import", json=_demo_playbook())
    assert good.status_code == 200 and len(good.get_json()["playbook"]["tasks"]) == 3

    # 未登录访问 → 401
    anon = app.test_client()
    assert anon.get("/api/quest/playbooks").status_code == 401
