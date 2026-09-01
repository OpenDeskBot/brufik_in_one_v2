from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deskbot_server.web.deps import load_session_user
from deskbot_server.web.view_helpers import ViewAPIRoute, render_template

router = APIRouter(route_class=ViewAPIRoute, tags=["site"])


@router.get("/")
def index(request: Request):
    """产品落地页：未登录展示宣传与登录入口，已登录直接进控制台。

    表情复用「家」页面的 idle 表情场景（data/global/deskbot-face.json），
    与登录后首页渲染同一份数据。
    """
    from deskbot_server.dao.face_expr_scenes_store import load_face_expr_scenes_file

    face_scenes = load_face_expr_scenes_file(seed_if_missing=False) or []
    return render_template(
        request,
        "landing.html",
        signed_in=load_session_user(request) is not None,
        face_scenes=face_scenes,
    )


@router.get("/health")
def health(request: Request):
    del request
    return JSONResponse({"ok": True, "service": "deskbot-web"})


ENDPOINTS = {"site.index": "/", "site.health": "/health"}
