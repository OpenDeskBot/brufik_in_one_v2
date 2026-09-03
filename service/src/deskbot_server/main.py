from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from deskbot_server.config import load_config
from deskbot_server.constants import CAMERA_VIEW_PATH, DEVICE_PIPELINE_PATH
from deskbot_server.controller.app import create_fastapi_app
from deskbot_server.controller.runtime import AppRuntime
from deskbot_server.infrastructure.bootstrap import build_chat_service
from deskbot_server.model.settings import AppSettings
from deskbot_server.service.application.scheduled_task_scheduler import ScheduledTaskScheduler
from deskbot_server.service.bus_service import BusService
from deskbot_server.service.camera_face_service import CameraFaceService, build_camera_face_runtime
from deskbot_server.service.device_ws_service import DeviceWsService
from deskbot_server.service.live_service import (
    ENTER_SEC,
    QUEST_ATTEMPT_IDLE_SEC,
    SLEEP_MAX_SEC,
    SLEEP_MIN_SEC,
    WANDER_MAX_CYCLES,
    LiveService,
)
from deskbot_server.service.pipeline.audio import AudioConfig
from deskbot_server.service.vad_service import VadService
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
    app_settings = AppSettings.from_config(config)
    audio_cfg = AudioConfig(
        input_codec=app_settings.audio.input_codec,
        sample_rate=app_settings.audio.sample_rate,
        channels=app_settings.audio.channels,
        min_speech_ms=app_settings.vad.min_speech_ms,
        max_silence_ms=app_settings.vad.max_silence_ms,
        pre_speech_ms=app_settings.vad.pre_speech_ms,
        silero_model_path=app_settings.vad.silero_model_path,
        silero_threshold=app_settings.vad.silero_threshold,
        silero_threshold_low=app_settings.vad.silero_threshold_low,
    )
    logger.info(
        "[VAD/AUDIO] codec=%s sample_rate=%d channels=%d | silero "
        "min_speech_ms=%d max_silence_ms=%d pre_speech_ms=%d "
        "threshold=%.2f threshold_low=%.2f | "
        "asr_text_filter: min_text_len=%s min_chinese_ratio=%s",
        audio_cfg.input_codec,
        audio_cfg.sample_rate,
        audio_cfg.channels,
        audio_cfg.min_speech_ms,
        audio_cfg.max_silence_ms,
        audio_cfg.pre_speech_ms,
        audio_cfg.silero_threshold,
        audio_cfg.silero_threshold_low,
        config.get("asr", {}).get("text_filter", {}).get("min_text_len"),
        config.get("asr", {}).get("text_filter", {}).get("min_chinese_ratio"),
    )
    pipeline = build_chat_service(config)
    VadService().configure(audio_cfg)
    bus_service = BusService()

    device_ws = DeviceWsService()
    device_ws.bind(pipeline, audio_cfg, bus_service=bus_service)
    live_svc = LiveService()
    live_svc.bind(device_ws)
    device_ws.bind_live_service(live_svc)
    from deskbot_server.service.application.quest_proactive import QuestProactiveRunner

    quest_runner = QuestProactiveRunner(chat=pipeline, device_ws=device_ws, bus_service=bus_service)
    live_svc.bind_quest_runner(quest_runner)

    logger.info(
        "[server] live_mode: per-device (DB), 无有效对话 %.1fs 后 wander，1-%d 轮后 sleep %.0f-%.0fs，gaze 优先；"
        "剧情主动推进: 用户在且冷场 >=%.0fs 时尝试",
        ENTER_SEC,
        WANDER_MAX_CYCLES,
        SLEEP_MIN_SEC,
        SLEEP_MAX_SEC,
        QUEST_ATTEMPT_IDLE_SEC,
    )
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

    scheduler = ScheduledTaskScheduler(
        chat=pipeline, device_ws=device_ws, bus_service=bus_service
    )
    scheduler.start()

    from deskbot_server.service.external.manager import ExternalServiceManager

    external_manager = ExternalServiceManager()
    external_manager.discover()
    logger.info("[external] 发现 %d 个外部服务", len(external_manager.names()))

    return AppRuntime(
        settings=app_settings,
        chat=pipeline,
        audio_cfg=audio_cfg,
        ws_path=ws_path,
        bus_service=bus_service,
        device_ws=device_ws,
        scheduler=scheduler,
        external_manager=external_manager,
    )


async def _apply_auto_start_safe(external_manager) -> None:
    try:
        await external_manager.apply_auto_start()
    except Exception:
        logger.exception("[external] auto_start 拉起失败")


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

    external_manager = runtime.external_manager
    if external_manager is not None:
        external_manager.start_watchdog()
        # 非阻塞拉起 auto_start 服务；失败仅记日志，不阻塞主服务启动
        asyncio.create_task(
            _apply_auto_start_safe(external_manager),
            name="external-auto-start",
        )
    try:
        await server.serve()
    finally:
        await runtime.device_ws.shutdown()
