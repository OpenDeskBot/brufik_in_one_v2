from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time

from fastapi import APIRouter, Request

from deskbot_server.auth.debug_ws_token import issue_debug_ws_token
from deskbot_server.auth.permissions import RequireDeveloper
from deskbot_server.constants import CAMERA_FACE_CFG_FILE, SCENE_PLAYBOOKS_FILE, USER_MEMORY_FILE
from deskbot_server.controller.decorators import access
from deskbot_server.dao.camera_face_config_store import (
    load_camera_face_cfg_file,
    normalize_camera_face_document,
    save_camera_face_cfg_file,
)
from deskbot_server.dao.scene_playbooks_store import (
    collect_missing_expression_scenes,
    collect_missing_servo_presets,
    load_scene_playbooks_file,
    normalize_scene_playbooks,
    save_scene_playbooks_file,
)
from deskbot_server.service.application.face_registration import register_face_for_device
from deskbot_server.service.application.voice_registration import register_voice_for_device
from deskbot_server.service.device_data_paths import load_llm_system_prompt, resolve_json_path, save_llm_system_prompt
from deskbot_server.service.face_profile_service import load_face_profiles
from deskbot_server.service.memory_service import add_memory, delete_memory, list_memory_for_device
from deskbot_server.service.user_service import UserService
from deskbot_server.service.voice_profile_service import load_voice_profiles
from deskbot_server.utils.util import pcm_to_wav_bytes
from deskbot_server.vision.camera_face_tune import apply_camera_face_tune
from deskbot_server.web.deps import RequireUser
from deskbot_server.web.helpers import (
    ALLOWED_LLM_ROLES,
    camera_view_ws_base,
    deskbot_http_base,
    device_pipeline_ws_base,
    load_config,
    tcp_alive,
)
from deskbot_server.web.pages import page
from deskbot_server.web.session_device import get_current_device_id
from deskbot_server.web.view_helpers import ViewAPIRoute, get_json, jsonify, redirect, render_template

logger = logging.getLogger("deskbot-server")


router = APIRouter(route_class=ViewAPIRoute, tags=["debug"])


def _deny_foreign_device(request: Request, device_id: str, user):
    did = (device_id or "").strip()
    if not did:
        return None
    if not UserService().user_owns_device(user.id, did):
        return jsonify({"ok": False, "error": "无权操作该设备"}), 403
    return None


def _effective_device_id(request: Request, *, required: bool = True) -> str | None:
    """调试页当前设备：query/body 优先，否则 session。"""
    did = str(request.query_params.get("device_id") or "").strip()
    if not did and request.method != "GET":
        payload = get_json(request, silent=True)
        if isinstance(payload, dict):
            did = str(payload.get("device_id") or "").strip()
    if not did:
        did = get_current_device_id(request) or ""
    if not did:
        return None if not required else None
    return did or None


def _require_device_id(request: Request, user):
    did = _effective_device_id(request, required=True)
    if not did:
        return None, (jsonify({"ok": False, "error": "请先选择设备", "t": time.time()}), 400)
    denied = _deny_foreign_device(request, did, user)
    if denied:
        return None, denied
    return did, None


def _optional_device_id(request: Request, user):
    did = _effective_device_id(request, required=False)
    if not did:
        return None, None
    denied = _deny_foreign_device(request, did, user)
    if denied:
        return None, denied
    return did, None


@router.get("/api/debug/ws_token")
@access('user')
def api_debug_ws_token(request: Request, user: RequireUser):
    """已登录用户获取调试台 WebSocket 令牌（默认 7 天有效）。"""
    info = issue_debug_ws_token(user.id)
    return jsonify({"ok": True, "token": info.token, "expires_in": info.expires_in})


@router.get("/debug/users")
@access('developer')
@page('debug_users.html')
def debug_users(request: Request, user: RequireDeveloper):
    rows = UserService().list_users()
    return render_template(request, "debug_users.html", active_nav="users", users=rows, current_user_id=user.id)


