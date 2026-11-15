"""HTTP 控制器认证与设备隔离回归：使用真实会话签名、数据库和应用中间件。"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import text

from deskbot_server.auth.debug_ws_token import issue_debug_ws_token
from deskbot_server.controller import runtime, web_controller
from deskbot_server.controller.app import create_fastapi_app
from deskbot_server.dao import device_mapper, user_mapper
from deskbot_server.db import init_database
from deskbot_server.db.engine import get_session, reset_engine
from deskbot_server.service import device_data_paths as device_data

SECRET = "controller-auth-test-secret"
SETTINGS = [
    ("/api/asr_auto_reply", {"enabled": "0"}, "auto_reply", False),
    ("/api/live_service", {"enabled": "0"}, "live_mode", False),
    ("/api/camera_servo_auto_mode", {"mode": "gaze"}, "servo_mode", "gaze"),
    ("/api/debug_prefs", {"asr_auto_reply": "0", "live_service": "0"}, "auto_reply", False),
]


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKBOT_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DESKBOT_WEB_SECRET_KEY", SECRET)
    monkeypatch.setattr(device_data, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_controller, "SERVO_CFG_FILE", str(tmp_path / "servo.json"))
    reset_engine()
    init_database()
    for uid in ("owner", "other"):
        user_mapper.create(uid, f"{uid}@example.com", "unused-password-hash", False)
        device_mapper.insert(f"pk-{uid}", f"device-{uid}", uid, uid)

    rt = SimpleNamespace(
        ws_path="/asr_chat",
        scheduler=None,
        chat=SimpleNamespace(),
        device_ws=SimpleNamespace(
            shutdown=AsyncMock(),
            snapshot=Mock(return_value=[{"device_id": "device-owner"}, {"device_id": "device-other"}]),
        ),
        bus_service=SimpleNamespace(
            max_events=100,
            snapshot=Mock(
                return_value=[{"device_id": "device-other", "seq": 1}, {"device_id": "device-owner", "seq": 2}]
            ),
        ),
    )
    monkeypatch.setattr(runtime, "_RUNTIME", rt)
    try:
        with TestClient(create_fastapi_app(rt)) as client:
            yield client
    finally:
        reset_engine()


def _login(client, user_id="owner", **session):
    payload = base64.b64encode(json.dumps({"user_id": user_id, **session}).encode())
    client.cookies.set("deskbot_session", TimestampSigner(SECRET).sign(payload).decode())


@pytest.mark.parametrize("path", ["/api/devices", *(row[0] for row in SETTINGS), "/api/servo_config"])
def test_anonymous_http_api_is_rejected_before_business(api_client, path):
    response = api_client.get(path, params={"device_id": "device-owner", "enabled": "0"})
    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "unauthorized"}
    runtime.get_runtime().device_ws.snapshot.assert_not_called()
    assert device_mapper.get_auto_reply("device-owner") is True


@pytest.mark.parametrize("credential", ["cookie", "token"])
@pytest.mark.parametrize("user_state", ["missing", "disabled"])
def test_stale_identity_is_rejected(api_client, credential, user_state):
    uid = "deleted-user" if user_state == "missing" else "owner"
    if user_state == "disabled":
        session = get_session()
        session.execute(text("UPDATE users SET is_active = 0 WHERE id = :uid"), {"uid": uid})
        session.commit()
    headers = {}
    if credential == "cookie":
        _login(api_client, uid)
    else:
        headers["X-Deskbot-Debug-Token"] = issue_debug_ws_token(uid).token
    assert api_client.get("/api/devices", headers=headers).status_code == 401
    runtime.get_runtime().device_ws.snapshot.assert_not_called()


@pytest.mark.parametrize("credential", ["cookie", "token"])
def test_tampered_credentials_are_rejected(api_client, credential):
    if credential == "cookie":
        _login(api_client)
        api_client.cookies.set("deskbot_session", api_client.cookies.get("deskbot_session") + "invalid")
        response = api_client.get("/api/devices")
    else:
        response = api_client.get("/api/devices", headers={"X-Deskbot-Debug-Token": "invalid"})
    assert response.status_code == 401
    runtime.get_runtime().device_ws.snapshot.assert_not_called()


@pytest.mark.parametrize("credential", ["cookie", "header-token", "query-token"])
def test_valid_identity_only_lists_owned_devices(api_client, credential):
    kwargs = {}
    if credential == "cookie":
        _login(api_client)
    else:
        token = issue_debug_ws_token("owner").token
        kwargs = (
            {"headers": {"X-Deskbot-Debug-Token": token}}
            if credential == "header-token"
            else {"params": {"debug_token": token}}
        )
    response = api_client.get("/api/devices", **kwargs)
    assert response.status_code == 200
    assert response.json()["devices"] == [{"device_id": "device-owner"}]


@pytest.mark.parametrize(("path", "updates", "field", "value"), SETTINGS)
@pytest.mark.parametrize("write", [False, True])
def test_other_users_device_settings_are_forbidden(api_client, path, updates, field, value, write):
    _login(api_client)
    params = {"device_id": "device-other", **(updates if write else {})}
    before = device_mapper.get_by_device_id("device-other")
    response = api_client.get(path, params=params)
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden_device"
    after = device_mapper.get_by_device_id("device-other")
    assert (after.auto_reply, after.live_mode, after.servo_mode) == (
        before.auto_reply,
        before.live_mode,
        before.servo_mode,
    )


@pytest.mark.parametrize(("path", "updates", "field", "value"), SETTINGS)
def test_owner_can_read_and_update_device_settings(api_client, path, updates, field, value):
    _login(api_client)
    assert api_client.get(path, params={"device_id": "device-owner"}).status_code == 200
    response = api_client.get(path, params={"device_id": "device-owner", **updates})
    assert response.status_code == 200
    assert getattr(device_mapper.get_by_device_id("device-owner"), field) == value


def test_current_session_device_can_still_be_updated(api_client):
    _login(api_client, current_device_id="device-owner")
    assert api_client.get("/api/asr_auto_reply", params={"enabled": "0"}).status_code == 200
    assert device_mapper.get_auto_reply("device-owner") is False


@pytest.mark.parametrize("method", ["GET", "POST-query", "POST-body"])
def test_other_users_servo_config_is_forbidden_before_file_access(api_client, monkeypatch, method):
    _login(api_client)
    read = Mock()
    write = Mock()
    monkeypatch.setattr(web_controller, "load_servo_cfg_file", read)
    monkeypatch.setattr(web_controller, "save_servo_cfg_file", write)
    if method == "GET":
        response = api_client.get("/api/servo_config", params={"device_id": "device-other"})
    elif method == "POST-query":
        response = api_client.post("/api/servo_config?device_id=device-other", json={"presets": []})
    else:
        response = api_client.post("/api/servo_config", json={"device_id": "device-other", "presets": []})
    assert response.status_code == 403
    read.assert_not_called()
    write.assert_not_called()


def test_owner_can_save_and_read_servo_config(api_client):
    _login(api_client)
    response = api_client.post(
        "/api/servo_config",
        json={
            "device_id": "device-owner",
            "presets": [],
            "xMin": 0,
            "xMax": 180,
            "yMin": 70,
            "yMax": 110,
            "xReverse": 0,
            "yReverse": 0,
        },
    )
    assert response.status_code == 200
    response = api_client.get("/api/servo_config", params={"device_id": "device-owner"})
    assert response.status_code == 200
    assert response.json()["config"]["presets"] == []


@pytest.mark.parametrize("action", ["append", "replace", "default"])
def test_servo_dispatch_preserves_requested_queue_policy(api_client, action):
    _login(api_client)
    rt = runtime.get_runtime()
    rt.device_ws.send = AsyncMock(return_value=1)
    response = api_client.get(
        "/api/device_servo",
        params={"device_id": "device-owner", "dyaw": 2, "dpitch": 0, "xm": 1, "ym": 1, "ms": 100, "action": action},
    )
    assert response.status_code == 200 and response.json()["delivered"] == 1
    dev, seq = rt.device_ws.send.call_args.args
    assert dev == "device-owner" and seq.action.wire == action
    assert seq.req == response.json()["req"] == seq.entries[0].req


@pytest.mark.parametrize("action", ["append", "replace", "default"])
def test_animation_preview_preserves_requested_queue_policy(api_client, action):
    _login(api_client)
    rt = runtime.get_runtime()
    rt.device_ws.send = AsyncMock(return_value=1)
    response = api_client.post(
        "/api/device_pb_anim",
        json={"device_id": "device-owner", "chunk_ms": 100, "action": action, "anim": {"elements": {}}},
    )
    assert response.status_code == 200
    assert rt.device_ws.send.call_args.args[1].action.wire == action


@pytest.mark.parametrize("angle", ["nan", "inf", "-inf"])
def test_servo_rejects_nonfinite_angles_before_dispatch(api_client, angle):
    _login(api_client)
    rt = runtime.get_runtime()
    rt.device_ws.send = AsyncMock(return_value=1)
    response = api_client.get("/api/device_servo", params={"device_id": "device-owner", "dyaw": angle})
    assert response.status_code == 400
    rt.device_ws.send.assert_not_called()


def test_servo_scene_has_one_request_and_no_inline_rendering(api_client, monkeypatch):
    _login(api_client)
    monkeypatch.setattr(
        "deskbot_server.dao.face_asset_catalog.find_face_asset",
        lambda _: ({"id": "curious", "frame_ms": [80, 120]}, "test-version"),
    )
    rt = runtime.get_runtime()
    rt.device_ws.send = AsyncMock(return_value=1)
    response = api_client.get(
        "/api/device_servo",
        params={
            "device_id": "device-owner",
            "dyaw": 2,
            "xm": 1,
            "ym": 1,
            "ms": 100,
            "action": "append",
            "with_scene": "curious",
        },
    )
    assert response.status_code == 200
    body = response.json()
    seq = rt.device_ws.send.call_args.args[1]
    assert body["scene_req"] == body["req"] == seq.req
    assert body["scene_frames"] == 2 and body["type"] == "pb_start"
    assert seq.action.wire == "append"
    assert [b.idx for b in seq.entries] == [0, 1, 2]
    assert [b.type.wire for b in seq.entries] == ["pb_start", "pb_chunk", "pb_end"]
    assert {b.req for b in seq.entries} == {seq.req}
    assert seq.entries[0].anim[0].ms == 100  # Wait for the servo before the scene.
    assert [b.anim[0].anim_id for b in seq.entries[1:]] == ["curious", "curious"]
    assert all(not a.elements for b in seq.entries for a in b.anim)


def test_pipeline_without_device_filters_ownership_before_limit(api_client):
    _login(api_client)
    response = api_client.get("/api/pipeline_recent", params={"limit": 1})
    assert response.status_code == 200
    assert response.json()["items"] == [{"device_id": "device-owner", "seq": 2}]


def test_anonymous_post_does_not_write_servo_config(api_client, monkeypatch):
    write = Mock()
    monkeypatch.setattr(web_controller, "save_servo_cfg_file", write)
    response = api_client.post("/api/servo_config", json={"device_id": "device-owner", "presets": []})
    assert response.status_code == 401
    write.assert_not_called()


def test_other_users_pipeline_events_are_forbidden(api_client):
    _login(api_client)
    response = api_client.get("/api/pipeline_recent", params={"device_id": "device-other"})
    assert response.status_code == 403
    runtime.get_runtime().bus_service.snapshot.assert_not_called()


def test_device_access_check_rejects_anonymous_user():
    from deskbot_server.controller.auth import device_access_denied

    response = device_access_denied(None, "device-owner")
    assert response is not None
    assert response.status_code == 401


def test_health_remains_public(api_client):
    assert api_client.get("/health").status_code == 200
