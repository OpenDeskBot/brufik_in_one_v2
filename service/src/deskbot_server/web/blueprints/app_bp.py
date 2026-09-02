from __future__ import annotations

import json

from fastapi import APIRouter, Request

from deskbot_server.service.face_profile_service import (
    delete_face_profile,
    list_face_profiles_summary,
    update_face_profile_name,
)
from deskbot_server.service.voice_profile_service import (
    delete_voice_profile,
    list_voice_profiles_summary,
    update_voice_profile_name,
)
from deskbot_server.dao import device_mapper
from deskbot_server.dao.device_memory_mapper import add_memory, delete_memory, get_memory, list_memory_for_device, update_memory
from deskbot_server.service.miot_service import (
    authorize_and_sync,
    error_payload,
    get_bind_url,
    get_status,
    load_homes_cache,
    miot_sdk_available,
    parse_auth_payload,
    sync_homes,
)
from deskbot_server.service.miot_service import unbind as miot_unbind
from deskbot_server.service.quest_service import QuestError, QuestService
from deskbot_server.service.scheduled_task_service import (
    count_scheduled_tasks_for_device,
    delete_scheduled_task,
    list_scheduled_tasks_for_device,
)
from deskbot_server.service.user_service import UserService
from deskbot_server.web.deps import RequireUser
from deskbot_server.web.helpers import fetch_live_device_details
from deskbot_server.web.session_device import clear_current_device, get_current_device_id, set_current_device_id
from deskbot_server.web.urls import flash, url_for
from deskbot_server.web.view_helpers import ViewAPIRoute, form_get, get_json, jsonify, redirect

router = APIRouter(route_class=ViewAPIRoute, prefix="/app", tags=["app"])


def _fmt_bytes(n: int) -> str:
    n = int(n or 0)
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.2f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def fmt_bytes_filter(n):
    return _fmt_bytes(n)


def _flatten_usage_daily_rows(
    stats: list[dict], *, label_key: str, sub_id_key: str, sub_label_key: str | None = None
) -> list[dict]:
    rows: list[dict] = []
    for item in stats:
        label = str(item.get(label_key) or item.get(sub_id_key) or "")
        sub_id = str(item.get(sub_id_key) or "")
        sub_label = str(item.get(sub_label_key) or "") if sub_label_key else sub_id
        for day in item.get("days") or []:
            if not isinstance(day, dict):
                continue
            rows.append(
                {
                    "label": label,
                    "sub_id": sub_id,
                    "sub_label": sub_label,
                    "date": str(day.get("date") or ""),
                    "asr_bytes": int(day.get("asr_bytes") or 0),
                    "face_bytes": int(day.get("face_bytes") or 0),
                    "llm_bytes": int(day.get("llm_bytes") or 0),
                    "tts_bytes": int(day.get("tts_bytes") or 0),
                    "total_bytes": int(day.get("total_bytes") or 0),
                }
            )
    rows.sort(key=lambda r: (r["date"], r["sub_id"]), reverse=True)
    return rows


@router.post("/settings/profile")
def update_profile_post(request: Request, user: RequireUser):
    try:
        UserService().update_display_name(user.id, form_get(request, "display_name") or "")
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("app2c.advanced"))
    flash(request, "用户名称已更新", "success")
    return redirect(url_for("app2c.advanced"))


@router.post("/settings/password")
def change_password_post(request: Request, user: RequireUser):
    old_password = form_get(request, "old_password") or ""
    new_password = form_get(request, "new_password") or ""
    confirm = form_get(request, "confirm_password") or ""
    if new_password != confirm:
        flash(request, "两次新密码不一致", "error")
        return redirect(url_for("app2c.advanced"))
    try:
        UserService().change_password(user.id, old_password, new_password)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect(url_for("app2c.advanced"))
    flash(request, "密码已更新", "success")
    return redirect(url_for("app2c.advanced"))