@router.get("/dev/users")
@access('developer')
@page('app2c/users.html')
def dev_users_page(request: Request, user: RequireDeveloper):
    """2C 后台「开发者选项 → 用户管理」页面。"""
    rows = UserService().list_users()
    return render_template(request, "app2c/users.html", active_nav="users", users=rows, current_user_id=user.id)


@router.post("/api/debug/users/{user_id}/developer")
@access('developer')
def api_set_user_developer(request: Request, user: RequireDeveloper, user_id: str):
    payload = get_json(request, silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400
    try:
        is_developer = bool(payload.get("is_developer"))
        user = UserService().set_developer(user_id, is_developer=is_developer)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "user": {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "is_developer": user.is_developer,
                "is_active": user.is_active,
            },
        }
    )


@router.get("/debug/devices")
@access('developer')
@page('debug_devices.html')
def debug_devices(request: Request, user: RequireUser):
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH
    from deskbot_server.pb.shapes import _default_mouth_fallback_shape, default_face_circles

    fc = default_face_circles()
    mouth_fb = _default_mouth_fallback_shape().get("elements") or []
    expr_default_anim = {
        "elements": {
            "nose": fc.get("nose") or [],
            "eye_l": fc.get("eye_l") or [],
            "eye_r": fc.get("eye_r") or [],
            "mouth": mouth_fb,
            "extra": [],
        }
    }

    owned = UserService().list_devices(user.id)
    owned_device_rows = [{"device_id": d.device_id, "display_name": d.display_name or d.device_id} for d in owned]
    current = get_current_device_id(request)
    owned_ids = {d.device_id for d in owned}
    if current and current not in owned_ids:
        current = owned[0].device_id if owned else None

    return render_template(
        request,
        "debug_devices.html",
        active_nav="devices",
        device_pipeline_ws_base=device_pipeline_ws_base(request),
        camera_view_ws_base=camera_view_ws_base(request),
        deskbot_http_base=deskbot_http_base(),
        face_lcd_w=FACE_LCD_WIDTH,
        face_lcd_h=FACE_LCD_HEIGHT,
        expr_default_anim=expr_default_anim,
        current_device_id=current or "",
        owned_device_rows=owned_device_rows,
    )


@router.get("/debug/tts")
@access('developer')
@page('debug_tts.html')
def debug_tts(request: Request, user: RequireUser):
    from deskbot_server.client.tts.doubao import load_doubao_tts_config
    from deskbot_server.client.tts.speakers import list_doubao_tts_speaker_presets

    cfg = load_doubao_tts_config()
    initial = {
        "api_key": "",
        "api_key_set": bool(cfg.api_key),
        "speaker": cfg.speaker,
        "resource_id": cfg.resource_id,
        "model": cfg.model,
        "ws_url": cfg.ws_url,
        "sample_rate": cfg.sample_rate,
        "audio_format": cfg.audio_format,
    }
    return render_template(
        request,
        "debug_tts.html",
        active_nav="tts",
        initial_config=initial,
        speaker_presets=list_doubao_tts_speaker_presets(),
    )


@router.get("/api/doubao_tts/speakers")
@access('user')
def api_doubao_tts_speakers(request: Request, user: RequireUser):
    from deskbot_server.client.tts.speakers import (
        list_doubao_tts_consumer_speaker_presets,
        list_doubao_tts_speaker_presets,
    )

    if request.query_params.get("scope") == "consumer":
        speakers = list_doubao_tts_consumer_speaker_presets()
    else:
        speakers = list_doubao_tts_speaker_presets()
    return jsonify({"ok": True, "speakers": speakers, "t": time.time()})


@router.get("/api/doubao_tts/config")
@access('user')
def api_doubao_tts_config_get(request: Request, user: RequireUser):
    from deskbot_server.client.tts.doubao import load_doubao_tts_config

    cfg = load_doubao_tts_config()
    return jsonify({"ok": True, "config": cfg.masked(), "t": time.time()})


