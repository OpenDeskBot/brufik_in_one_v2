"""顶栏设备选择器相关 API 测试：在线状态 / 最近访问 / 最近同步时间字段。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(db_path)
        init_database()
        yield db_path


def test_fetch_live_device_details_formats_registry_ts(monkeypatch):
    """同进程注册表快照（last_seen_ts / last_pb_ack_ts 时间戳）→ 格式化为时间字符串。"""
    from deskbot_server.web import helpers

    ts = 1785300000.0
    rows = [
        {
            "device_id": "brfk_online",
            "online": True,
            "last_seen_ts": ts,
            "last_pb_ack_ts": ts - 60,
        },
        {
            "device_id": "brfk_offline",
            "online": False,
            "last_seen_ts": 0.0,
            "last_pb_ack_ts": 0.0,
        },
    ]
    monkeypatch.setattr(helpers, "_fetch_registry_devices", lambda **kw: rows)
    out = helpers.fetch_live_device_details(user_id="any")
    assert out["brfk_online"]["online"] is True
    assert out["brfk_online"]["last_seen"] != "—"
    assert out["brfk_online"]["last_sync"] != "—"
    assert out["brfk_offline"]["online"] is False
    assert out["brfk_offline"]["last_seen"] == "—"
    assert out["brfk_offline"]["last_sync"] == "—"


def test_fetch_live_device_details_upstream_string(monkeypatch):
    """上游 /api/devices（last_seen 已是字符串）格式兼容。"""
    from deskbot_server.web import helpers

    rows = [
        {"device_id": "brfk_up", "online": True, "last_seen": "2026-08-30 10:00:00"},
    ]
    monkeypatch.setattr(helpers, "_fetch_registry_devices", lambda **kw: None)
    monkeypatch.setattr(helpers, "_fetch_upstream_devices", lambda **kw: rows)
    out = helpers.fetch_live_device_details()
    assert out["brfk_up"]["last_seen"] == "2026-08-30 10:00:00"
    assert out["brfk_up"]["last_sync"] == "—"


def test_api_list_devices_returns_live_fields(temp_db, monkeypatch):
    """/app/api/devices 每台设备返回 online / last_seen / last_sync。"""
    from deskbot_server.web.app import create_app
    from deskbot_server.web.blueprints import app_bp
    from tests._auth_compat import create_user
    from tests.device_bind_helpers import bind_device_online

    user = create_user("topbar@example.com", "password1234")
    bind_device_online(user.id, "brfk_topbar")
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": "topbar@example.com", "password": "password1234"})

    # 测试进程无 runtime，实时状态走不到注册表，直接 mock 实时查询
    monkeypatch.setattr(
        app_bp,
        "fetch_live_device_details",
        lambda **kw: {
            "brfk_topbar": {
                "online": True,
                "last_seen": "2026-08-30 10:00:00",
                "last_sync": "2026-08-30 10:00:10",
            }
        },
    )
    resp = client.get("/app/api/devices")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    dev = next(d for d in data["devices"] if d["device_id"] == "brfk_topbar")
    assert dev["online"] is True
    assert dev["last_seen"] == "2026-08-30 10:00:00"
    assert dev["last_sync"] == "2026-08-30 10:00:10"
    assert dev["is_current"] is False
    assert data["current_device_id"] is None


def test_api_list_devices_without_live_map(temp_db):
    """实时注册表无该设备时，online 为 False、时间为占位符。"""
    from deskbot_server.web.app import create_app
    from tests._auth_compat import create_user
    from tests.device_bind_helpers import bind_device_online

    user = create_user("topbar2@example.com", "password1234")
    bind_device_online(user.id, "brfk_quiet")
    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": "topbar2@example.com", "password": "password1234"})

    resp = client.get("/app/api/devices")
    data = resp.get_json()
    dev = next(d for d in data["devices"] if d["device_id"] == "brfk_quiet")
    assert dev["online"] is False
    assert dev["last_seen"] == "—"
    assert dev["last_sync"] == "—"
