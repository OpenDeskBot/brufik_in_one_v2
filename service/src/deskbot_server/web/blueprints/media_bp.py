"""Authenticated device media library and direct playback controls."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from deskbot_server.controller.decorators import access
from deskbot_server.dao import media_asset_store as store
from deskbot_server.service import media_library
from deskbot_server.service.device_ws_service import DeviceWsService
from deskbot_server.service.user_service import UserService
from deskbot_server.web.deps import RequireUser
from deskbot_server.web.pages import page
from deskbot_server.web.session_device import get_current_device_id
from deskbot_server.web.view_helpers import ViewAPIRoute, files_get, form_get, get_json, jsonify, render_template

router = APIRouter(route_class=ViewAPIRoute, tags=["device_media"])


def device(request, user):
    did = request.query_params.get("device_id") or get_current_device_id(request)
    if not did:
        raise HTTPException(400, "请先在顶栏选择设备")
    if not UserService().user_owns_device(user.id, did):
        raise HTTPException(403, "设备不属于当前账号")
    return did


@router.get("/my/media")
@access("user")
@page("app2c/media.html")
def media_page(request: Request, user: RequireUser):
    return render_template(request, "app2c/media.html", active_nav="media")


@router.get("/api/device-media")
@access("user")
async def media_list(request: Request, user: RequireUser):
    did = device(request, user)
    hub = DeviceWsService.instance()
    entry = hub._devices.get(did) if hub else None
    session = entry.conversation_session if entry else None
    media = getattr(session, "media", None)
    return jsonify({"ok": True, "device_id": did, "assets": await asyncio.to_thread(store.list_assets, did),
                    "capabilities": sorted(entry.capabilities) if entry else [], "online": bool(entry and entry.ws),
                    "playback": media.status if media else {"state": "idle"}})


@router.post("/api/device-media")
@access("user")
async def media_upload(request: Request, user: RequireUser):
    did = device(request, user)
    upload = files_get(request, "file")
    if upload is None:
        return jsonify({"ok": False, "error": "请选择文件"}), 400
    data = await upload.read(media_library.MAX_UPLOAD_BYTES+1)
    try:
        title = form_get(request, "title") or Path(upload.filename or "未命名媒体").stem
        item = await asyncio.to_thread(media_library.upload, did, str(form_get(request, "kind") or ""), title, data)
        return jsonify({"ok": True, "asset": item})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@router.get("/api/device-media/{asset_id}/file")
@access("user")
async def media_file(request: Request, user: RequireUser, asset_id: str):
    did = device(request, user)
    try:
        item = await asyncio.to_thread(store.get, did, asset_id)
        extension = "wav" if item["kind"] == "music" else "png"
        return FileResponse(store.asset_path(did, asset_id, extension), media_type="audio/wav" if extension == "wav" else "image/png",
                            headers={"Cache-Control": "private, no-store"})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@router.delete("/api/device-media/{asset_id}")
@access("user")
async def media_delete(request: Request, user: RequireUser, asset_id: str):
    did = device(request, user)
    try:
        await asyncio.to_thread(store.delete, did, asset_id)
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@router.post("/api/device-media/control")
@access("user")
async def media_control(request: Request, user: RequireUser):
    did = device(request, user)
    body = get_json(request) or {}
    hub = DeviceWsService.instance()
    entry = hub._devices.get(did) if hub else None
    if not entry or not entry.ws:
        return jsonify({"ok": False, "error": "设备未连接"}), 409
    name = body.get("tool")
    arguments = body.get("arguments") or {}
    try:
        if not isinstance(name, str):
            raise ValueError("请选择设备操作")
        if not isinstance(arguments, dict):
            raise ValueError("参数需要是对象")
        if name == "rom_volume":
            action, value = arguments.get("action"), arguments.get("value", 0)
            if (not isinstance(action, str) or action not in {"get", "set", "adjust"} or type(value) is not int
                    or not -100 <= value <= 100 or (action == "set" and value < 0) or (action != "get" and "value" not in arguments)):
                raise ValueError("音量参数无效")
            result = await hub.rom_command(did, "volume", arguments)
        elif name in {"play_music", "show_image", "stop_media"}:
            session = entry.conversation_session
            if not callable(getattr(session, "media_tool", None)):
                return jsonify({"ok": False, "error": "请先连接 Flecket 外部 Agent"}), 409
            result = await session.media_tool(name, {**arguments, "speak": False})
        else:
            raise ValueError("未知设备操作")
        return jsonify(result), 200 if result.get("ok") else 409
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


ENDPOINTS = {"media.page": "/my/media"}