@router.post("/api/doubao_tts/config")
@access('user')
def api_doubao_tts_config_post(request: Request, user: RequireUser):
    from deskbot_server.client.tts.doubao import load_doubao_tts_config, resolve_optional_secret
    from deskbot_server.client.tts.env_store import save_doubao_tts_env

    payload = get_json(request, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400

    base = load_doubao_tts_config()
    api_key = resolve_optional_secret(payload.get("api_key"), base.api_key)
    if not api_key:
        return jsonify({"ok": False, "error": "DOUBAO_TTS_API_KEY 不能为空"}), 400
    try:
        save_doubao_tts_env(payload)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    cfg = load_doubao_tts_config()
    return jsonify({"ok": True, "config": cfg.masked(), "t": time.time()})


def _doubao_cfg_from_payload(payload: dict):
    from deskbot_server.client.tts.doubao import DoubaoTtsConfig, load_doubao_tts_config, resolve_optional_secret

    base = load_doubao_tts_config()
    api_key = resolve_optional_secret(payload.get("api_key"), base.api_key)
    sample_rate_raw = payload.get("sample_rate", base.sample_rate)
    try:
        sample_rate = int(sample_rate_raw)
    except (TypeError, ValueError):
        sample_rate = base.sample_rate
    return DoubaoTtsConfig(
        api_key=api_key,
        speaker=str(payload.get("speaker") or base.speaker).strip(),
        resource_id=str(payload.get("resource_id") or base.resource_id).strip(),
        model=str(payload.get("model") or base.model).strip(),
        ws_url=str(payload.get("ws_url") or base.ws_url).strip(),
        sample_rate=sample_rate,
        audio_format=str(payload.get("audio_format") or base.audio_format).strip(),
    )


@router.post("/api/doubao_tts/synthesize")
@access('user')
def api_doubao_tts_synthesize(request: Request, user: RequireUser):
    from deskbot_server.client.tts.doubao import synthesize_doubao_tts

    payload = get_json(request, force=True, silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "空文本"}), 400
    cfg = _doubao_cfg_from_payload(payload)
    t0 = time.monotonic()
    try:
        result = asyncio.run(synthesize_doubao_tts(text, cfg))
    except ValueError as exc:
        logger.warning("豆包 TTS 参数错误: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.exception(
            "豆包 TTS 合成失败 text_len=%d speaker=%r resource_id=%s elapsed_ms=%d",
            len(text),
            cfg.speaker,
            cfg.resource_id,
            elapsed_ms,
        )
        return jsonify({"ok": False, "error": str(exc), "elapsed_ms": elapsed_ms}), 502
    wav = pcm_to_wav_bytes(result.pcm, result.sample_rate)
    return jsonify(
        {
            "ok": True,
            "sample_rate": result.sample_rate,
            "pcm_total_bytes": len(result.pcm),
            "elapsed_ms": result.elapsed_ms,
            "events": result.events,
            "speaker": cfg.speaker,
            "ws_url": cfg.ws_url,
            "wav_base64": base64.b64encode(wav).decode("ascii"),
        }
    )


@router.get("/debug/llm")
@access('developer')
@page('debug_llm.html')
def debug_llm(request: Request, user: RequireUser):
    from deskbot_server.service.application.action_decision import build_systemone_payload
    from deskbot_server.service.llm_context import build_action_decision_prompt
    from deskbot_server.service.llm_routing import resolve_device_decision_provider

    cfg = load_config()
    llm_cfg = cfg.get("llm", {}) or {}
    owned = UserService().list_devices(user.id)
    owned_device_rows = [{"device_id": d.device_id, "display_name": d.display_name or d.device_id} for d in owned]
    current = get_current_device_id(request)
    owned_ids = {d.device_id for d in owned}
    if current and current not in owned_ids:
        current = owned[0].device_id if owned else None
    initial_prompt = load_llm_system_prompt(current) if current else load_llm_system_prompt()
    try:
        if current and resolve_device_decision_provider(current) == "unee":
            payload = build_systemone_payload(
                device_id=current, user_text="<用户话>", reply_text="<机器人回复>"
            )
            decision_appendix = (
                "unee /v1/systemone choice 决策请求（state = 用户话 + 机器人回复）：\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
                if payload
                else ""
            )
        else:
            decision_appendix = build_action_decision_prompt(device_id=current or None)
    except Exception as exc:  # 页面预览失败不影响调试页
        decision_appendix = f"（决策模型 prompt 预览失败：{exc}）"

    return render_template(
        request,
        "debug_llm.html",
        active_nav="llm",
        llm_model=llm_cfg.get("model_name", ""),
        llm_base_url=llm_cfg.get("base_url", ""),
        llm_system_prompt=initial_prompt,
        llm_plan_appendix=decision_appendix,
        current_device_id=current or "",
        owned_device_rows=owned_device_rows,
    )