@router.get("/api/devices")
def api_list_devices(request: Request, user: RequireUser):
    devices = UserService().list_devices(user.id)
    live_map = fetch_live_device_details(user_id=user.id)
    current = get_current_device_id(request)
    return jsonify(
        {
            "ok": True,
            "devices": [
                {
                    "id": d.id,
                    "device_id": d.device_id,
                    "display_name": d.display_name or d.device_id,
                    "quest_id": d.quest_id or None,
                    "claimed_at": d.claimed_at.isoformat() if d.claimed_at else None,
                    "online": live_map.get(d.device_id, {}).get("online", False),
                    "last_seen": live_map.get(d.device_id, {}).get("last_seen", "—"),
                    "last_sync": live_map.get(d.device_id, {}).get("last_sync", "—"),
                    "is_current": d.device_id == current,
                }
                for d in devices
            ],
            "current_device_id": current,
        }
    )


@router.post("/api/devices")
def api_bind_device(request: Request, user: RequireUser):
    payload = get_json(request, silent=True) or {}
    device_id = str(payload.get("device_id") or form_get(request, "device_id") or "").strip()
    display_name = str(payload.get("display_name") or "").strip() or None
    if not device_id:
        return jsonify({"ok": False, "error": "绑定失败：请输入 device_id"}), 400
    try:
        device = UserService().bind_device(user.id, device_id, display_name=display_name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    set_current_device_id(request, device.device_id)
    return jsonify(
        {
            "ok": True,
            "message": "绑定成功",
            "device": {"device_id": device.device_id},
            "current_device_id": device.device_id,
        }
    )


@router.post("/api/devices/select")
def api_select_device(request: Request, user: RequireUser):
    payload = get_json(request, silent=True) or {}
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        clear_current_device(request)
        return jsonify({"ok": True, "current_device_id": None})
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    set_current_device_id(request, device_id)
    return jsonify({"ok": True, "current_device_id": device_id})


@router.delete("/api/devices/{device_id}")
def api_unbind_device(request: Request, user: RequireUser, device_id: str):
    if not UserService().unbind_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不存在"}), 404
    if get_current_device_id(request) == device_id:
        clear_current_device(request)
    return jsonify({"ok": True})


@router.post("/api/devices/{device_id}/reset-id")
def api_reset_device_id(request: Request, user: RequireUser, device_id: str):
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    try:
        new_device_id = UserService().reset_device_id(device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    # 同步更新 session 中的当前设备
    if get_current_device_id(request) == device_id:
        set_current_device_id(request, new_device_id)
    return jsonify({"ok": True, "device_id": new_device_id})


@router.put("/api/devices/{device_id}/quest")
def api_set_device_quest(request: Request, user: RequireUser, device_id: str):
    """绑定/解绑设备剧本：body ``{"quest_id": "剧本名"}``；空串或缺省 = 解绑。"""
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    payload = get_json(request, silent=True) or {}
    quest_id = str(payload.get("quest_id") or "").strip() or None
    if quest_id is not None:
        try:
            if QuestService().get_playbook(quest_id) is None:
                return jsonify({"ok": False, "error": f"剧本不存在: {quest_id}"}), 404
        except QuestError as exc:  # 非法剧本名（正则不匹配）/ 剧本文件损坏
            return jsonify({"ok": False, "error": str(exc)}), 400
    device_mapper.update_quest_id(device_id, quest_id)
    return jsonify({"ok": True, "quest_id": quest_id})


@router.put("/api/devices/{device_id}/llm")
def api_set_device_llm(request: Request, user: RequireUser, device_id: str):
    """设备级 LLM provider 与参数（partial 更新）。

    body ``{"provider": "qwen", "context_window": 8192}``：
    - ``provider``：可选；提供则整体覆盖（空串/缺省键不写）。
    - ``context_window``：可选；正整数写入 / 显式 null 或 0 清除该键。
      llm_param 其余键原样保留（JSON 合并）。
    """
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    payload = get_json(request, silent=True) or {}

    if "provider" in payload:
        provider = str(payload.get("provider") or "").strip()
        device_mapper.update_llm_provider(device_id, provider)

    param = device_mapper.get_llm_param(device_id)
    if "context_window" in payload:
        raw = payload.get("context_window")
        if raw is None or raw == "" or str(raw).strip().lower() in ("0", "null", "none"):
            param.pop("context_window", None)
        else:
            try:
                cw = int(raw)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "context_window 须为正整数"}), 400
            if cw <= 0:
                return jsonify({"ok": False, "error": "context_window 须为正整数"}), 400
            param["context_window"] = cw
    device_mapper.update_llm_param(device_id, json.dumps(param, ensure_ascii=False) if param else None)

    return jsonify({
        "ok": True,
        "provider": device_mapper.get_llm_provider(device_id),
        "llm_param": param,
    })


