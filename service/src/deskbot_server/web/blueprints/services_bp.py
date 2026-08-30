"""网页外部进程服务管理：页面与 REST API。

管理对象是 service/externals/ 下声明式 manifest 定义的外部服务
（如 tts-engine），支持安装/启动/停止/重启/状态/日志/默认启动。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request

from deskbot_server.service.external.manager import ExternalServiceManager, ServiceError
from deskbot_server.web.deps import RequireUser
from deskbot_server.web.view_helpers import ViewAPIRoute, args_get, get_json, jsonify, render_template

router = APIRouter(route_class=ViewAPIRoute, tags=["services"])

NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _manager() -> ExternalServiceManager:
    from deskbot_server.controller.runtime import get_runtime

    mgr = get_runtime().external_manager
    if mgr is None:
        raise ServiceError("外部服务管理器未装配（运行时缺少 external_manager）")
    return mgr


def _validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ServiceError(f"非法服务名: {name!r}")
    return name


@router.get("/services")
def services_page(request: Request, user: RequireUser):
    return render_template(request, "app2c/services.html", active_nav="services")


@router.get("/api/services")
def api_services_list(request: Request, user: RequireUser):
    try:
        return jsonify({"ok": True, "services": [s.to_dict() for s in _manager().status_all()]})
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.get("/api/services/{name}/logs")
def api_service_logs(request: Request, name: str, user: RequireUser):
    name = _validate_name(name)
    since = args_get(request, "since", 0, type=int)
    try:
        return jsonify({"ok": True, **_manager().log_snapshot(name, since)})
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@router.post("/api/services/{name}/install")
async def api_service_install(request: Request, name: str, user: RequireUser):
    name = _validate_name(name)
    try:
        return _after_action(await _manager().install(name))
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


@router.post("/api/services/{name}/uninstall")
async def api_service_uninstall(request: Request, name: str, user: RequireUser):
    name = _validate_name(name)
    try:
        return _after_action(await _manager().uninstall(name))
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


@router.post("/api/services/{name}/start")
async def api_service_start(request: Request, name: str, user: RequireUser):
    name = _validate_name(name)
    try:
        return _after_action(await _manager().start(name))
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


@router.post("/api/services/{name}/stop")
async def api_service_stop(request: Request, name: str, user: RequireUser):
    name = _validate_name(name)
    try:
        return _after_action(await _manager().stop(name))
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


@router.post("/api/services/{name}/restart")
async def api_service_restart(request: Request, name: str, user: RequireUser):
    name = _validate_name(name)
    try:
        return _after_action(await _manager().restart(name))
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


@router.get("/api/services/{name}/test-info")
async def api_service_test_info(request: Request, name: str, user: RequireUser):
    """测试契约元信息（不发请求）：契约描述 / 请求 / 可复制 curl 命令。

    管理后台「测试」对话框打开时调用；点「执行测试」才真正发请求（POST test）。
    """
    name = _validate_name(name)
    try:
        return jsonify({"ok": True, **await _manager().test_info(name)})
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@router.post("/api/services/{name}/test")
async def api_service_test(request: Request, name: str, user: RequireUser):
    """按测试规格（manifest test 覆盖或类型契约）发标准样本请求并校验响应（对话框展示）。

    fr 额外支持 JSON body 传 {"image_base64": "..."} 指定测试图片；
    asr 额外支持 {"audio_base64": "..."} 指定测试音频（WAV 或原始 PCM）；
    tts 额外支持 {"text": "..."} 指定合成文本；
    不传则用默认样本（data/test/face.jpg / asr.wav / manifest 或契约文本，结果里回 base64 供前端预览）。
    """
    name = _validate_name(name)
    body = get_json(request, silent=True)
    image_base64 = str(body.get("image_base64") or "").strip() if isinstance(body, dict) else ""
    audio_base64 = str(body.get("audio_base64") or "").strip() if isinstance(body, dict) else ""
    text = str(body.get("text") or "").strip() if isinstance(body, dict) else ""
    demo_id = str(body.get("demo_id") or "").strip() if isinstance(body, dict) else ""
    try:
        return jsonify(
            {
                "ok": True,
                **await _manager().test_service(
                    name,
                    image_base64=image_base64,
                    audio_base64=audio_base64,
                    text=text or None,
                    demo_id=demo_id or None,
                ),
            }
        )
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@router.put("/api/services/{name}/autostart")
def api_service_autostart(request: Request, name: str, user: RequireUser):
    name = _validate_name(name)
    body = get_json(request, silent=True) or {}
    try:
        enabled = bool(body.get("enabled"))
        return _after_action(_manager().set_auto_start(name, enabled))
    except ServiceError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


def _after_action(snapshot) -> object:
    return jsonify({"ok": True, "service": snapshot.to_dict()})


ENDPOINTS = {
    "services.page": "/services",
}
