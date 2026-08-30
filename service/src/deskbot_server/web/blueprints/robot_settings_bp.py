"""机器人设置：ASR / LLM / TTS 能力查看与热切换（页面 + REST API）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from deskbot_server.service.robot_capability import CapabilityError, RobotCapabilityService
from deskbot_server.web.deps import RequireUser
from deskbot_server.web.session_device import get_current_device_id
from deskbot_server.web.view_helpers import ViewAPIRoute, get_json, jsonify, render_template

router = APIRouter(route_class=ViewAPIRoute, tags=["robot_settings"])


def _svc() -> RobotCapabilityService:
    return RobotCapabilityService()


@router.get("/robot-settings")
def robot_settings_page(request: Request, user: RequireUser):
    return render_template(request, "app2c/robot_settings.html", active_nav="robot_settings")


@router.get("/api/robot-settings")
def api_robot_settings_status(request: Request, user: RequireUser):
    try:
        return jsonify({"ok": True, **_svc().get_status(get_current_device_id(request))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/asr")
def api_robot_settings_apply_asr(request: Request, user: RequireUser):
    body = get_json(request) or {}
    try:
        status = _svc().apply_asr(str(body.get("provider") or ""), get_current_device_id(request))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/asr/clear-device-override")
def api_robot_settings_clear_device_asr_override(request: Request, user: RequireUser):
    try:
        status = _svc().clear_device_asr_override(get_current_device_id(request))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/llm")
def api_robot_settings_apply_llm(request: Request, user: RequireUser):
    body = get_json(request) or {}
    try:
        status = _svc().apply_llm(str(body.get("provider") or ""), get_current_device_id(request))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/tts")
async def api_robot_settings_apply_tts(request: Request, user: RequireUser):
    body = get_json(request) or {}
    try:
        status = await _svc().apply_tts(str(body.get("provider") or ""), get_current_device_id(request))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/llm/clear-device-override")
def api_robot_settings_clear_device_override(request: Request, user: RequireUser):
    try:
        status = _svc().clear_device_llm_override(get_current_device_id(request))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


ENDPOINTS = {"robot_settings.page": "/robot-settings"}