@router.get("/debug/simulation")
@access('developer')
def debug_simulation(request: Request, user: RequireUser):
    """Keep old bookmarks usable after retiring the phoneme simulator."""
    return redirect("/debug/devices")


@router.post("/api/tts/phoneme_tts")
@access('user')
def api_tts_phoneme_tts(request: Request, user: RequireUser):
    """兼容旧地址的 TTS 音频合成接口；分片不再生成音素标签。"""
    from deskbot_server.client.tts.factory import build_tts_adapter
    from deskbot_server.model.settings import AppSettings

    payload = get_json(request, force=True, silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "空文本"}), 400
    cfg = load_config()
    provider = str((cfg.get("tts") or {}).get("provider") or "moss-tts-nano")
    sr_cfg = int((cfg.get("tts") or {}).get("sample_rate", 48000))
    try:
        settings = AppSettings.from_config(cfg)
        adapter = build_tts_adapter(settings)
        sr, segs = asyncio.run(adapter.synthesize_phoneme_segments(text))
    except Exception as exc:  # pragma: no cover
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
    pcm = bytearray()
    display: list[dict] = []
    for seg in segs:
        chunk = bytes(seg.pcm or b"")
        pcm.extend(chunk)
        display.append({"phoneme_id": seg.phoneme_id, "phoneme": seg.phoneme, "ms": seg.ms, "pcm_bytes": len(chunk)})
    sr = int(sr or sr_cfg or 48000)
    wav = pcm_to_wav_bytes(bytes(pcm), sr)
    return jsonify(
        {
            "ok": True,
            "provider": provider,
            "sample_rate": sr,
            "segments": display,
            "wav_base64": base64.b64encode(wav).decode("ascii"),
            "pcm_total_bytes": len(pcm),
        }
    )


@router.get("/api/llm/system_prompt")
@access('user')
def api_llm_system_prompt_get(request: Request, user: RequireUser):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    return jsonify(
        {
            "ok": True,
            "device_id": device_id,
            "system_prompt": load_llm_system_prompt(device_id),
            "file": "llm_system.txt",
            "t": time.time(),
        }
    )


@router.post("/api/llm/system_prompt")
@access('user')
def api_llm_system_prompt_post(request: Request, user: RequireUser):
    payload = get_json(request, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object", "t": time.time()}), 400
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    content = str(payload.get("system_prompt") or payload.get("content") or "")
    try:
        path = save_llm_system_prompt(content, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "device_id": device_id,
            "system_prompt": load_llm_system_prompt(device_id),
            "file": os.path.basename(path),
            "t": time.time(),
        }
    )


