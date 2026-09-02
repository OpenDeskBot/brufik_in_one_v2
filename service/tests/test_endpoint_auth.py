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


def test_dev_menu_apis_require_developer(temp_db):
    """开发者选项（表情设计/剧情设计）的全部写 API 仅开发者可调用。

    读接口例外：GET /api/face_expr_scenes、GET /api/emotion_expr_map 供消费端
    首页 3D 表情预览使用，普通用户应可达（无设备时 400 校验错误而非 403）。
    """
    from tests._auth_compat import create_user
    from deskbot_server.web.app import create_app

    # 第一个注册用户自动为开发者；member 为普通用户
    create_user("dm-admin@example.com", "password1234")
    create_user("dm-member@example.com", "password1234")
    app = create_app()

    def login(email: str):
        client = app.test_client()
        client.post("/login", data={"email": email, "password": "password1234"})
        return client

    member = login("dm-member@example.com")
    dev = login("dm-admin@example.com")

    # ── 普通用户：开发者菜单全部写 API → 403 ──
    blocked = [
        ("GET", "/api/quest/playbooks"),
        ("POST", "/api/quest/playbooks"),
        ("DELETE", "/api/quest/playbooks/whatever"),
        ("POST", "/api/quest/playbooks/whatever/tasks"),
        ("POST", "/api/quest/playbooks/whatever/simulate/t1"),
        ("POST", "/api/face_expr_scenes"),
        ("POST", "/api/emotion_expr_map"),
        ("GET", "/api/face_mouth_by_phoneme"),
        ("POST", "/api/face_mouth_by_phoneme"),
        ("POST", "/api/scene_playbook/export_plan"),
        ("POST", "/api/face_design/generate"),
        ("POST", "/api/face_design/generate-from-image"),
    ]
    for method, path in blocked:
        if method == "GET":
            resp = member.get(path)
        elif method == "DELETE":
            resp = member.delete(path)
        else:
            resp = member.post(path, json={})
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}（应 403）"

    # ── 普通用户：消费端读接口仍可达（无设备 → 400 校验错误，而不是 403）──
    assert member.get("/api/face_expr_scenes").status_code == 400
    assert member.get("/api/emotion_expr_map").status_code == 400

    # ── 开发者：可访问页面与列表 API ──
    assert dev.get("/expr").status_code == 200
    assert dev.get("/quest").status_code == 200
    resp = dev.get("/api/quest/playbooks")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


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
