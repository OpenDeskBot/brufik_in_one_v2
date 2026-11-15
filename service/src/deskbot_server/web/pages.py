"""页面只发布静态壳；原页面入口提供受权限保护的 JSON 上下文。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute, APIWebSocketRoute

from deskbot_server.controller.decorators import access
from deskbot_server.controller.decorators.http import SopAPIRoute

PUBLIC_DIR = Path(__file__).resolve().parents[1] / "public"


def page(template: str):
    def declare(endpoint):
        endpoint.__page_template__ = template
        return endpoint

    return declare


def _static_endpoint(path: str):
    @access("public")
    def static_page():
        return FileResponse(
            PUBLIC_DIR / ("site" if path == "/" else "admin") / "index.html", headers={"X-Deskbot-Page": "static", "Cache-Control": "no-store"}
        )

    return static_page


def browser_router(router):
    """不修改共享 router；每个应用独立生成页面与 context 路由。"""
    output = APIRouter()
    for route in router.routes:
        if isinstance(route, APIWebSocketRoute):
            output.add_api_websocket_route(route.path, route.endpoint, name=route.name, dependencies=route.dependencies)
        elif isinstance(route, APIRoute):
            is_page = getattr(route.endpoint, "__page_template__", None) is not None
            if is_page:
                static = APIRouter(route_class=SopAPIRoute)
                static.add_api_route(route.path, _static_endpoint(route.path), methods=["GET"], include_in_schema=False)
                output.routes.extend(static.routes)
            output.add_api_route(
                f"/api/pages{route.path}" if is_page else route.path,
                route.endpoint,
                methods=route.methods,
                name=route.name,
                response_model=route.response_model,
                status_code=route.status_code,
                response_class=route.response_class,
                dependencies=route.dependencies,
                route_class_override=type(route),
                include_in_schema=route.include_in_schema,
            )
    return output


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    from deskbot_server.db.models import User

    if isinstance(value, User):
        return {
            key: _json_value(getattr(value, key, None))
            for key in ("id", "email", "display_name", "is_developer", "is_active", "created_at")
        }
    raise TypeError(f"unsupported page data type: {type(value).__name__}")


def page_data(request, name, context):
    from deskbot_server.web.deps import load_session_user
    from deskbot_server.web.session_device import get_current_device_id
    from deskbot_server.web.urls import _endpoint_map, get_flashed_messages

    user = load_session_user(request)
    context.setdefault("nav_user_email", user.email if user else None)
    context.setdefault("nav_display_name", (user.display_name or user.email) if user else None)
    context.setdefault("nav_current_device_id", get_current_device_id(request) if user else None)
    context.setdefault("nav_is_developer", bool(user and user.is_developer))
    context["flashes"] = get_flashed_messages(request, with_categories=True)
    return JSONResponse(
        {"template": name, "context": _json_value(context), "endpoints": dict(_endpoint_map)},
        headers={"Cache-Control": "no-store"},
    )