@router.post("/api/llm/chat")
@access('user')
def llm_chat(request: Request, user: RequireUser):
    payload = get_json(request, force=True, silent=True) or {}
    user_text = str(payload.get("text") or "").strip()
    raw_history = payload.get("history") or []

    if not user_text:
        return jsonify({"ok": False, "error": "空文本"}), 400

    cfg = load_config()
    llm_cfg = cfg.get("llm", {}) or {}
    debug_device_id = str(payload.get("device_id") or "").strip()
    if debug_device_id:
        denied = _deny_foreign_device(request, debug_device_id, user)
        if denied:
            return denied

    default_system_prompt = load_llm_system_prompt(debug_device_id or None) or llm_cfg.get(
        "system_prompt", "你是中文助手，请简洁回答。每次回答不超过50字"
    )
    raw_sys = payload.get("system_prompt")
    system_prompt = raw_sys if isinstance(raw_sys, str) and raw_sys.strip() else default_system_prompt

    from deskbot_server.client.llm.runtime import api_key_error_message
    from deskbot_server.service.llm_routing import resolve_llm_config

    try:
        llm_runtime_cfg = resolve_llm_config(debug_device_id or None)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    key_error = api_key_error_message(llm_runtime_cfg)
    if key_error:
        return jsonify({"ok": False, "error": key_error}), 400

    from deskbot_server.service.llm_context import build_llm_system_prompt, build_llm_user_message

    sys_content = build_llm_system_prompt(system_prompt, device_id=debug_device_id or None)

    raw_ctx = payload.get("device_context")
    device_context: str | None = None
    if isinstance(raw_ctx, dict):
        device_context = json.dumps(raw_ctx, ensure_ascii=False)
    elif isinstance(raw_ctx, str) and raw_ctx.strip():
        device_context = raw_ctx.strip()

    messages = [{"role": "system", "content": sys_content}]
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "")
        if role not in ALLOWED_LLM_ROLES or not content:
            continue
        if role == "system":
            continue
        messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": build_llm_user_message(
                user_text, device_id=debug_device_id or None, device_context=device_context
            ),
        }
    )

    try:
        from deskbot_server.client.llm.runtime import chat_completion

        t0 = time.monotonic()
        raw, meta = chat_completion(
            messages,
            device_id=debug_device_id or None,
            temperature=float(payload.get("temperature", 0.7)),
            config=llm_runtime_cfg,
            json_mode=False,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        # 对话模型已改为纯文本协议：原文即要说的话；动作/表情由独立决策模型负责
        reply = str(raw or "").strip()
        return jsonify(
            {
                "ok": True,
                "reply": reply,
                "raw": raw,
                "moves": [],
                "anims": [],
                "servo": [],
                "scenes": [],
                "json_ok": True,
                "need_reply": bool(reply),
                "model": meta.get("model"),
                "model_source": meta.get("source"),
                "model_display_name": meta.get("display_name"),
                "elapsed_ms": elapsed_ms,
                "usage": meta.get("usage"),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


def _json_object_from_llm(raw: str) -> dict:
    text = str(raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM did not return a JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON must be an object")
    return parsed


def _normalize_generated_design(raw: dict) -> dict:
    def _items(value: object) -> list[dict]:
        if not isinstance(value, list):
            return []
        out: list[dict] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            frames = item.get("frames")
            if not isinstance(frames, list):
                frames = []
            clean_frames: list[dict] = []
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                elements = frame.get("elements")
                if not isinstance(elements, dict):
                    continue
                try:
                    ms = int(frame.get("ms") or 320)
                except (TypeError, ValueError):
                    ms = 320
                clean_frames.append({"ms": max(40, min(ms, 5000)), "elements": elements})
            name = str(item.get("name") or item.get("title") or "").strip()
            if not name or not clean_frames:
                continue
            alias = item.get("alias") if isinstance(item.get("alias"), list) else []
            out.append(
                {
                    "name": name[:64],
                    "title": str(item.get("title") or name).strip()[:80],
                    "alias": [str(v).strip() for v in alias if str(v).strip()],
                    "frames": clean_frames,
                }
            )
        return out

    design = {
        "name": str(raw.get("name") or raw.get("title") or "ai-face-design").strip()[:80],
        "description": str(raw.get("description") or "").strip()[:240],
        "phonemes": _items(raw.get("phonemes")),
        "emotions": _items(raw.get("emotions") if isinstance(raw.get("emotions"), list) else raw.get("scenes")),
    }
    if not design["phonemes"] and not design["emotions"]:
        raise ValueError("generated design has no usable phonemes or emotions")
    return design


@router.post("/api/face_design/generate")
@access('developer')
def api_face_design_generate(request: Request, user: RequireUser):
    device_id, err = _optional_device_id(request, user)
    if err:
        return err
    payload = get_json(request, silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "请先输入想要的表情风格", "t": time.time()}), 400
    try:
        temperature = float(payload.get("temperature", 0.7))
    except (TypeError, ValueError):
        temperature = 0.7
    temperature = max(0.1, min(temperature, 1.2))

    system_prompt = (
        "你是 Deskbot 小歪的 VisemeSync JSON 表情设计助手。"
        "只输出一个 JSON object，不要 Markdown，不要解释。"
        "JSON schema: {name, description, phonemes, emotions}。"
        "phonemes/emotions 的每一项必须包含 name, title, alias[], frames[]；"
        "frames[] 每项包含 ms 和 elements；elements 可以包含 mouth/eye_l/eye_r/nose/extra 数组。"
        "坐标范围基于 284x240 OLED，嘴部大致在 x=90..194, y=135..180。"
        "至少生成 5 个 emotions 和 8 个常用中文拼音/音素 phonemes。"
    )
    user_prompt = (
        "根据这段描述生成 VisemeSync JSON：\n"
        f"{prompt}\n\n"
        "优先使用这些 shape: ellipse_fill, circle_fill, line, round_rect_outline, round_rect_fill。"
        "请让表情适合 2C 用户配置，名字简短稳定，alias 可以包含中文拼音或英文 mood。"
    )

    try:
        from deskbot_server.client.llm.runtime import chat_completion

        t0 = time.monotonic()
        raw, meta = chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            device_id=device_id,
            temperature=temperature,
            json_mode=True,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        design = _normalize_generated_design(_json_object_from_llm(raw))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    except json.JSONDecodeError as exc:
        return jsonify({"ok": False, "error": f"LLM JSON 解析失败: {exc}", "t": time.time()}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}", "t": time.time()}), 500

    return jsonify(
        {
            "ok": True,
            "design": design,
            "raw": raw,
            "model": meta.get("model"),
            "model_source": meta.get("source"),
            "model_display_name": meta.get("display_name"),
            "usage": meta.get("usage"),
            "elapsed_ms": elapsed_ms,
            "device_id": device_id or "",
            "t": time.time(),
        }
    )


@router.get("/api/camera_face_config")
@access('developer')
def api_camera_face_config_get(request: Request, user: RequireUser):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    cfg_path = resolve_json_path(CAMERA_FACE_CFG_FILE, device_id)
    cfg = load_config()
    base = dict(cfg.get("camera_face") or {})
    try:
        file_cfg = load_camera_face_cfg_file(device_id=device_id)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    merged = {**base, **(file_cfg or {})}
    try:
        norm = normalize_camera_face_document(merged)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "config": norm,
            "file": os.path.basename(cfg_path),
            "exists": file_cfg is not None,
            "device_id": device_id,
            "t": time.time(),
        }
    )


@router.post("/api/camera_face_config")
@access('developer')
def api_camera_face_config_post(request: Request, user: RequireUser):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    payload = get_json(request, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object", "t": time.time()}), 400
    cfg_path = resolve_json_path(CAMERA_FACE_CFG_FILE, device_id)
    try:
        cfg = normalize_camera_face_document(payload)
        save_camera_face_cfg_file(cfg, device_id=device_id)
        apply_camera_face_tune(cfg)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "config": cfg,
            "file": os.path.basename(cfg_path),
            "device_id": device_id,
            "hint": "检测器 num_faces/置信度需 ESP32 重连 /asr_chat 后生效",
            "t": time.time(),
        }
    )


