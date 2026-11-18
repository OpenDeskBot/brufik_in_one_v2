"""One-way migration of old SQLite/JSON facts into unattributed device files."""

from __future__ import annotations

import json

from deskbot_server.dao import device_memory_mapper as legacy
from deskbot_server.dao import memory_file_store as store
from deskbot_server.service import device_data_paths
from deskbot_server.service import memory_service as memory


def migrate_legacy_memories() -> int:
    by_device = {}
    for row in legacy.list_legacy_rows():
        by_device.setdefault(row.device_id, []).append(("sqlite:" + str(row.id), row.text))
    json_paths = {}
    for path in device_data_paths.DATA_DIR.glob("*/user_memory.json"):
        if path.parent.name in {"global", "device"} or path.is_symlink():
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        entries = value.get("entries", []) if isinstance(value, dict) else value
        if not isinstance(entries, list):
            raise ValueError(f"旧记忆格式无效：{path.parent.name}")
        json_paths[path.parent.name] = path
        for index, entry in enumerate(entries):
            text = str(entry.get("text") or entry.get("value") or "").strip() if isinstance(entry, dict) else ""
            if text:
                by_device.setdefault(path.parent.name, []).append(("json:" + str(index), text))
    imported = 0
    for device_id, entries in by_device.items():
        from deskbot_server.service.device_identity_service import ensure_device_identity

        ensure_device_identity(device_id)
        with store.transaction(device_id) as files:
            state = memory._state(files)
            done = state["legacy_imported"]
        if not done:
            for key, text in entries:
                # Keep source text intact, including legacy entries longer than
                # the new single-fact limit, by exporting consecutive chunks.
                for start in range(0, len(text), 2000):
                    memory.add_memory(text[start:start + 2000], device_id=device_id,
                                      request_key=f"legacy:{key}:{start}", person_name="未归属")
                    imported += 1
            with store.transaction(device_id) as files:
                state = memory._state(files)
                state["legacy_imported"] = True
                store.save_state(files, state)
        # File commit and idempotency precede removing old sources. Interrupted
        # migration can repeat without duplicates; normal runtime never dual-writes.
        legacy.delete_by_device(device_id)
        if path := json_paths.get(device_id):
            path.unlink()
    return imported
