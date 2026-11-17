"""进程内共享运行时（lifespan 装配）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deskbot_server.model.settings import AppSettings
    from deskbot_server.service.bus_service import BusService
    from deskbot_server.service.device_ws_service import DeviceWsService


@dataclass
class AppRuntime:
    settings: AppSettings
    ws_path: str
    bus_service: BusService
    device_ws: DeviceWsService
    scheduler: object | None = None
    perception_services: object | None = None


_RUNTIME: AppRuntime | None = None


def set_runtime(runtime: AppRuntime) -> None:
    global _RUNTIME
    _RUNTIME = runtime


def get_runtime() -> AppRuntime:
    if _RUNTIME is None:
        raise RuntimeError("AppRuntime 未初始化")
    return _RUNTIME