@router.get("/api/face_profiles")
@access('developer')
def api_face_profiles_get(request: Request, user: RequireUser):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    try:
        profiles = load_face_profiles(device_id=device_id)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {"ok": True, "profiles": profiles, "device_id": device_id, "t": time.time()}
    )


@router.post("/api/face_profiles/register")
@access('developer')
def api_face_profiles_register(request: Request, user: RequireUser):
    payload = get_json(request, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object", "t": time.time()}), 400
    name = str(payload.get("name") or "").strip()
    device_id = str(payload.get("device_id") or "").strip()
    face_id_raw = payload.get("face_id")
    if not name:
        return jsonify({"ok": False, "error": "name required", "t": time.time()}), 400
    if not device_id or face_id_raw is None:
        return jsonify({"ok": False, "error": "device_id and face_id required", "t": time.time()}), 400
    denied = _deny_foreign_device(request, device_id, user)
    if denied:
        return denied
    try:
        face_id = int(face_id_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "face_id must be int", "t": time.time()}), 400
    try:
        out = register_face_for_device(device_id, name, face_id=face_id, extra=payload)
        profile = out["profile"]
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    return jsonify(
        {
            "ok": True,
            "profile": profile,
            "device_id": device_id,
            "hint": (
                "档案已写入（InsightFace 512 维）；请 ESP32 重连 /asr_chat 后正对镜头识别。旧几何档案需重新「保存人名」"
            ),
            "t": time.time(),
        }
    )


