from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Request

from deskbot_server.auth.permissions import RequireDeveloper
from deskbot_server.dao.emotion_expr_map_store import load_emotion_expr_map, save_emotion_expr_map
from deskbot_server.dao.face_expr_scenes_store import load_face_expr_scenes_file, save_face_expr_scenes_file
from deskbot_server.dao.face_mouth_config_store import load_face_mouth_cfg_file, save_face_mouth_cfg_file
from deskbot_server.service.user_service import UserService
from deskbot_server.web.deps import RequireUser
from deskbot_server.web.helpers import camera_view_ws_base, device_pipeline_ws_base
from deskbot_server.web.session_device import get_current_device_id
from deskbot_server.web.view_helpers import (
    ViewAPIRoute,
    files_get,
    form_get,
    get_json,
    is_json_request,
    jsonify,
    render_template,
)

# No url_prefix: 2C consumer routes live at root (/home, /my/*)
router = APIRouter(route_class=ViewAPIRoute, tags=["app2c"])


def _default_robot_face_payload() -> dict:
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH
    from deskbot_server.pb.shapes import _default_mouth_fallback_shape, default_face_circles

    face = default_face_circles()
    return {
        "face_lcd_w": FACE_LCD_WIDTH,
        "face_lcd_h": FACE_LCD_HEIGHT,
        "expr_default_anim": {
            "elements": {
                "nose": face.get("nose") or [],
                "eye_l": face.get("eye_l") or [],
                "eye_r": face.get("eye_r") or [],
                "mouth": (_default_mouth_fallback_shape().get("elements") or []),
                "extra": [],
            }
        },
    }


@router.get("/home")
def home(request: Request, user: RequireUser):
    return render_template(
        request,
        "app2c/home.html",
        active_nav="home",
        camera_view_ws_base=camera_view_ws_base(request),
        **_default_robot_face_payload(),
    )


@router.get("/expr")
def expr(request: Request, user: RequireDeveloper):
    """表情设计：开发者选项下，仅开发者可进入（导航隐藏不是门禁）。"""
    return render_template(request, "app2c/expr.html", active_nav="expr")


@router.get("/lab")
def lab(request: Request, user: RequireUser):
    return render_template(
        request,
        "app2c/lab.html",
        active_nav="lab",
        device_pipeline_ws_base=device_pipeline_ws_base(request),
    )


@router.get("/my/memories")
def memories(request: Request, user: RequireUser):
    return render_template(request, "app2c/memories.html", active_nav="memory")


@router.get("/my/reminders")
def reminders(request: Request, user: RequireUser):
    return render_template(request, "app2c/reminders.html", active_nav="remind")


@router.get("/my/people")
def people(request: Request, user: RequireUser):
    return render_template(request, "app2c/people.html", active_nav="people")


@router.get("/my/devices")
def devices(request: Request, user: RequireUser):
    return render_template(request, "app2c/devices.html", active_nav="device")


@router.get("/my/miot")
def miot(request: Request, user: RequireUser):
    return render_template(request, "app2c/miot.html", active_nav="miot")


@router.get("/advanced")
def advanced(request: Request, user: RequireUser):
    return render_template(request, "app2c/advanced.html", active_nav="advanced")


@router.get("/api/advanced")
def advanced_summary_get(request: Request, user: RequireUser):
    user = UserService().get_user(user.id)

    return jsonify(
        {
            "ok": True,
            "user": {
                "email": getattr(user, "email", "") if user else "",
                "display_name": getattr(user, "display_name", "") if user else "",
            },
        }
    )


