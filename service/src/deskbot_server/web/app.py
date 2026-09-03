from __future__ import annotations

import os

from fastapi import FastAPI

from deskbot_server.controller.app import create_fastapi_app


def web_debug_enabled() -> bool:
    raw = (os.environ.get("DESKBOT_WEB_DEBUG") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def create_app() -> FastAPI:
    """仅挂载 Web 控制台（测试用；运行时由主服务 create_fastapi_app 一并挂载）。"""
    app = create_fastapi_app(None, web_only=True)

    def test_client():
        from tests.web_test_client import WebTestClient

        return WebTestClient(app)

    app.test_client = test_client  # type: ignore[attr-defined]
    return app


app = create_app()
