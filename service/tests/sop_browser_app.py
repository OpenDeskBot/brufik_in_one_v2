"""浏览器验收专用应用：无模型、无硬件，数据库必须由环境指定为临时库。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from deskbot_server.config import load_config
from deskbot_server.controller.app import create_fastapi_app
from deskbot_server.model.settings import AppSettings


def create_app():
    settings = AppSettings.from_config(load_config())
    runtime = SimpleNamespace(
        ws_path="/asr_chat", settings=settings, scheduler=None,
        chat=SimpleNamespace(settings=settings, aclose=AsyncMock()),
        device_ws=SimpleNamespace(snapshot=lambda: [], shutdown=AsyncMock()),
        bus_service=SimpleNamespace(max_events=100, snapshot=lambda *args: []),
    )
    return create_fastapi_app(runtime)