@router.patch("/api/advanced/profile")
def advanced_profile_patch(request: Request, user: RequireUser):
    payload = get_json(request, silent=True) or {}
    try:
        UserService().update_display_name(user.id, str(payload.get("display_name") or ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    user = UserService().get_user(user.id)
    return jsonify(
        {
            "ok": True,
            "user": {"email": getattr(user, "email", ""), "display_name": getattr(user, "display_name", "") or ""},
        }
    )


@router.post("/api/advanced/password")
def advanced_password_post(request: Request, user: RequireUser):
    payload = get_json(request, silent=True) or {}
    old_password = str(payload.get("old_password") or "")
    new_password = str(payload.get("new_password") or "")
    confirm = str(payload.get("confirm_password") or "")
    if new_password != confirm:
        return jsonify({"ok": False, "error": "两次新密码不一致"}), 400
    try:
        UserService().change_password(user.id, old_password, new_password)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


def _owned_device_or_error(request: Request, user):
    device_id = (request.query_params.get("device_id") or "").strip()
    if not device_id and is_json_request(request):
        payload = get_json(request, silent=True) or {}
        if isinstance(payload, dict):
            device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        device_id = str(form_get(request, "device_id") or "").strip()
    if not device_id:
        device_id = (get_current_device_id(request) or "").strip()
    if not device_id:
        return None, (jsonify({"ok": False, "error": "请先选择设备"}), 400)
    if not UserService().user_owns_device(user.id, device_id):
        return None, (jsonify({"ok": False, "error": "设备不属于当前账号"}), 403)
    return device_id, None


def _optional_owned_device_or_error(request: Request, user):
    device_id = (request.query_params.get("device_id") or "").strip()
    if not device_id and is_json_request(request):
        payload = get_json(request, silent=True) or {}
        if isinstance(payload, dict):
            device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        device_id = str(form_get(request, "device_id") or "").strip()
    if not device_id:
        device_id = (get_current_device_id(request) or "").strip()
    if not device_id:
        return "", None
    if not UserService().user_owns_device(user.id, device_id):
        return None, (jsonify({"ok": False, "error": "设备不属于当前账号"}), 403)
    return device_id, None


def _image_mime_from_upload(filename: str, content_type: str, image_bytes: bytes) -> str:
    mime_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if mime_type.startswith("image/"):
        return mime_type
    guessed = mimetypes.guess_type(filename or "")[0] or ""
    guessed = guessed.split(";", 1)[0].strip().lower()
    if guessed.startswith("image/"):
        return guessed
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return mime_type


@router.get("/api/emotion_expr_map")
def emotion_expr_map_get(request: Request, user: RequireUser):
    device_id, err = _owned_device_or_error(request, user)
    if err:
        return err
    return jsonify({"ok": True, "device_id": device_id, "map": load_emotion_expr_map(device_id=device_id)})


@router.post("/api/emotion_expr_map")
def emotion_expr_map_post(request: Request, user: RequireDeveloper):
    # 读取保留给消费端首页（GET），写入属表情设计编辑器，仅开发者可用

    device_id, err = _owned_device_or_error(request, user)
    if err:
        return err
    payload = get_json(request, silent=True) or {}
    mapping = payload.get("map")
    if not isinstance(mapping, dict):
        return jsonify({"ok": False, "error": "map 必须是对象"}), 400
    try:
        saved = save_emotion_expr_map(mapping, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "map": saved})


@router.get("/api/face_expr_scenes")
def face_expr_scenes_get(request: Request, user: RequireUser):
    device_id, err = _owned_device_or_error(request, user)
    if err:
        return err
    try:
        rows = load_face_expr_scenes_file(seed_if_missing=True, device_id=device_id) or []
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "device_id": device_id, "config": rows})


@router.post("/api/face_expr_scenes")
def face_expr_scenes_post(request: Request, user: RequireDeveloper):
    device_id, err = _owned_device_or_error(request, user)
    if err:
        return err
    payload = get_json(request, silent=True) or {}
    scenes = payload.get("scenes")
    if scenes is None:
        scenes = payload.get("config")
    if not isinstance(scenes, list):
        return jsonify({"ok": False, "error": "scenes 必须是数组"}), 400
    try:
        saved = save_face_expr_scenes_file(scenes, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "device_id": device_id, "config": saved})