@router.get("/api/scheduled-tasks")
def api_list_scheduled_tasks(request: Request, user: RequireUser):
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "请先选择设备"}), 400
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    page = max(1, int(request.query_params.get("page") or 1))
    per_page = int(request.query_params.get("per_page") or 10)
    if per_page not in (10, 50, 100, 200):
        per_page = 10
    total = count_scheduled_tasks_for_device(device_id)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    tasks = list_scheduled_tasks_for_device(device_id, limit=per_page, offset=offset)
    return jsonify(
        {
            "ok": True,
            "device_id": device_id,
            "tasks": tasks,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )


@router.get("/api/face-profiles")
def api_list_face_profiles(request: Request, user: RequireUser):
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "请先选择设备"}), 400
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    profiles = list_face_profiles_summary(device_id=device_id)
    return jsonify({"ok": True, "device_id": device_id, "profiles": profiles})


@router.delete("/api/face-profiles/{profile_id}")
def api_delete_face_profile(request: Request, user: RequireUser, profile_id: int):
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "请先选择设备"}), 400
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    if not delete_face_profile(profile_id, device_id=device_id):
        return jsonify({"ok": False, "error": "人脸档案不存在"}), 404
    return jsonify({"ok": True})


@router.put("/api/face-profiles/{profile_id}")
@router.patch("/api/face-profiles/{profile_id}")
def api_update_face_profile(request: Request, user: RequireUser, profile_id: int):
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "请先选择设备"}), 400
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    payload = get_json(request, silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name 不能为空"}), 400
    try:
        profile = update_face_profile_name(profile_id, name, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if profile is None:
        return jsonify({"ok": False, "error": "人脸档案不存在"}), 404
    return jsonify({"ok": True, "profile": profile})


@router.get("/api/voice-profiles")
def api_list_voice_profiles(request: Request, user: RequireUser):
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "请先选择设备"}), 400
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    profiles = list_voice_profiles_summary(device_id=device_id)
    return jsonify({"ok": True, "device_id": device_id, "profiles": profiles})


@router.delete("/api/voice-profiles/{profile_id}")
def api_delete_voice_profile(request: Request, user: RequireUser, profile_id: int):
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "请先选择设备"}), 400
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    if not delete_voice_profile(profile_id, device_id=device_id):
        return jsonify({"ok": False, "error": "声纹档案不存在"}), 404
    return jsonify({"ok": True})


