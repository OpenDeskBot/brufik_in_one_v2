"""Developer controls for the two trusted perception services."""

from fastapi import APIRouter, HTTPException, Request

from deskbot_server.auth.permissions import RequireDeveloper
from deskbot_server.controller.decorators import access
from deskbot_server.service.external.manager import ServiceError
from deskbot_server.service.perception_services import PERCEPTION_SERVICES
from deskbot_server.web.pages import page
from deskbot_server.web.view_helpers import ViewAPIRoute, get_json, jsonify, render_template

router = APIRouter(route_class=ViewAPIRoute, tags=["perception"])


def manager(request):
    value = getattr(request.app.state, "perception_services", None)
    if value is None:
        raise HTTPException(503, "感知服务管理器尚未启动")
    return value


@router.get("/perception-services")
@access("developer")
@page("app2c/perception_services.html")
def perception_page(request: Request, user: RequireDeveloper):
    return render_template(request, "app2c/perception_services.html", active_nav="perception")


@router.get("/api/perception-services")
@access("developer")
def perception_status(request: Request, user: RequireDeveloper):
    return jsonify({"ok": True, "services": [s.to_dict() for s in manager(request).status_all()]})


@router.post("/api/perception-services/{name}/{operation}")
@access("developer")
async def perception_control(request: Request, user: RequireDeveloper, name: str, operation: str):
    if name not in PERCEPTION_SERVICES or operation not in {"install", "start", "stop", "restart", "auto-start"}:
        raise HTTPException(404, "未知感知服务操作")
    value = manager(request)
    try:
        if operation == "auto-start":
            enabled = (get_json(request) or {}).get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须为布尔值")
            result = value.set_auto_start(name, enabled)
        else:
            result = await getattr(value, operation)(name)
        return jsonify({"ok": True, "service": result.to_dict()})
    except (ValueError, ServiceError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


ENDPOINTS = {"perception.page": "/perception-services"}
