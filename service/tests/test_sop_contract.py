"""SOP 行为验收：路由拒绝、统一切面、静态网页、事务与调度生命周期。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.websockets import WebSocketDisconnect

from deskbot_server.auth.debug_ws_token import issue_debug_ws_token
from deskbot_server.controller.app import create_fastapi_app
from deskbot_server.controller.decorators import access
from deskbot_server.controller.decorators.http import RequestMiddleware, SopAPIRoute
from deskbot_server.controller.registry import register_routers
from deskbot_server.dao import device_mapper, user_mapper
from deskbot_server.dao.decorators import select_one
from deskbot_server.db import init_database
from deskbot_server.db.engine import get_session, reset_engine
from deskbot_server.db.transaction import transaction
from deskbot_server.model.errors import ClientError, DataAccessError, TransactionError


@pytest.fixture()
def database(tmp_path, monkeypatch):
    from deskbot_server.service import device_data_paths as device_data

    monkeypatch.setenv("DESKBOT_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DESKBOT_WEB_SECRET_KEY", "sop-contract-secret")
    monkeypatch.setattr(device_data, "DATA_DIR", tmp_path / "data")
    reset_engine()
    init_database()
    user_mapper.create("owner", "owner@example.com", "private-password-hash", True)
    user_mapper.create("member", "member@example.com", "another-private-hash", False)
    device_mapper.insert("pk", "device-owner", "owner", "Device")
    yield tmp_path
    reset_engine()


def test_registration_rejects_missing_access_before_startup():
    router = APIRouter(route_class=SopAPIRoute)

    @router.get("/missing")
    def missing():
        return {}

    with pytest.raises(ValueError, match="missing access declaration.*missing"):
        register_routers(FastAPI(), [router])


def test_registration_rejects_duplicate_method_path_but_allows_different_methods():
    first, second = APIRouter(route_class=SopAPIRoute), APIRouter(route_class=SopAPIRoute)

    @first.get("/same")
    @access("public")
    def one():
        return {}

    @second.get("/same")
    @access("public")
    def two():
        return {}

    with pytest.raises(ValueError, match="duplicate route GET /same"):
        register_routers(FastAPI(), [first, second])
    different = APIRouter(route_class=SopAPIRoute)
    different.add_api_route("/same", two, methods=["POST"])
    register_routers(FastAPI(), [first, different])


def test_common_http_aspects_cover_success_denial_errors_and_missing_routes(database, caplog):
    router = APIRouter(route_class=SopAPIRoute)
    calls = []

    @router.get("/sync")
    @access("public")
    def sync():
        raise RuntimeError("secret database password")

    @router.get("/async")
    @access("public")
    async def async_error():
        raise RuntimeError("secret api key")

    @router.get("/private")
    @access("user")
    def private():
        calls.append(True)
        return {"ok": True}

    app = FastAPI()
    register_routers(app, [router])
    app.add_middleware(RequestMiddleware)
    with TestClient(app) as client, caplog.at_level(logging.INFO, logger="deskbot-server"):
        for url, status in (("/sync", 500), ("/async", 500), ("/private", 401), ("/missing", 404)):
            response = client.get(url)
            assert response.status_code == status
            assert len(response.headers["X-Request-ID"]) == 32
            assert "secret" not in response.text
            assert any(
                response.headers["X-Request-ID"] in row.message and f"status={status}" in row.message
                for row in caplog.records
            )
        token = issue_debug_ws_token("owner").token
        assert client.get("/private", headers={"X-Deskbot-Debug-Token": token}).status_code == 200
    assert calls == [True]


def test_static_pages_are_public_but_page_data_is_protected(database):
    with TestClient(create_fastapi_app()) as client:
        for url in ("/", "/login", "/home", "/expr", "/dev/users"):
            response = client.get(url)
            assert response.status_code == 200
            assert response.headers["X-Deskbot-Page"] == "static"
            assert "page-loader.js?v=" in response.text
            assert response.headers["Cache-Control"] == "no-store"
            assert "owner@example.com" not in response.text
        assert client.get("/api/pages/home").status_code == 401
        member = {"X-Deskbot-Debug-Token": issue_debug_ws_token("member").token}
        assert client.get("/api/pages/expr", headers=member).status_code == 403
        assert client.get("/api/pages/home", headers=member).status_code == 200
        owner = {"X-Deskbot-Debug-Token": issue_debug_ws_token("owner").token}
        users = client.get("/api/pages/dev/users", headers=owner)
        assert users.status_code == 200
        assert "private-password-hash" not in users.text
        assert "password_hash" not in users.text
        assert client.get("/health").json() == {"status": "ok"}
        for url in ("/api/unknown", "/static/missing.js", "/unregistered-page"):
            response = client.get(url)
            assert response.status_code == 404
            assert "page-loader.js" not in response.text


def test_published_settings_include_flecket_and_mutable_resources_are_not_cached(database):
    with TestClient(create_fastapi_app()) as client:
        response = client.get("/static/views.json")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        settings = response.json()["app2c/robot_settings.html"]
        for provider in ("moss-live", "flecket"):
            assert f'<option value="{provider}">' in settings
        assert '<option value="local">' not in settings
        for path in ("/static/page-loader.js", "/static/page-renderer.js"):
            resource = client.get(path)
            assert resource.status_code == 200
            assert resource.headers["Cache-Control"] == "no-store"
        loader = client.get("/static/page-loader.js").text
        assert "/static/views.json?v=" in loader
        assert "Promise.all" in loader
        versioned = client.get("/static/views.json?v=test-version")
        assert "immutable" in versioned.headers["Cache-Control"]


def test_service_transaction_commits_together_and_rolls_back_inner_failure(database):
    with transaction() as tx:
        device_mapper.update_volume("device-owner", 42, transaction=tx)
        device_mapper.update_live_mode("device-owner", False, transaction=tx)
    assert device_mapper.get_by_device_id("device-owner").volume == 42
    with pytest.raises(DataAccessError), transaction() as tx:
        device_mapper.update_volume("device-owner", 10, transaction=tx)
        user_mapper.create("owner", "duplicate@example.com", "unused", False, transaction=tx)
    assert device_mapper.get_by_device_id("device-owner").volume == 42
    # A failed standalone write must also release and roll back its own connection.
    with pytest.raises(DataAccessError):
        user_mapper.create("owner", "duplicate@example.com", "unused", False)
    device_mapper.update_volume("device-owner", 43)
    assert device_mapper.get_by_device_id("device-owner").volume == 43


def test_nested_transaction_participant_cannot_commit_after_failure_is_caught(database):
    with pytest.raises(TransactionError), transaction() as outer:
        device_mapper.update_volume("device-owner", 1, transaction=outer)
        try:
            with transaction(outer) as inner:
                assert inner is outer
                raise ValueError("inner failure")
        except ValueError:
            pass
    assert device_mapper.get_by_device_id("device-owner").volume == 80


def test_single_result_query_rejects_multiple_rows(database):
    @select_one("SELECT id FROM users")
    def many(): ...

    with pytest.raises(DataAccessError) as caught:
        many()
    assert caught.value.kind == "cardinality"


def test_invalid_combined_preference_does_not_partially_write(database):
    from deskbot_server.service.device_preferences import update_preferences

    with pytest.raises(ValueError):
        update_preferences("device-owner", asr_auto_reply="0", live_service="invalid")
    assert device_mapper.get_auto_reply("device-owner") is True


def test_async_requests_do_not_share_database_session(database):
    router = APIRouter(route_class=SopAPIRoute)
    ready, release = asyncio.Event(), asyncio.Event()
    sessions = []

    @router.get("/scope")
    @access("public")
    async def scope(request: Request):
        session = get_session()
        sessions.append(session)
        if len(sessions) == 2:
            ready.set()
        await release.wait()
        return {"ok": True}

    app = FastAPI()
    register_routers(app, [router])
    app.add_middleware(RequestMiddleware)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            tasks = [asyncio.create_task(client.get("/scope")) for _ in range(2)]
            try:
                await asyncio.wait_for(ready.wait(), 1)
                assert sessions[0] is not sessions[1]
            finally:
                release.set()
                await asyncio.gather(*tasks)

    asyncio.run(run())


def test_schedule_registration_and_overlap_records(caplog):
    from deskbot_server.schedule.executor import ScheduleExecutor, register, scheduled

    @scheduled("test.job", "interval")
    async def job():
        await release.wait()

    with pytest.raises(ValueError, match="duplicate"):
        register([job, job], {"interval": 1})
    with pytest.raises(ValueError, match="invalid"):
        register([job], {})

    async def run():
        nonlocal release
        release = asyncio.Event()
        executor = ScheduleExecutor()
        worker = asyncio.create_task(executor.run("test.job", job))
        await asyncio.sleep(0)
        assert await executor.run("test.job", job) is None
        release.set()
        await worker

    release = None
    with caplog.at_level(logging.INFO, logger="deskbot-server"):
        asyncio.run(run())
    assert any("status=skipped" in row.message and "reason=overlap" in row.message for row in caplog.records)
    assert any("status=success" in row.message and "trigger_id=" in row.message for row in caplog.records)


def test_scheduler_shutdown_drains_running_work(database, monkeypatch):
    from deskbot_server.service.application.scheduled_task_scheduler import ScheduledTaskScheduler

    async def run():
        scheduler = ScheduledTaskScheduler(chat=SimpleNamespace(), device_ws=SimpleNamespace())
        started, release = asyncio.Event(), asyncio.Event()

        async def tick():
            async def work():
                started.set()
                await release.wait()

            worker = asyncio.create_task(work())
            scheduler._workers.add(worker)
            worker.add_done_callback(scheduler._workers.discard)

        monkeypatch.setattr(scheduler, "_tick", tick)
        scheduler.start()
        await asyncio.wait_for(started.wait(), 1)
        closing = asyncio.create_task(scheduler.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 1)
        assert scheduler._task is None
        assert not scheduler._workers

    asyncio.run(run())


def test_configuration_overrides_validate_and_runtime_is_a_snapshot(tmp_path, monkeypatch):
    import deskbot_server.config as config

    path = tmp_path / "config.yaml"
    path.write_text("server:\n  port: 9000\n  host: 127.0.0.1\n")
    monkeypatch.setenv("DESKBOT_SERVER_PORT", "9100")
    cfg = config.load_config(path)
    assert cfg["server"] == {"port": 9100, "host": "127.0.0.1"}
    monkeypatch.setattr(config, "_RUNTIME_CONFIG", None)
    monkeypatch.setattr(config, "_RUNTIME_PATH", None)
    config.configure_runtime(cfg, path)
    path.write_text("server:\n  port: 9200\n")
    assert config.get_config(path)["server"]["port"] == 9100
    monkeypatch.setenv("DESKBOT_SERVER_PORT", "0")
    with pytest.raises(ValueError, match="server.port"):
        config.load_config(path)


@pytest.mark.parametrize("user_id,allowed", [("owner", True), ("member", False), ("deleted", False)])
def test_debug_websocket_checks_identity_and_device_ownership(database, user_id, allowed):
    from deskbot_server.controller.auth import require_web_ws_subscriber_auth

    router = APIRouter()

    @router.websocket("/view")
    @access("subscriber")
    @require_web_ws_subscriber_auth
    async def view(websocket: WebSocket):
        await websocket.send_json({"ok": True})
        await websocket.close()

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="sop-contract-secret")
    register_routers(app, [router])
    token = issue_debug_ws_token(user_id).token
    with TestClient(app) as client, client.websocket_connect(f"/view?device_id=device-owner&debug_token={token}") as ws:
        if allowed:
            assert ws.receive_json() == {"ok": True}
        else:
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_json()
            assert caught.value.code == 1008


def test_tts_connection_failure_is_typed_and_not_automatically_retried(monkeypatch):
    from deskbot_server.client.tts.doubao import DoubaoTtsConfig, DoubaoTtsConnection

    connection = DoubaoTtsConnection(DoubaoTtsConfig(api_key="test", speaker="test"))
    connect = AsyncMock(side_effect=OSError("offline"))
    monkeypatch.setattr(connection, "_ensure_ready", connect)
    monkeypatch.setattr(connection, "_reset", AsyncMock())
    with pytest.raises(ClientError) as caught:
        asyncio.run(connection.synthesize("测试"))
    assert caught.value.provider == "doubao"
    assert isinstance(caught.value.__cause__, OSError)
    connect.assert_awaited_once()


def test_client_mapping_failure_is_distinct_from_empty_result(monkeypatch):
    from deskbot_server.client.face.fr_http_client import FrHttpClient

    response = Mock()
    response.read.return_value = b"not-json"
    opened = Mock()
    opened.__enter__ = Mock(return_value=response)
    opened.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("urllib.request.urlopen", Mock(return_value=opened))
    with pytest.raises(ClientError) as caught:
        asyncio.run(FrHttpClient("http://face").detect(b"jpeg"))
    assert caught.value.kind == "mapping"
