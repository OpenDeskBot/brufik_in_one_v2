"""设备记忆查询、归属判断和修改编排。"""

from __future__ import annotations

import uuid
from typing import Any

from deskbot_server.dao.device_memory_mapper import (
    delete_by_id,
    get_by_device_and_title,
    get_by_id,
    insert,
    list_all,
    list_by_device,
    update_text,
)
from deskbot_server.db.models import DeviceMemory
from deskbot_server.db.transaction import transaction

_MAX_PROMPT_ENTRIES = 30


def _row_to_dict(row: DeviceMemory) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "device_id": row.device_id,
        "title": row.title,
        "parent": row.parent,
        "text": row.text,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_memory_for_device(device_id: str | None = None, *, limit: int = _MAX_PROMPT_ENTRIES) -> list[dict[str, Any]]:
    """列出设备记忆，返回 dict 列表（兼容旧 memory_store.list_memory_for_device）。"""
    dev = str(device_id or "").strip()
    rows = list_by_device(dev) if dev else list_all()
    return [_row_to_dict(r) for r in rows[:limit]]


def add_memory(text: str, *, device_id: str | None = None) -> dict[str, Any]:
    """添加一条记忆，返回 dict（兼容旧 memory_store.add_memory）。"""
    dev = str(device_id or "").strip()
    title = uuid.uuid4().hex[:12]
    with transaction() as tx:
        insert(dev, title, "", str(text or "").strip(), transaction=tx)
        row = get_by_device_and_title(dev, title, transaction=tx)
        if row is None:
            raise RuntimeError("创建记忆失败")
        return _row_to_dict(row)


def get_memory(entry_id: str, *, device_id: str | None = None) -> dict[str, Any] | None:
    """获取单条记忆（兼容旧 memory_store.get_memory）。"""
    try:
        row = get_by_id(int(entry_id))
    except (ValueError, TypeError):
        return None
    if row is None:
        return None
    dev = str(device_id or "").strip()
    if dev and row.device_id != dev:
        return None
    return _row_to_dict(row)


def update_memory(entry_id: str, text: str, *, device_id: str | None = None) -> dict[str, Any] | None:
    """更新记忆内容（兼容旧 memory_store.update_memory）。"""
    try:
        row_id = int(entry_id)
    except (ValueError, TypeError):
        return None
    row = get_by_id(row_id)
    if row is None:
        return None
    dev = str(device_id or "").strip()
    if dev and row.device_id != dev:
        return None
    update_text(row_id, str(text or "").strip())
    row = get_by_id(row_id)
    return _row_to_dict(row) if row else None


def delete_memory(entry_id: str, *, device_id: str | None = None) -> bool:
    """删除记忆（兼容旧 memory_store.delete_memory）。"""
    try:
        row_id = int(entry_id)
    except (ValueError, TypeError):
        return False
    row = get_by_id(row_id)
    if row is None:
        return False
    dev = str(device_id or "").strip()
    if dev and row.device_id != dev:
        return False
    delete_by_id(row_id)
    return True