@router.get("/api/face_mouth_by_phoneme")
def face_mouth_by_phoneme_get(request: Request, user: RequireDeveloper):
    # 音素口型库仅表情设计编辑器使用（无消费端读取方）

    device_id, err = _owned_device_or_error(request, user)
    if err:
        return err
    try:
        groups = load_face_mouth_cfg_file(seed_if_missing=True, device_id=device_id) or []
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "device_id": device_id, "mouth_by_phoneme_groups": groups})


@router.post("/api/face_mouth_by_phoneme")
def face_mouth_by_phoneme_post(request: Request, user: RequireDeveloper):
    device_id, err = _owned_device_or_error(request, user)
    if err:
        return err
    payload = get_json(request, silent=True) or {}
    groups = payload.get("mouth_by_phoneme_groups")
    if not isinstance(groups, list):
        return jsonify({"ok": False, "error": "mouth_by_phoneme_groups 必须是数组"}), 400
    try:
        save_face_mouth_cfg_file(groups, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "device_id": device_id, "mouth_by_phoneme_groups": groups})


@router.post("/api/scene_playbook/export_plan")
def scene_playbook_export_plan_post(request: Request, user: RequireDeveloper):
    device_id, err = _owned_device_or_error(request, user)
    if err:
        return err
    payload = get_json(request, silent=True) or {}
    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        return jsonify({"ok": False, "error": "missing playbook"}), 400
    try:
        from deskbot_server.dao.scene_playbooks_store import normalize_playbook
        from deskbot_server.service.scene_playbook_runner import playbook_debug_snapshot

        pb = normalize_playbook(playbook)
        snap = playbook_debug_snapshot(pb, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "device_id": device_id, **snap})


@router.post("/api/face_design/generate-from-image")
def face_design_generate_from_image_post(request: Request, user: RequireDeveloper):
    device_id, err = _optional_owned_device_or_error(request, user)
    if err:
        return err
    from deskbot_server.web.view_helpers import read_upload_bytes

    upload = files_get(request, "image") or files_get(request, "file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "请先上传图片"}), 400
    image_bytes = read_upload_bytes(upload)
    prompt = str(form_get(request, "prompt") or "").strip()
    mime_type = _image_mime_from_upload(
        upload.filename, getattr(upload, "mimetype", None) or getattr(upload, "content_type", None) or "", image_bytes
    )
    try:
        from deskbot_server.vision.ark_face_svg import generate_face_svg_from_image

        result = generate_face_svg_from_image(image_bytes, mime_type, prompt=prompt)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify({"device_id": device_id, **result})


ENDPOINTS = {
    "app2c.home": "/home",
    "app2c.expr": "/expr",
    "app2c.lab": "/lab",
    "app2c.memories": "/my/memories",
    "app2c.reminders": "/my/reminders",
    "app2c.people": "/my/people",
    "app2c.devices": "/my/devices",
    "app2c.miot": "/my/miot",
    "app2c.advanced": "/advanced",
    "app2c.advanced_summary_get": "/api/advanced",
    "app2c.advanced_profile_patch": "/api/advanced/profile",
    "app2c.advanced_password_post": "/api/advanced/password",
    "app2c.emotion_expr_map_get": "/api/emotion_expr_map",
    "app2c.emotion_expr_map_post": "/api/emotion_expr_map",
    "app2c.face_expr_scenes_get": "/api/face_expr_scenes",
    "app2c.face_expr_scenes_post": "/api/face_expr_scenes",
    "app2c.face_mouth_by_phoneme_get": "/api/face_mouth_by_phoneme",
    "app2c.face_mouth_by_phoneme_post": "/api/face_mouth_by_phoneme",
    "app2c.scene_playbook_export_plan_post": "/api/scene_playbook/export_plan",
    "app2c.face_design_generate_from_image_post": "/api/face_design/generate-from-image",
}
