"""将 Web 控制台挂到 FastAPI：Session、Jinja、Static、鉴权。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from deskbot_server.db import init_database
from deskbot_server.utils.env import load_dotenv
from deskbot_server.web.urls import register_endpoint

logger = logging.getLogger("deskbot-server")

_WEB_DIR = Path(__file__).resolve().parent
_PUBLIC_PREFIXES = ("/login", "/register", "/health", "/docs", "/openapi.json", "/redoc")


def _register_endpoints_from_modules() -> None:
    from deskbot_server.web.blueprints import (
        app2c_bp,
        app_bp,
        auth_bp,
        debug_bp,
        flash_bp,
        quest_bp,
        robot_settings_bp,
        site,
    )

    for mod in (site, auth_bp, app_bp, app2c_bp, debug_bp, flash_bp, quest_bp, robot_settings_bp):
        for name, path in getattr(mod, "ENDPOINTS", {}).items():
            register_endpoint(name, path)


async def _prepare_body(request: Request) -> None:
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        request.state.form = None
        request.state.json = None
        request.state.body = b""
        return
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            request.state.json = await request.json()
        except Exception:
            request.state.json = None
        request.state.form = None
        request.state.body = b""
        return
    if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        try:
            request.state.form = await request.form()
        except Exception:
            request.state.form = None
        request.state.json = None
        request.state.body = b""
        return
    try:
        request.state.body = await request.body()
    except Exception:
        request.state.body = b""
    request.state.form = None
    request.state.json = None


def mount_web(app: FastAPI) -> None:
    """在已有 FastAPI app 上挂载 Web 控制台。"""
    load_dotenv()
    init_database()

    from deskbot_server.auth.web_secret import resolve_web_secret_key

    secret = resolve_web_secret_key()

    from deskbot_server.web.pages import PUBLIC_DIR

    app.mount("/static", StaticFiles(directory=str(PUBLIC_DIR / "shared")), name="static")

    _register_endpoints_from_modules()

    from deskbot_server.web.blueprints.app2c_bp import router as app2c_router
    from deskbot_server.web.blueprints.app_bp import router as app_router
    from deskbot_server.web.blueprints.auth_bp import router as auth_router
    from deskbot_server.web.blueprints.debug_bp import router as debug_router
    from deskbot_server.web.blueprints.flash_bp import router as flash_router
    from deskbot_server.web.blueprints.quest_bp import router as quest_router
    from deskbot_server.web.blueprints.robot_settings_bp import router as robot_settings_router
    from deskbot_server.web.blueprints.site import router as site_router

    @app.middleware("http")
    async def prepare_legacy_body(request: Request, call_next):
        await _prepare_body(request)
        response = await call_next(request)
        if request.url.path in {"/static/views.json", "/static/page-loader.js", "/static/page-renderer.js", "/static/ui-runtime.js"}:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.query_params.get("v") else "no-store"
        return response

    from deskbot_server.controller.registry import register_routers
    from deskbot_server.web.pages import browser_router

    register_routers(app, [browser_router(router) for router in (
        site_router, auth_router, app_router, app2c_router, flash_router,
        quest_router, robot_settings_router, debug_router,
    )])

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="deskbot_session",
        max_age=604800,
        same_site="lax",
        https_only=False,
    )

    logger.info("Web console mounted on FastAPI (templates=%s)", _WEB_DIR / "templates")
