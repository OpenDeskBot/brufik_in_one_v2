from __future__ import annotations

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(db_path)
        init_database()
        yield db_path


def test_flask_api_requires_session(temp_db):
    from deskbot_server.web.app import create_app

    app = create_app()
    client = app.test_client()

    assert client.get("/health").status_code == 200
    assert client.get("/debug/devices").status_code == 302


def test_flask_api_allows_logged_in_developer(temp_db):
    from tests._auth_compat import create_user, set_user_developer
    from deskbot_server.web.app import create_app

    user = create_user("alice@example.com", "password1234")
    set_user_developer(user.id, is_developer=True)
    app = create_app()
    client = app.test_client()

    login = client.post(
        "/login", data={"email": "alice@example.com", "password": "password1234"}, follow_redirects=False
    )
    assert login.status_code == 302

    resp = client.get("/debug/llm")
    assert resp.status_code == 200


def test_flask_api_denies_debug_for_non_developer(temp_db):
    from tests._auth_compat import create_user
    from deskbot_server.web.app import create_app

    create_user("first@example.com", "password1234")
    create_user("bob@example.com", "password1234")
    app = create_app()
    client = app.test_client()

    login = client.post("/login", data={"email": "bob@example.com", "password": "password1234"}, follow_redirects=False)
    assert login.status_code == 302

    resp = client.get("/debug/llm", follow_redirects=False)
    assert resp.status_code in (302, 307)
    loc = resp.headers.get("Location", "")
    assert loc.startswith("/home") or "/login" in loc or loc.startswith("/app")


def test_register_and_login_flow(temp_db):
    from deskbot_server.web.app import create_app

    app = create_app()
    client = app.test_client()

    r = client.post(
        "/register",
        data={"email": "newbie@example.com", "password": "password1234", "confirm_password": "password1234"},
        follow_redirects=False,
    )
    assert r.status_code == 302

    client.post("/logout")
    r2 = client.post("/login", data={"email": "newbie@example.com", "password": "password1234"}, follow_redirects=False)
    assert r2.status_code == 302


def test_developer_user_management(temp_db):
    from tests._auth_compat import create_user, get_user_by_email, set_user_developer
    from deskbot_server.web.app import create_app

    admin = create_user("admin@example.com", "password1234")
    set_user_developer(admin.id, is_developer=True)
    create_user("member@example.com", "password1234")

    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": "admin@example.com", "password": "password1234"})

    resp = client.get("/debug/users")
    assert resp.status_code == 200
    assert b"member@example.com" in resp.data

    member = get_user_by_email("member@example.com")
    assert member is not None

    api = client.post(f"/api/debug/users/{member.id}/developer", json={"is_developer": True})
    assert api.status_code == 200
    assert api.get_json()["user"]["is_developer"] is True


def test_user_management_api_requires_developer(temp_db):
    """用户管理接口：普通用户不可设开发者；未登录 401。"""
    from tests._auth_compat import create_user, get_user_by_email, set_user_developer
    from deskbot_server.web.app import create_app

    admin = create_user("um-admin@example.com", "password1234")
    set_user_developer(admin.id, is_developer=True)
    member = create_user("um-member@example.com", "password1234")

    app = create_app()
    client = app.test_client()

    # 未登录 → 401
    assert client.post(f"/api/debug/users/{member.id}/developer", json={"is_developer": True}).status_code == 401

    # 普通用户 → 403，且用户权限未被改动
    client.post("/login", data={"email": "um-member@example.com", "password": "password1234"})
    resp = client.post(f"/api/debug/users/{admin.id}/developer", json={"is_developer": False})
    assert resp.status_code == 403
    assert bool(get_user_by_email("um-admin@example.com").is_developer) is True

    # 页面同样受保护：普通用户被弹走
    page = client.get("/dev/users", follow_redirects=False)
    assert page.status_code in (302, 307)

    # 开发者 → 200
    client.post("/logout")
    client.post("/login", data={"email": "um-admin@example.com", "password": "password1234"})
    assert client.get("/dev/users").status_code == 200
    assert client.post(f"/api/debug/users/{member.id}/developer", json={"is_developer": True}).status_code == 200


def test_services_api_requires_developer(temp_db, monkeypatch):
    """独立服务管理：页面与全部 API 仅开发者可访问。"""
    from tests._auth_compat import create_user, set_user_developer
    from deskbot_server.web.app import create_app

    class FakeService:
        def to_dict(self):
            return {"name": "fake-svc", "state": "running"}

    class FakeManager:
        def status_all(self):
            return [FakeService()]

    import deskbot_server.web.blueprints.services_bp as services_bp

    monkeypatch.setattr(services_bp, "_manager", lambda: FakeManager())

    admin = create_user("svc-admin@example.com", "password1234")
    set_user_developer(admin.id, is_developer=True)
    create_user("svc-bob@example.com", "password1234")

    app = create_app()
    client = app.test_client()

    # 未登录 → 401
    assert client.get("/api/services").status_code == 401

    # 普通用户：列表 API 403，页面被弹走
    client.post("/login", data={"email": "svc-bob@example.com", "password": "password1234"})
    assert client.get("/api/services").status_code == 403
    page = client.get("/services", follow_redirects=False)
    assert page.status_code in (302, 307)

    # 开发者：列表 API 与页面均可访问
    client.post("/logout")
    client.post("/login", data={"email": "svc-admin@example.com", "password": "password1234"})
    resp = client.get("/api/services")
    assert resp.status_code == 200
    assert resp.get_json()["services"][0]["name"] == "fake-svc"
    assert client.get("/services").status_code == 200
