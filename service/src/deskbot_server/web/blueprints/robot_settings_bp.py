"""机器人设置：ASR / LLM / TTS 能力查看与热切换（页面 + REST API）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from deskbot_server.service.robot_capability import (
    DEFAULT_ASR_TEST_AUDIO,
    DOUBAO_ASR_FIELDS,
    CapabilityError,
    RobotCapabilityService,
)
from deskbot_server.web.deps import RequireUser
from deskbot_server.web.session_device import get_current_device_id
from deskbot_server.web.view_helpers import (
    ViewAPIRoute,
    files_get,
    form_get,
    get_json,
    jsonify,
    read_upload_bytes,
    render_template,
)

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


@router.get("/api/robot-settings/asr/config-info")
def api_robot_settings_asr_config_info(request: Request, user: RequireUser):
    """ASR 配置对话框元信息：默认测试音频 / funasr 端点 / 豆包 env 当前值。"""
    try:
        return jsonify({"ok": True, **_svc().asr_config_info()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.get("/api/robot-settings/asr/default-audio")
def api_robot_settings_asr_default_audio(request: Request, user: RequireUser):
    """返回默认测试音频 data/test/asr.wav（配置对话框播放试听用）。"""
    from fastapi.responses import FileResponse

    if not DEFAULT_ASR_TEST_AUDIO.is_file():
        return jsonify({"ok": False, "error": "默认音频文件缺失（data/test/asr.wav）"}), 404
    return FileResponse(DEFAULT_ASR_TEST_AUDIO, media_type="audio/wav", filename=DEFAULT_ASR_TEST_AUDIO.name)


@router.post("/api/robot-settings/asr/config")
def api_robot_settings_asr_save_config(request: Request, user: RequireUser):
    """保存豆包 ASR 配置到 .env（掩码/空值不覆盖已有），返回最新 info。"""
    body = get_json(request) or {}
    try:
        return jsonify({"ok": True, **_svc().save_doubao_asr_config(body)})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/asr/test")
async def api_robot_settings_asr_test(request: Request, user: RequireUser):
    """ASR 测试：multipart 上传音频（或 use_default=1 用 data/test/asr.wav）。

    doubao_* 六字段为表单覆盖值（空/掩码回落 env），可不保存直接测试。
    执行结果恒 200 ok:true + success 字段；参数非法才 400。
    """
    upload = files_get(request, "audio")
    raw = read_upload_bytes(upload) if upload is not None else None
    use_default = str(form_get(request, "use_default") or "").lower() in ("1", "true", "on")
    overrides = {k: str(form_get(request, "doubao_" + k) or "") for k in DOUBAO_ASR_FIELDS}
    try:
        result = await _svc().asr_test(
            str(form_get(request, "provider") or ""),
            raw,
            use_default=use_default,
            doubao_overrides=overrides,
        )
        return jsonify(result)
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
        status = await _svc().apply_tts(
            str(body.get("provider") or ""),
            get_current_device_id(request),
            voice_id=str(body.get("voice_id") or "").strip() or None,
        )
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/face")
def api_robot_settings_apply_face(request: Request, user: RequireUser):
    """人脸识别能力切换：none=不识别；insightface=外部独立服务（config.yaml 真源，保存即生效）。"""
    body = get_json(request) or {}
    try:
        status = _svc().apply_face(str(body.get("mode") or ""))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.get("/api/robot-settings/tts/test-info")
def api_robot_settings_tts_test_info(request: Request, user: RequireUser):
    try:
        return jsonify({"ok": True, **_svc().tts_test_info()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/tts/test")
async def api_robot_settings_tts_test(request: Request, user: RequireUser):
    body = get_json(request) or {}
    try:
        result = await _svc().tts_test(
            str(body.get("provider") or ""),
            str(body.get("text") or ""),
            voice_id=str(body.get("voice_id") or "").strip() or None,
        )
        return jsonify({"ok": True, **result})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


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
