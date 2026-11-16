"""显式访问类别；HTTP 权限检查统一发生在业务和依赖执行前。"""

from __future__ import annotations

from fastapi import HTTPException, Request

ACCESS_KINDS = frozenset({"public", "user", "developer", "device", "subscriber", "pipeline"})


def access(kind: str):
    if kind not in ACCESS_KINDS:
        raise ValueError(f"unknown access kind: {kind}")

    def declare(endpoint):
        endpoint.__access_kind__ = kind
        return endpoint

    return declare


def resolve_user(request: Request):
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached or None
    from deskbot_server.auth.session_user import SessionUser
    from deskbot_server.controller.auth import _resolve_user_id_from_session, request_qargs
    from deskbot_server.service.user_service import UserService

    uid = str((request.scope.get("session") or {}).get("user_id") or "").strip()
    if not uid:
        uid = _resolve_user_id_from_session(request_qargs(request), request.headers)
    user = UserService().get_user(uid) if uid else None
    if user is None or not user.is_active:
        request.state.current_user = False
        return None
    request.state.current_user = SessionUser(user)
    request.state.user_id = str(user.id)
    return request.state.current_user


def enforce_access(request: Request, kind: str) -> None:
    if kind == "public":
        return
    if kind not in ("user", "developer"):
        raise RuntimeError(f"HTTP route cannot use {kind!r} access")
    user = resolve_user(request)
    if user is None:
        raise HTTPException(401, detail={"ok": False, "error": "unauthorized"})
    if kind == "developer" and not user.is_developer:
        raise HTTPException(403, detail={"ok": False, "error": "需要开发者权限"})
    explicit_device = request.headers.get("x-deskbot-device")
    if explicit_device is not None and explicit_device != "__none__":
        from deskbot_server.service.user_service import UserService

        if not explicit_device.strip() or not UserService().user_owns_device(user.id, explicit_device.strip()):
            raise HTTPException(403, detail={"ok": False, "error": "设备不属于当前账号"})
