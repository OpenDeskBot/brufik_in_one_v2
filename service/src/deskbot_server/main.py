from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from deskbot_server.config import configure_runtime, load_config
from deskbot_server.constants import CAMERA_VIEW_PATH, DEVICE_PIPELINE_PATH
from deskbot_server.controller.app import create_fastapi_app
from deskbot_server.controller.runtime import AppRuntime
from deskbot_server.model.settings import AppSettings
from deskbot_server.service.application.external_reminder_scheduler import ExternalReminderScheduler
from deskbot_server.service.bus_service import BusService
from deskbot_server.service.camera_face_service import CameraFaceService, build_camera_face_runtime
from deskbot_server.service.device_ws_service import DeviceWsService
from deskbot_server.service.live_service import LiveService
from deskbot_server.service.perception_services import build_perception_services
from deskbot_server.service.voiceprint_service import VoiceprintService, build_voiceprint_runtime
from deskbot_server.utils.env import load_dotenv

logger = logging.getLogger("deskbot-server")


def build_runtime() -> AppRuntime:
    load_dotenv()
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import default_db_path

    init_database()
    logger.info("[server] auth DB ready path=%s", default_db_path())
    config = load_config(os.environ.get("DESKBOT_SERVER_CONFIG", "config.yaml"))
    configure_runtime(config, os.environ.get("DESKBOT_SERVER_CONFIG", "config.yaml"))
    app_settings = AppSettings.from_config(config)
    bus_service = BusService()
    device_ws = DeviceWsService()
    device_ws.bind(bus_service=bus_service)
    live_svc = LiveService()
    live_svc.bind(device_ws)
    device_ws.bind_live_service(live_svc)
    logger.info("[server] conversation: external Flecket/MOSS only; local ASR/LLM/TTS pipeline removed")
    camera_face_runtime = build_camera_face_runtime(config)
    CameraFaceService().configure(camera_face_runtime)
    voiceprint_runtime = build_voiceprint_runtime(config)
    VoiceprintService().configure(voiceprint_runtime)
    logger.info(
        "[server] send_face_info_to_asr_chat=%s（device_pb_only 为 true 时强制关闭；仅经 /asr_chat camera_frame 生效）",
        app_settings.server.send_face_info_to_asr_chat,
    )

    ws_path = app_settings.server.ws_path
    if not ws_path.startswith("/"):
        ws_path = f"/{ws_path}"

    return AppRuntime(
        settings=app_settings,
        ws_path=ws_path,
        bus_service=bus_service,
        device_ws=device_ws,
        perception_services=build_perception_services(),
        scheduler=ExternalReminderScheduler(device_ws, interval=(config.get("scheduler") or {}).get("external_poll_interval_sec", 5))
        if (config.get("scheduler") or {}).get("enabled", True) else None,
    )


async def main():
    runtime = build_runtime()
    app = create_fastapi_app(runtime)
    host = runtime.settings.server.host
    port = runtime.settings.server.port
    ping_interval = runtime.settings.server.ws_ping_interval
    if ping_interval is not None:
        ping_interval = float(max(5, ping_interval))
    ping_timeout = float(max(5, runtime.settings.server.ws_ping_timeout))

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(
        lambda _loop, context: logger.error(
            "未捕获事件循环异常: %s", context.get("message", "unknown"), exc_info=context.get("exception")
        )
    )

    logger.info(
        "deskbot-server FastAPI/uvicorn on http://%s:%s (asr=%s, camera_view=%s, "
        "device_pipeline=%s; ws_ping_interval=%s ws_ping_timeout=%s)",
        host,
        port,
        runtime.ws_path,
        CAMERA_VIEW_PATH,
        DEVICE_PIPELINE_PATH,
        ping_interval,
        ping_timeout,
    )

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        ws_ping_interval=ping_interval,
        ws_ping_timeout=ping_timeout,
        # ESP32 可能推较大 JPEG / PCM 帧
        ws_max_size=None,
    )
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        await runtime.device_ws.shutdown()