@router.get("/api/voice_profiles")
@access('developer')
def api_voice_profiles_get(request: Request, user: RequireUser):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    try:
        profiles = load_voice_profiles(device_id=device_id)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {"ok": True, "profiles": profiles, "device_id": device_id, "t": time.time()}
    )


@router.post("/api/voice_profiles/register")
@access('developer')
def api_voice_profiles_register(request: Request, user: RequireUser):
    """用最近一次 VAD 语音注册声纹档案（样本 = 最近一次成功抽出 embedding 的 utterance）。"""
    payload = get_json(request, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object", "t": time.time()}), 400
    name = str(payload.get("name") or "").strip()
    device_id = str(payload.get("device_id") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required", "t": time.time()}), 400
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required", "t": time.time()}), 400
    denied = _deny_foreign_device(request, device_id, user)
    if denied:
        return denied
    try:
        out = register_voice_for_device(device_id, name)
        profile = out["profile"]
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    return jsonify(
        {
            "ok": True,
            "profile": profile,
            "device_id": device_id,
            "hint": (
                "声纹档案已写入（WeSpeaker 256 维）；样本来自最近一次 VAD 语音，"
                "请让该用户再对机器人说几句话验证识别"
            ),
            "t": time.time(),
        }
    )


@router.get("/api/user_memory")
@access('developer')
def api_user_memory_get(request: Request, user: RequireUser):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    cfg_path = resolve_json_path(USER_MEMORY_FILE, device_id)
    try:
        entries = list_memory_for_device(device_id)
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {"ok": True, "entries": entries, "file": os.path.basename(cfg_path), "device_id": device_id, "t": time.time()}
    )


@router.post("/api/user_memory")
@access('developer')
def api_user_memory_post(request: Request, user: RequireUser):
    payload = get_json(request, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object", "t": time.time()}), 400
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text required", "t": time.time()}), 400
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    try:
        entry = add_memory(text, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    return jsonify({"ok": True, "entry": entry, "t": time.time()})


@router.delete("/api/user_memory/{entry_id}")
@access('developer')
def api_user_memory_delete(request: Request, user: RequireUser, entry_id: str):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    try:
        ok = delete_memory(entry_id, device_id=device_id)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    if not ok:
        return jsonify({"ok": False, "error": "not found", "t": time.time()}), 404
    return jsonify({"ok": True, "id": entry_id, "t": time.time()})


@router.get("/api/scene_playbooks")
@access('developer')
def api_scene_playbooks_get(request: Request, user: RequireUser):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    cfg_path = resolve_json_path(SCENE_PLAYBOOKS_FILE, device_id)
    try:
        rows = load_scene_playbooks_file(seed_if_missing=True, device_id=device_id)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    return jsonify(
        {
            "ok": True,
            "config": rows or [],
            "exists": os.path.isfile(cfg_path),
            "file": os.path.basename(cfg_path),
            "device_id": device_id,
            "t": time.time(),
        }
    )


@router.post("/api/scene_playbooks")
@access('developer')
def api_scene_playbooks_post(request: Request, user: RequireUser):
    device_id, err = _require_device_id(request, user)
    if err:
        return err
    payload = get_json(request, silent=True)
    cfg_path = resolve_json_path(SCENE_PLAYBOOKS_FILE, device_id)
    try:
        rows = normalize_scene_playbooks(payload if payload is not None else [])
        save_scene_playbooks_file(rows, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc), "t": time.time()}), 500
    missing = collect_missing_servo_presets(rows, device_id=device_id)
    missing_expr = collect_missing_expression_scenes(rows, device_id=device_id)
    out = {"ok": True, "config": rows, "file": os.path.basename(cfg_path), "device_id": device_id, "t": time.time()}
    if missing:
        out["missing_servo_presets"] = missing
        out["warning"] = "部分 pb 包引用的舵机 preset 未写入 servo.json，设备下发时会跳过：" + ", ".join(missing)
    if missing_expr:
        out["missing_expression_scenes"] = missing_expr
        expr_warning = "部分 pb 包引用的表情场景不存在，设备下发时会跳过：" + ", ".join(missing_expr)
        out["warning"] = (str(out.get("warning") or "") + "；" + expr_warning).lstrip("；")
    return jsonify(out)


@router.get("/api/health")
@access('user')
def health(request: Request):
    cfg = load_config()
    deskbot_host = os.environ.get("DESKBOT_SERVER_HOST") or cfg.get("server", {}).get("host", "127.0.0.1")
    if deskbot_host == "0.0.0.0":
        deskbot_host = "127.0.0.1"
    deskbot_port = int(os.environ.get("DESKBOT_SERVER_PORT") or cfg.get("server", {}).get("port", 9000))

    from deskbot_server.client.tts.doubao import load_doubao_tts_config

    tts_cfg = load_doubao_tts_config()
    tts_provider = str((cfg.get("tts") or {}).get("provider") or "moss-tts-nano").strip().lower()
    tts_configured = bool(str(tts_cfg.api_key or "").strip())

    return jsonify(
        {
            "deskbot_server": tcp_alive(deskbot_host, deskbot_port),
            "tts_provider": tts_provider,
            "tts_configured": tts_configured,
            "deskbot_target": f"{deskbot_host}:{deskbot_port}",
            "tts_target": tts_cfg.ws_url,
        }
    )


ENDPOINTS = {
    "debug.api_debug_ws_token": "/api/debug/ws_token",
    "debug.debug_users": "/debug/users",
    "debug.dev_users_page": "/dev/users",
    "debug.api_set_user_developer": "/api/debug/users/{user_id}/developer",
    "debug.debug_devices": "/debug/devices",
    "debug.debug_tts": "/debug/tts",
    "debug.api_doubao_tts_speakers": "/api/doubao_tts/speakers",
    "debug.api_doubao_tts_config_get": "/api/doubao_tts/config",
    "debug.api_doubao_tts_config_post": "/api/doubao_tts/config",
    "debug.api_doubao_tts_synthesize": "/api/doubao_tts/synthesize",
    "debug.debug_llm": "/debug/llm",
    "debug.debug_simulation": "/debug/simulation",
    "debug.api_tts_phoneme_tts": "/api/tts/phoneme_tts",
    "debug.api_llm_system_prompt_get": "/api/llm/system_prompt",
    "debug.api_llm_system_prompt_post": "/api/llm/system_prompt",
    "debug.llm_chat": "/api/llm/chat",
    "debug.api_face_design_generate": "/api/face_design/generate",
    "debug.api_camera_face_config_get": "/api/camera_face_config",
    "debug.api_camera_face_config_post": "/api/camera_face_config",
    "debug.api_face_profiles_get": "/api/face_profiles",
    "debug.api_face_profiles_register": "/api/face_profiles/register",
    "debug.api_voice_profiles_get": "/api/voice_profiles",
    "debug.api_voice_profiles_register": "/api/voice_profiles/register",
    "debug.api_user_memory_get": "/api/user_memory",
    "debug.api_user_memory_post": "/api/user_memory",
    "debug.api_user_memory_delete": "/api/user_memory/{entry_id}",
    "debug.api_scene_playbooks_get": "/api/scene_playbooks",
    "debug.api_scene_playbooks_post": "/api/scene_playbooks",
    "debug.health": "/api/health",
}
