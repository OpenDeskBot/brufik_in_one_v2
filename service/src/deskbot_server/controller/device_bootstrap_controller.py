"""Public device bootstrap contains no user data, credentials or API keys."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from deskbot_server.controller.decorators import access
from deskbot_server.controller.decorators.http import SopAPIRoute
from deskbot_server.service.device_bootstrap_service import DeviceBootstrapService

router = APIRouter(route_class=SopAPIRoute, tags=["device-bootstrap"])


@router.get("/api/device/bootstrap")
@access("public")
def bootstrap(request: Request, device_id: str):
    try:
        data = DeviceBootstrapService().bootstrap(device_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # URLs are relative: reverse-proxy host/prefix is not inferred from untrusted headers.
    return JSONResponse(dict(code=0, message="ok", data=data), headers={"Cache-Control": "no-cache"})


@router.get("/api/device/face-pack/{version}")
@access("public")
def face_pack(version: str, request: Request):
    try:
        _, payload = DeviceBootstrapService().package(version)
    except KeyError as exc:
        raise HTTPException(404, "face package unavailable; refresh bootstrap") from exc
    import hashlib

    etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=31536000, immutable"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type="application/json", headers=headers)
