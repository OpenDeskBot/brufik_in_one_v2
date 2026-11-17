"""FastAPI 应用工厂。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from deskbot_server.controller import device_bootstrap_controller, health_controller
from deskbot_server.controller.decorators.http import RequestMiddleware
from deskbot_server.controller.device_controller import router as device_router
from deskbot_server.controller.flash_controller import router as flash_router
from deskbot_server.controller.registry import register_routers
from deskbot_server.controller.runtime import AppRuntime, set_runtime
from deskbot_server.controller.web_controller import router as web_router
from deskbot_server.web.mount import mount_web

logger = logging.getLogger("deskbot-server")


def create_fastapi_app(runtime: AppRuntime | None = None, *, web_only: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if runtime is not None:
            set_runtime(runtime)
            app.state.perception_services = getattr(runtime, "perception_services", None)
            if app.state.perception_services is not None:
                app.state.perception_services.start_watchdog()
                # Model loading stays outside the HTTP startup critical path.
                app.state.perception_startup = asyncio.create_task(app.state.perception_services.apply_auto_start())
            if runtime.scheduler is not None and callable(getattr(runtime.scheduler, "start", None)):
                runtime.scheduler.start()
            logger.info("FastAPI started ws_path=%s", runtime.ws_path)
        try:
            yield
        finally:
            if runtime is not None and runtime.scheduler is not None:
                stop = getattr(runtime.scheduler, "aclose", None) or getattr(runtime.scheduler, "stop", None)
                if callable(stop):
                    try:
                        result = stop()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.exception("scheduler.stop failed")
            if runtime is not None:
                startup = getattr(app.state, "perception_startup", None)
                if startup is not None:
                    startup.cancel()
                    await asyncio.gather(startup, return_exceptions=True)
                manager = getattr(app.state, "perception_services", None)
                try:
                    await runtime.device_ws.shutdown()
                except Exception:
                    logger.exception("device_ws.shutdown failed")
                if manager is not None:
                    await manager.shutdown()
                from deskbot_server.client.registry import aclose_clients
                await aclose_clients()
            logger.info("FastAPI stopped")

    app = FastAPI(title="deskbot-server", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Deskbot-Web-Token", "X-Deskbot-Debug-Token"],
    )

    mount_web(app)
    register_routers(app, [health_controller, flash_router, device_bootstrap_controller])
    if runtime is not None and not web_only:
        register_routers(app, [device_router, web_router])
        ws_path = runtime.ws_path if runtime.ws_path.startswith("/") else f"/{runtime.ws_path}"
        if ws_path != "/asr_chat":
            from deskbot_server.controller.device_controller import asr_chat

            alias = APIRouter()
            alias.add_api_websocket_route(ws_path, asr_chat)
            register_routers(app, [alias])
        app.state.runtime = runtime
    app.add_middleware(RequestMiddleware)
    return app