@router.put("/api/voice-profiles/{profile_id}")
@router.patch("/api/voice-profiles/{profile_id}")
def api_update_voice_profile(request: Request, user: RequireUser, profile_id: int):
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "请先选择设备"}), 400
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    payload = get_json(request, silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name 不能为空"}), 400
    try:
        profile = update_voice_profile_name(profile_id, name, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if profile is None:
        return jsonify({"ok": False, "error": "声纹档案不存在"}), 404
    return jsonify({"ok": True, "profile": profile})


def _require_owned_device_id(request: Request, user) -> tuple[str | None, tuple | None]:
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return None, (jsonify({"ok": False, "error": "请先选择设备"}), 400)
    if not UserService().user_owns_device(user.id, device_id):
        return None, (jsonify({"ok": False, "error": "设备不属于当前账号"}), 403)
    return device_id, None




@router.get("/api/memories")
def api_list_memories(request: Request, user: RequireUser):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    entries = list_memory_for_device(device_id)
    return jsonify({"ok": True, "device_id": device_id, "memories": entries, "count": len(entries)})


@router.post("/api/memories")
def api_create_memory(request: Request, user: RequireUser):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    payload = get_json(request, silent=True) or {}
    text = str(payload.get("text") or form_get(request, "text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text 不能为空"}), 400
    try:
        entry = add_memory(text, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "memory": entry})


@router.get("/api/memories/{entry_id}")
def api_get_memory(request: Request, user: RequireUser, entry_id: str):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    entry = get_memory(entry_id, device_id=device_id)
    if entry is None:
        return jsonify({"ok": False, "error": "记忆不存在"}), 404
    return jsonify({"ok": True, "memory": entry})


@router.put("/api/memories/{entry_id}")
@router.patch("/api/memories/{entry_id}")
def api_update_memory(request: Request, user: RequireUser, entry_id: str):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    payload = get_json(request, silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text 不能为空"}), 400
    try:
        entry = update_memory(entry_id, text, device_id=device_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if entry is None:
        return jsonify({"ok": False, "error": "记忆不存在"}), 404
    return jsonify({"ok": True, "memory": entry})


@router.delete("/api/memories/{entry_id}")
def api_delete_memory(request: Request, user: RequireUser, entry_id: str):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    if not delete_memory(entry_id, device_id=device_id):
        return jsonify({"ok": False, "error": "记忆不存在"}), 404
    return jsonify({"ok": True})




@router.delete("/api/scheduled-tasks/{task_id}")
def api_delete_scheduled_task(request: Request, user: RequireUser, task_id: str):
    device_id = str(request.query_params.get("device_id") or get_current_device_id(request) or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "请先选择设备"}), 400
    if not UserService().user_owns_device(user.id, device_id):
        return jsonify({"ok": False, "error": "设备不属于当前账号"}), 403
    if not delete_scheduled_task(task_id, device_id=device_id):
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True})


# ----- 米家 IoT -----


@router.get("/api/miot/status")
def api_miot_status(request: Request, user: RequireUser):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    ok_sdk, sdk_err = miot_sdk_available()
    if not ok_sdk:
        return jsonify({"ok": False, "error": sdk_err, "sdk_ok": False}), 503
    refresh = str(request.query_params.get("refresh") or "").strip().lower() in ("1", "true", "yes")
    try:
        status = get_status(device_id, refresh=refresh)
    except Exception as exc:
        return jsonify({"ok": False, **error_payload(exc)}), 400
    homes = load_homes_cache(device_id)
    return jsonify(
        {
            "ok": True,
            "sdk_ok": True,
            "device_id": device_id,
            "status": status,
            "homes": homes,
            "device_count": homes.get("device_count") or len(homes.get("devices") or []),
            "scene_count": homes.get("scene_count") or len(homes.get("scenes") or []),
        }
    )


