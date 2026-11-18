"""Saved expression catalog shared by PB references and HTTP resource discovery."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from deskbot_server.model.face_package import compile_editor_document
from deskbot_server.service.device_data_paths import global_config_dir

CATALOG_PATH = Path(__file__).resolve().parents[1] / "pb/data/face_assets_manifest.json"


@lru_cache(maxsize=2)
def _read_catalog(path: str, mtime_ns: int) -> dict[str, Any]:
    del mtime_ns
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_face_asset_catalog() -> dict[str, Any]:
    return load_face_asset_library()


@lru_cache(maxsize=4)
def _compile_source(source: bytes) -> dict[str, Any]:
    return compile_editor_document(json.loads(source))


def load_face_asset_library() -> dict[str, Any]:
    source = global_config_dir() / "deskbot-face.json"
    if source.exists():
        return _compile_source(source.read_bytes())
    # A packaged installation also works before an editable source has been seeded.
    path = CATALOG_PATH.with_name("face_package.json")
    return _read_catalog(str(path), path.stat().st_mtime_ns)["library"]


def find_face_asset(name: str) -> tuple[dict[str, Any], str] | None:
    doc = load_face_asset_catalog()
    want = str(name).strip().lower()
    for entry in doc["assets"]:
        if want in [entry["id"], *entry["aliases"]]:
            return entry, doc["version"]
    return None
