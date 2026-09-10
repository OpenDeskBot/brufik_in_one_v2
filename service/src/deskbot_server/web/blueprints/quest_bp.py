"""网页剧本任务系统：模块连线编辑器页面与 REST API。

剧本设计后台只管理 data/quest/ 下的剧本 JSON（任务定义），与设备无关。
模拟端点（simulate）用固定沙箱设备跑 QuestService 状态机（与 LLM 工具同一条逻辑），
便于设计时试跑剧情走向；真实设备运行态由运行时引擎后续接入。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from deskbot_server.auth.permissions import RequireDeveloper
from deskbot_server.service.quest_service import (
    DESIGN_SANDBOX_DEVICE,
    QuestError,
    QuestService,
)
from deskbot_server.web.view_helpers import (
    ViewAPIRoute,
    args_get,
    get_json,
    jsonify,
    render_template,
)

router = APIRouter(route_class=ViewAPIRoute, tags=["quest"])

# 沙箱模拟动作：set_state 直接改三态 / complete 走完整任务完成链（与 LLM 工具同一条路径）
_SIM_ACTIONS = ("set_state", "complete")
_SIM_USER_DEFAULT = "__design__"


def _service() -> QuestService:
    return QuestService()


def _ensure_sandbox(name: str) -> None:
    """懒创建沙箱实例（幂等），保证设计页状态显示与模拟可用。"""
    _service().ensure_instances(DESIGN_SANDBOX_DEVICE, name)


def _pb_error(exc: QuestError) -> tuple[dict, int]:
    return jsonify({"ok": False, "error": str(exc)}), 400


@router.get("/quest")
def quest_page(request: Request, user: RequireDeveloper):
    return render_template(request, "quests.html", active_nav="quest")


# ── 剧本 ──────────────────────────────────────────────────────


@router.get("/api/quest/playbooks")
def api_playbooks_list(request: Request, user: RequireDeveloper):
    return jsonify({"ok": True, "playbooks": _service().list_playbooks()})


@router.post("/api/quest/playbooks")
def api_playbooks_create(request: Request, user: RequireDeveloper):
    body = get_json(request, silent=True) or {}
    name = str(body.get("name") or "").strip()
    try:
        return jsonify({"ok": True, "playbook": _service().create_playbook(name)})
    except QuestError as exc:
        return _pb_error(exc)


@router.get("/api/quest/playbooks/{name}")
def api_playbook_get(request: Request, name: str, user: RequireDeveloper):
    try:
        pb = _service().get_playbook(name)
    except QuestError as exc:
        return _pb_error(exc)
    if pb is None:
        return jsonify({"ok": False, "error": f"剧本不存在: {name}"}), 404
    return jsonify({"ok": True, "playbook": pb})


@router.get("/api/quest/playbooks/{name}/state")
def api_playbook_state(request: Request, name: str, user: RequireDeveloper):
    """设计沙箱状态：{task_id: 实例运行态}，供编辑器叠加显示（与设备无关）。"""
    try:
        _ensure_sandbox(name)
        instances = {r["task_id"]: r for r in _service().get_instances(DESIGN_SANDBOX_DEVICE, name)}
        return jsonify({"ok": True, "instances": instances})
    except QuestError as exc:
        return _pb_error(exc)


@router.delete("/api/quest/playbooks/{name}")
def api_playbook_delete(request: Request, name: str, user: RequireDeveloper):
    try:
        if _service().get_playbook(name) is None:
            return jsonify({"ok": False, "error": f"剧本不存在: {name}"}), 404
        _service().delete_playbook(name)
        return jsonify({"ok": True})
    except QuestError as exc:
        return _pb_error(exc)


@router.get("/api/quest/playbooks/{name}/export")
def api_playbook_export(request: Request, name: str, user: RequireDeveloper):
    try:
        pb = _service().get_playbook(name)
    except QuestError as exc:
        return _pb_error(exc)
    if pb is None:
        return jsonify({"ok": False, "error": f"剧本不存在: {name}"}), 404
    from fastapi.responses import JSONResponse

    return JSONResponse(
        pb,
        headers={
            "Content-Disposition": f'attachment; filename="quest_{name}.json"',
            "Content-Type": "application/json; charset=utf-8",
        },
    )


@router.post("/api/quest/playbooks/{name}/import")
def api_playbook_import(request: Request, name: str, user: RequireDeveloper):
    """整体覆盖导入：body 为剧本 JSON（含 tasks），校验通过后原子写盘。"""
    data = get_json(request, silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "body 必须是剧本 JSON 对象"}), 400
    try:
        return jsonify({"ok": True, "playbook": _service().save_playbook(name, data)})
    except QuestError as exc:
        return _pb_error(exc)


# ── 任务 ──────────────────────────────────────────────────────


@router.post("/api/quest/playbooks/{name}/tasks")
def api_task_create(request: Request, name: str, user: RequireDeveloper):
    body = get_json(request, silent=True) or {}
    try:
        return jsonify({"ok": True, "task": _service().add_task(name, body)})
    except QuestError as exc:
        return _pb_error(exc)


@router.put("/api/quest/playbooks/{name}/tasks/{task_id}")
def api_task_update(request: Request, name: str, task_id: str, user: RequireDeveloper):
    body = get_json(request, silent=True) or {}
    try:
        return jsonify({"ok": True, "task": _service().update_task(name, task_id, body)})
    except QuestError as exc:
        return _pb_error(exc)


@router.delete("/api/quest/playbooks/{name}/tasks/{task_id}")
def api_task_delete(request: Request, name: str, task_id: str, user: RequireDeveloper):
    try:
        _service().delete_task(name, task_id)
        return jsonify({"ok": True})
    except QuestError as exc:
        return _pb_error(exc)


# ── 连线（后继单端口，无分数）─────────────────────────────────


@router.put("/api/quest/playbooks/{name}/edges")
def api_edge_set(request: Request, name: str, user: RequireDeveloper):
    body = get_json(request, silent=True) or {}
    from_id = str(body.get("from") or "").strip()
    to_id = str(body.get("to") or "").strip()
    try:
        return jsonify({"ok": True, "edge": _service().set_edge(name, from_id, to_id)})
    except QuestError as exc:
        return _pb_error(exc)


@router.delete("/api/quest/playbooks/{name}/edges")
def api_edge_remove(request: Request, name: str, user: RequireDeveloper):
    from_id = args_get(request, "from", "", type=str) or ""
    to_id = args_get(request, "to", "", type=str) or ""
    try:
        _service().remove_edge(name, from_id, to_id)
        return jsonify({"ok": True})
    except QuestError as exc:
        return _pb_error(exc)


# ── 设计沙箱模拟（与 LLM 工具共用同一 service 方法）───────────


@router.post("/api/quest/playbooks/{name}/simulate/{task_id}")
def api_quest_simulate(request: Request, name: str, task_id: str, user: RequireDeveloper):
    """沙箱模拟：
    action=set_state → 直接改三态状态（status：not_started/running/completed，result 可选）
    action=complete → 走完整 complete_task（reason 可选；user 默认 __design__；
                       一次性会置完成并激活后继，日常/长期会真写 __design__ 下的记录文件）
    """
    body = get_json(request, silent=True) or {}
    action = str(body.get("action") or "").strip()
    if action not in _SIM_ACTIONS:
        return jsonify({"ok": False, "error": f"action 必须是 {_SIM_ACTIONS}（{action!r}）"}), 400
    svc = _service()
    try:
        _ensure_sandbox(name)
        if action == "complete":
            user_name = str(body.get("user") or _SIM_USER_DEFAULT).strip() or _SIM_USER_DEFAULT
            reason = str(body.get("reason") or "").strip()
            return jsonify(
                {
                    "ok": True,
                    "result": svc.complete_task(
                        DESIGN_SANDBOX_DEVICE, name, task_id, user=user_name, reason=reason
                    ),
                }
            )
        # set_state
        status = str(body.get("status") or "").strip()
        result = str(body.get("result") or "").strip() or None
        return jsonify(
            {"ok": True, "result": svc.set_state(DESIGN_SANDBOX_DEVICE, name, task_id, status, result)}
        )
    except QuestError as exc:
        return _pb_error(exc)


ENDPOINTS = {
    "quest.page": "/quest",
}
