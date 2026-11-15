"""FastAPI 依赖：会话用户解析与鉴权。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from deskbot_server.auth.session_user import SessionUser


def load_session_user(request: Request) -> SessionUser | None:
    from deskbot_server.controller.decorators.access import resolve_user

    return resolve_user(request)


async def get_current_user(request: Request) -> SessionUser | None:
    return load_session_user(request)


def require_user(request: Request) -> SessionUser:
    user = load_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail={"ok": False, "error": "unauthorized"})
    return user


RequireUser = Annotated[SessionUser, Depends(require_user)]
