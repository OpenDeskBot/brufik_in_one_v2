"""统计 → 异常转换 → 权限 → 接口；同时覆盖同步依赖与异步业务异常。"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from deskbot_server.controller.decorators.access import enforce_access
from deskbot_server.db.engine import remove_session, request_scope
from deskbot_server.model.errors import ClientError, DataAccessError, ServiceError
from deskbot_server.utils.async_helpers import run_blocking

logger = logging.getLogger("deskbot-server")
current_request: ContextVar[Request | None] = ContextVar("current_http_request", default=None)


def error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        detail = exc.detail
        payload = detail if isinstance(detail, dict) else {"ok": False, "error": str(detail)}
        return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)
    if isinstance(exc, RequestValidationError):
        return JSONResponse({"ok": False, "error": "invalid_request"}, status_code=422)
    if isinstance(exc, ServiceError):
        return JSONResponse({"ok": False, "error": exc.code, "message": str(exc)}, status_code=400)
    if isinstance(exc, DataAccessError) and exc.kind == "conflict":
        return JSONResponse({"ok": False, "error": "data_conflict"}, status_code=409)
    if isinstance(exc, ClientError):
        return JSONResponse({"ok": False, "error": "upstream_error"}, status_code=502)
    logger.exception(
        "HTTP request failed request_id=%s",
        getattr(current_request.get().state, "request_id", None) if current_request.get() else None,
        exc_info=exc,
    )
    return JSONResponse({"ok": False, "error": "internal_error", "message": "服务暂时不可用"}, status_code=500)


class SopAPIRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                kind = getattr(self.endpoint, "__access_kind__", None)
                if kind is None:
                    raise RuntimeError(f"route missing access declaration: {self.path}")
                await run_blocking(enforce_access, request, kind)
                response = await original(request)
                if response.status_code >= 500 and "application/json" in response.headers.get("content-type", ""):
                    # Historical handlers sometimes converted an exception to str(exc).
                    # Keep the status/shape while making internal details private.
                    payload = json.loads(response.body)
                    if isinstance(payload, dict):
                        payload["error"] = "upstream_error" if response.status_code == 502 else "internal_error"
                        payload["message"] = "服务暂时不可用"
                        logger.error(
                            "handled_http_failure request_id=%s status=%s",
                            request.state.request_id,
                            response.status_code,
                        )
                        return JSONResponse(payload, status_code=response.status_code)
                return response
            except Exception as exc:
                return error_response(exc)

        return handle


class RequestMiddleware:
    """ASGI 切面保证 404/405、权限拒绝、异常与流式响应都记录最终状态。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        token = current_request.set(request)
        db_token = request_scope.set(object())
        started, status = False, 500
        begin = time.perf_counter()

        async def send_response(message):
            nonlocal started, status
            if message["type"] == "http.response.start":
                started, status = True, message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                message["headers"] = [*headers, (b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        except Exception as exc:
            if started:
                logger.exception("response stream failed request_id=%s", request_id)
                raise
            await error_response(exc)(scope, receive, send_response)
        finally:
            route = getattr(scope.get("route"), "path", scope.get("path", ""))
            logger.info(
                "http_request request_id=%s method=%s route=%s status=%d duration_ms=%.3f",
                request_id,
                scope["method"],
                route,
                status,
                (time.perf_counter() - begin) * 1000,
            )
            remove_session()
            request_scope.reset(db_token)
            current_request.reset(token)
