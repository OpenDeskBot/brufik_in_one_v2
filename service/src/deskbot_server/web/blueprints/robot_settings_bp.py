"""机器人设置：外部实时会话与独立感知服务。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from deskbot_server.auth.permissions import RequireDeveloper
from deskbot_server.controller.decorators import access
from deskbot_server.dao.device_mapper import (
    get_auto_reply,
    get_camera_servo_auto_mode,
    normalize_camera_servo_auto_mode,
    set_auto_reply,
    set_camera_servo_auto_mode,
)
from deskbot_server.service.robot_capability import CapabilityError, RobotCapabilityService
from deskbot_server.web.deps import RequireUser
from deskbot_server.web.pages import page
from deskbot_server.web.session_device import get_current_device_id
from deskbot_server.web.view_helpers import ViewAPIRoute, get_json, jsonify, render_template

router = APIRouter(route_class=ViewAPIRoute, tags=["robot_settings"])


def _svc() -> RobotCapabilityService:
    return RobotCapabilityService()


@router.get("/robot-settings")
@access('user')
@page('app2c/robot_settings.html')
def robot_settings_page(request: Request, user: RequireUser):
    return render_template(request, "app2c/robot_settings.html", active_nav="robot_settings", model_configuration=False)


@router.get("/model-settings")
@access('developer')
@page('app2c/model_settings.html')
def model_settings_page(request: Request, user: RequireDeveloper):
    return render_template(request, "app2c/model_settings.html", active_nav="model_settings", model_configuration=True)


@router.get("/api/robot-settings")
@access('user')
def api_robot_settings_status(request: Request, user: RequireUser):
    try:
        device_id = get_current_device_id(request)
        status = _svc().get_external_status(device_id)
        # 行为开关（设备级，落 devices 表）：自动回复 auto_reply / 人脸跟随 servo_mode
        status["behavior"] = {
            "auto_reply": get_auto_reply(device_id),
            "follow_mode": get_camera_servo_auto_mode(device_id),
        }
        from deskbot_server.service.conversation_service import conversation_status
        status["conversation"] = conversation_status(device_id)
        return jsonify({"ok": True, **status})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/behavior")
@access('user')
async def api_robot_settings_behavior(request: Request, user: RequireUser):
    """行为开关（机器人配置页顶部控制面板）：写 device 表 auto_reply / servo_mode，保存即生效。

    body: {"auto_reply": bool?, "follow_mode": "follow"|"follow_frontal"|"gaze"|""?}，至少一项。
    关闭 auto_reply 会连带清空 follow_mode（与设备调试页一致）；响应回传写后规范态。
    """
    body = get_json(request) or {}
    device_id = get_current_device_id(request)
    if not device_id:
        return jsonify({"ok": False, "error": "请先在顶栏选择设备"}), 400
    has_ar = body.get("auto_reply") is not None
    has_fm = body.get("follow_mode") is not None
    if not has_ar and not has_fm:
        return jsonify({"ok": False, "error": "缺少 auto_reply / follow_mode 参数"}), 400
    if has_ar:
        raw = body.get("auto_reply")
        if not isinstance(raw, bool):
            return jsonify({"ok": False, "error": "invalid auto_reply; use true/false"}), 400
        set_auto_reply(device_id, raw)  # False 会连带清空 servo_mode
        from deskbot_server.service.conversation_service import conversation_provider
        if conversation_provider(device_id) in {"moss-live", "flecket"}:
            # Same reconnect boundary as provider switching; disabled means no
            # upstream microphone or automatically generated MOSS audio.
            from deskbot_server.service.device_ws_service import DeviceWsService
            hub = DeviceWsService.instance()
            if hub and (ws := hub._get_ws(device_id)):
                await hub._close_old_connection(device_id, ws)
    if has_fm:
        raw = str(body.get("follow_mode") or "").strip()
        norm = normalize_camera_servo_auto_mode(raw)
        if raw and not norm:
            return jsonify(
                {"ok": False, "error": "invalid follow_mode; use follow, follow_frontal, gaze or empty"}
            ), 400
        set_camera_servo_auto_mode(device_id, norm)
    return jsonify(
        {
            "ok": True,
            "behavior": {
                "auto_reply": get_auto_reply(device_id),
                "follow_mode": get_camera_servo_auto_mode(device_id),
            },
        }
    )


@router.post("/api/robot-settings/conversation")
@access('user')
async def api_robot_settings_conversation(request: Request, user: RequireUser):
    from deskbot_server.service.conversation_service import save_conversation
    try:
        status = await save_conversation(get_current_device_id(request), get_json(request) or {})
        return jsonify({"ok": True, "conversation": status})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@router.post("/api/robot-settings/face")
@access('user')
def api_robot_settings_apply_face(request: Request, user: RequireUser):
    """人脸识别能力切换：none=不识别；insightface=外部独立服务（config.yaml 真源，保存即生效）。"""
    body = get_json(request) or {}
    try:
        svc = _svc()
        svc.apply_face(str(body.get("mode") or ""))
        status = svc.get_external_status(get_current_device_id(request))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/voiceprint")
@access('user')
def api_robot_settings_apply_voiceprint(request: Request, user: RequireUser):
    """声纹识别能力切换：none=不识别；vpr=外部 wespeaker 独立服务（config.yaml 真源，保存即生效）。"""
    body = get_json(request) or {}
    try:
        svc = _svc()
        svc.apply_voiceprint(str(body.get("mode") or ""))
        status = svc.get_external_status(get_current_device_id(request))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


ENDPOINTS = {"robot_settings.page": "/robot-settings", "robot_settings.models": "/model-settings"}
