"""公开的进程自身健康检查，不访问数据库或三方。"""

from fastapi import APIRouter

from deskbot_server.controller.decorators import access
from deskbot_server.controller.decorators.http import SopAPIRoute

router = APIRouter(route_class=SopAPIRoute, tags=["health"])


@router.get("/health")
@access("public")
def health():
    return {"status": "ok"}
