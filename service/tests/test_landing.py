from __future__ import annotations

import json
import re
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


def test_landing_page(temp_db):
    from deskbot_server.web.app import create_app

    html = create_app().test_client().get("/").text
    assert 'id="heroFace"' in html
    assert "face_preview_2c.js" in html
    assert "./start.sh" in html
    assert "0.0.0.0:9000" in html
    assert "deskbot</span> start" not in html
    m = re.search(r"const scenes = (\[.*?\]);", html, re.S)
    assert m, "scenes not embedded"
    scenes = json.loads(m.group(1))
    idle = next(s for s in scenes if s["name"] == "idle")
    assert idle["title"] == "日常待机"
    assert idle["frames"][0]["elements"]["eye_l"][0]["rh"] == 11
    assert len(idle["frames"]) >= 2
