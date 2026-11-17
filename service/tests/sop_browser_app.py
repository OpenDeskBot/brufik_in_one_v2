"""浏览器验收专用应用：无模型、无硬件，数据库必须由环境指定为临时库。"""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from deskbot_server.config import load_config
from deskbot_server.controller.app import create_fastapi_app
from deskbot_server.model.settings import AppSettings


def create_app():
    # Browser uploads belong beside the disposable database, never in real device data.
    from deskbot_server.service import device_data_paths

    data = Path(os.environ["DESKBOT_DB_PATH"]).parent / "runtime-data"
    for section in ("", "global"):
        target = data / section
        target.mkdir(parents=True, exist_ok=True)
        for source in (device_data_paths.DATA_DIR / section).glob("*"):
            if source.is_file() and source.suffix in {".json", ".txt"} and not (target / source.name).exists():
                shutil.copy2(source, target / source.name)
    device_data_paths.DATA_DIR = data
    settings = AppSettings.from_config(load_config())
    runtime = SimpleNamespace(
        ws_path="/asr_chat", settings=settings, scheduler=None,
        chat=SimpleNamespace(settings=settings, aclose=AsyncMock()),
        device_ws=SimpleNamespace(snapshot=lambda: [], shutdown=AsyncMock()),
        bus_service=SimpleNamespace(max_events=100, snapshot=lambda *args: []),
    )
    return create_fastapi_app(runtime)
