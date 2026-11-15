"""pytest 共享 fixture 与设备绑定辅助。"""

from __future__ import annotations

import pytest

from tests.device_bind_helpers import bind_device_online, mark_device_online

__all__ = ["bind_device_online", "mark_device_online"]


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """所有测试默认使用独立库；专用迁移 fixture 可继续覆盖，禁止复用已删除的临时库。"""
    import shutil

    from deskbot_server.db.engine import init_engine, reset_engine
    from deskbot_server.db.models import Base
    from deskbot_server.service import device_data_paths
    from deskbot_server.utils.paths import DATA_DIR

    monkeypatch.setenv("DESKBOT_DB_PATH", str(tmp_path / "isolated.db"))
    runtime_data = tmp_path / "sop_runtime_data"
    runtime_data.mkdir()
    for section in ("", "global"):
        target = runtime_data / section
        target.mkdir(exist_ok=True)
        for source in (DATA_DIR / section).glob("*"):
            if source.is_file() and source.suffix in (".json", ".txt"):
                shutil.copy2(source, target / source.name)
    monkeypatch.setattr(device_data_paths, "DATA_DIR", runtime_data)
    reset_engine()
    Base.metadata.create_all(init_engine())
    yield
    reset_engine()


def pytest_configure(config):
    del config


def pytest_runtest_teardown(item, nextitem):
    del item, nextitem
    from deskbot_server.service.device_ws_service import DeviceWsService

    svc = DeviceWsService.instance()
    if svc is not None:
        svc._devices.clear()
