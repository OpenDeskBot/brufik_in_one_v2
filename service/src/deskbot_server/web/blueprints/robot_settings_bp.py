"""机器人设置：ASR / LLM / TTS 能力查看与热切换（页面 + REST API）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from deskbot_server.dao.device_mapper import (
    get_auto_reply,
    get_camera_servo_auto_mode,
    normalize_camera_servo_auto_mode,
    set_auto_reply,
    set_camera_servo_auto_mode,
)
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
        device_id = get_current_device_id(request)
        status = _svc().get_status(device_id)
        # 行为开关（设备级，落 devices 表）：自动回复 auto_reply / 人脸跟随 servo_mode
        status["behavior"] = {
            "auto_reply": get_auto_reply(device_id),
            "follow_mode": get_camera_servo_auto_mode(device_id),
        }
        return jsonify({"ok": True, **status})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/behavior")
def api_robot_settings_behavior(request: Request, user: RequireUser):
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
    """ASR 配置对话框元信息：默认测试音频 / funasr 端点 / 豆包当前值（设备 asr_param 优先）。"""
    try:
        return jsonify({"ok": True, **_svc().asr_config_info(get_current_device_id(request))})
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
    """保存设备级 ASR 配置到 device 表 asr_param（JSON，掩码/空值保留已有），不再写全局 .env。"""
    body = get_json(request) or {}
    try:
        return jsonify({"ok": True, **_svc().save_device_asr_config(get_current_device_id(request), body)})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/asr/test")
async def api_robot_settings_asr_test(request: Request, user: RequireUser):
    """ASR 测试：multipart 上传音频（或 use_default=1 用 data/test/asr.wav）。

    doubao_* 四字段为表单覆盖值（空/掩码回落设备 asr_param → env），可不保存直接测试。
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
            device_id=get_current_device_id(request),
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


@router.get("/api/robot-settings/llm/config-info")
def api_robot_settings_llm_config_info(request: Request, user: RequireUser):
    """LLM 配置对话框元信息：ark 当前值（api_key 掩码）；本地模型只读固定端点。"""
    provider = str(request.query_params.get("provider") or "").strip()
    try:
        return jsonify({"ok": True, **_svc().llm_config_info(provider)})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/llm/config")
def api_robot_settings_llm_save_config(request: Request, user: RequireUser):
    """保存 ark 配置：API Key → .env（掩码/空不覆盖已有）；模型/地址 → config.yaml llm 段（快照键不污染当前本地配置）。"""
    body = get_json(request) or {}
    try:
        return jsonify({"ok": True, **_svc().save_llm_config(str(body.get("provider") or ""), body)})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/llm/test")
async def api_robot_settings_llm_test(request: Request, user: RequireUser):
    """LLM 试聊：临时 config 合成（overrides 为表单临时值，不落盘）。执行结果恒 200 ok:true + 字段；参数非法才 400。"""
    body = get_json(request) or {}
    overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
    try:
        result = await _svc().llm_test(
            str(body.get("provider") or ""),
            str(body.get("text") or ""),
            overrides=overrides,
        )
        return jsonify({"ok": True, **result})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@router.post("/api/robot-settings/tts")
def api_robot_settings_apply_tts(request: Request, user: RequireUser):
    """切换 TTS provider（设备级，写 device 表 tts_provider 即生效）。"""
    body = get_json(request) or {}
    try:
        status = _svc().apply_tts(str(body.get("provider") or ""), get_current_device_id(request))
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


@router.post("/api/robot-settings/voiceprint")
def api_robot_settings_apply_voiceprint(request: Request, user: RequireUser):
    """声纹识别能力切换：none=不识别；vpr=外部 wespeaker 独立服务（config.yaml 真源，保存即生效）。"""
    body = get_json(request) or {}
    try:
        status = _svc().apply_voiceprint(str(body.get("mode") or ""))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.get("/api/robot-settings/tts/config-info")
def api_robot_settings_tts_config_info(request: Request, user: RequireUser):
    """TTS 配置对话框元信息：音色列表 + 当前设备参数（api_key 掩码，设备 tts_param 优先回落 .env）。"""
    try:
        return jsonify({"ok": True, **_svc().tts_config_info(get_current_device_id(request))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/tts/config")
def api_robot_settings_tts_save_config(request: Request, user: RequireUser):
    """保存设备级 TTS 参数到 device 表 tts_param（JSON，掩码/空值按回填链保留），不再写全局 .env。"""
    body = get_json(request) or {}
    try:
        return jsonify({"ok": True, **_svc().save_device_tts_config(get_current_device_id(request), body)})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/tts/clear-device-override")
def api_robot_settings_tts_clear_override(request: Request, user: RequireUser):
    """重置设备 TTS：provider 回落 moss-tts-nano，并清空设备级参数。"""
    try:
        status = _svc().clear_device_tts_override(get_current_device_id(request))
        return jsonify({"ok": True, **status})
    except CapabilityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.get("/api/robot-settings/tts/test-info")
def api_robot_settings_tts_test_info(request: Request, user: RequireUser):
    try:
        return jsonify({"ok": True, **_svc().tts_test_info(get_current_device_id(request))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@router.post("/api/robot-settings/tts/test")
async def api_robot_settings_tts_test(request: Request, user: RequireUser):
    """TTS 合成测试：设备参数优先，overrides / voice_id 为临时覆盖（不落盘）。

    body: {provider, text, voice_id?, overrides?: {api_key, speaker, ...}}。
    执行结果恒 200 ok:true + 合成字段；参数非法才 400。
    """
    body = get_json(request) or {}
    overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
    try:
        result = await _svc().tts_test(
            str(body.get("provider") or ""),
            str(body.get("text") or ""),
            device_id=get_current_device_id(request),
            voice_id=str(body.get("voice_id") or "").strip() or None,
            overrides=overrides,
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
