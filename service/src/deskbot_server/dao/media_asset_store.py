"""Device-isolated media files and metadata. IDs never contain user paths."""

import json
import re
from pathlib import Path

from deskbot_server.service.device_data_paths import safe_device_data_dir


def directory(device_id: str) -> Path:
    root = safe_device_data_dir(device_id).resolve()
    folder = root / "media"
    if not folder.resolve().is_relative_to(root):
        raise ValueError("媒体目录无效")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def asset_path(device_id, asset_id, extension):
    if not isinstance(asset_id, str) or not re.fullmatch(r"[a-f0-9]{32}", asset_id):
        raise ValueError("媒体 id 无效")
    if extension not in {"json", "wav", "png", "rgb565"}:
        raise ValueError("媒体格式无效")
    folder = directory(device_id)
    path = folder / f"{asset_id}.{extension}"
    if not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError("媒体文件路径无效")
    return path


def get(device_id, asset_id):
    path = asset_path(device_id, asset_id, "json")
    if not path.is_file():
        raise ValueError("该设备的媒体库中没有此资源")
    item = json.loads(path.read_text())
    if item.get("id") != asset_id or item.get("kind") not in {"music", "image"}:
        raise ValueError("媒体资料损坏")
    return item


def list_assets(device_id, kind=None, query=""):
    items = []
    candidates = []
    for path in directory(device_id).glob("*.json"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True):
        try:
            item = get(device_id, path.stem)
        except (ValueError, OSError):
            continue
        if (not kind or item["kind"] == kind) and str(query).lower() in item.get("title", "").lower():
            items.append(item)
    return items[:100]


def save(device_id, item):
    target = asset_path(device_id, item["id"], "json")
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(item, ensure_ascii=False))
    temporary.replace(target)


def delete(device_id, asset_id):
    get(device_id, asset_id)
    for extension in ("json", "wav", "png", "rgb565"):
        asset_path(device_id, asset_id, extension).unlink(missing_ok=True)