@router.post("/api/miot/bind-url")
def api_miot_bind_url(request: Request, user: RequireUser):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    ok_sdk, sdk_err = miot_sdk_available()
    if not ok_sdk:
        return jsonify({"ok": False, "error": sdk_err}), 503
    try:
        url = get_bind_url(device_id)
    except Exception as exc:
        return jsonify({"ok": False, **error_payload(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "auth_url": url,
            "hint": (
                "请在浏览器打开授权链接并登录小米账号。"
                "授权完成后会进入「小米账号授权完成」页面，"
                "点击「复制授权码」，回到本页粘贴即可（也可粘贴完整回调 URL）。"
            ),
        }
    )


@router.post("/api/miot/authorize")
def api_miot_authorize(request: Request, user: RequireUser):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    payload = get_json(request, silent=True) or {}
    code = str(payload.get("code") or "").strip()
    state = str(payload.get("state") or "").strip()
    callback = str(payload.get("callback_url") or payload.get("url") or payload.get("payload") or "").strip()
    try:
        if code and state:
            auth_code, auth_state = code, state
        elif callback:
            auth_code, auth_state = parse_auth_payload(callback)
        else:
            raise ValueError("请粘贴授权回调完整 URL，或提供 code 与 state")
        result = authorize_and_sync(device_id, auth_code, auth_state)
    except Exception as exc:
        return jsonify({"ok": False, **error_payload(exc)}), 400
    return jsonify(result)


@router.post("/api/miot/sync")
def api_miot_sync(request: Request, user: RequireUser):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    try:
        homes = sync_homes(device_id)
        status = get_status(device_id, refresh=True)
    except Exception as exc:
        import logging

        logging.getLogger("deskbot-server").exception("[miot] sync failed device_id=%s", device_id)
        return jsonify({"ok": False, **error_payload(exc)}), 400
    return jsonify(
        {
            "ok": True,
            "status": status,
            "homes": homes,
            "device_count": homes.get("device_count"),
            "scene_count": homes.get("scene_count"),
        }
    )


@router.post("/api/miot/unbind")
def api_miot_unbind(request: Request, user: RequireUser):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    try:
        miot_unbind(device_id)
    except Exception as exc:
        return jsonify({"ok": False, **error_payload(exc)}), 400
    return jsonify({"ok": True})


@router.get("/api/miot/homes")
def api_miot_homes(request: Request, user: RequireUser):
    device_id, err = _require_owned_device_id(request, user)
    if err:
        return err
    assert device_id is not None
    homes = load_homes_cache(device_id)
    return jsonify({"ok": True, "homes": homes})


ENDPOINTS = {
    "app.update_profile_post": "/app/settings/profile",
    "app.change_password_post": "/app/settings/password",
    "app.api_list_devices": "/app/api/devices",
    "app.api_bind_device": "/app/api/devices",
    "app.api_select_device": "/app/api/devices/select",
    "app.api_unbind_device": "/app/api/devices/{device_id}",
    "app.api_set_device_quest": "/app/api/devices/{device_id}/quest",
    "app.api_set_device_llm": "/app/api/devices/{device_id}/llm",
    "app.api_list_scheduled_tasks": "/app/api/scheduled-tasks",
    "app.api_list_face_profiles": "/app/api/face-profiles",
    "app.api_delete_face_profile": "/app/api/face-profiles/{profile_id}",
    "app.api_update_face_profile": "/app/api/face-profiles/{profile_id}",
    "app.api_list_voice_profiles": "/app/api/voice-profiles",
    "app.api_delete_voice_profile": "/app/api/voice-profiles/{profile_id}",
    "app.api_update_voice_profile": "/app/api/voice-profiles/{profile_id}",
    "app.api_list_memories": "/app/api/memories",
    "app.api_create_memory": "/app/api/memories",
    "app.api_get_memory": "/app/api/memories/{entry_id}",
    "app.api_update_memory": "/app/api/memories/{entry_id}",
    "app.api_delete_memory": "/app/api/memories/{entry_id}",
    "app.api_delete_scheduled_task": "/app/api/scheduled-tasks/{task_id}",
    "app.api_miot_status": "/app/api/miot/status",
    "app.api_miot_bind_url": "/app/api/miot/bind-url",
    "app.api_miot_authorize": "/app/api/miot/authorize",
    "app.api_miot_sync": "/app/api/miot/sync",
    "app.api_miot_unbind": "/app/api/miot/unbind",
    "app.api_miot_homes": "/app/api/miot/homes",
}
