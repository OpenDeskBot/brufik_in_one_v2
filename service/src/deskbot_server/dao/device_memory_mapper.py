"""设备记忆的直接 SQL 映射。"""

from __future__ import annotations

from deskbot_server.dao.decorators import delete as db_delete
from deskbot_server.dao.decorators import insert as db_insert
from deskbot_server.dao.decorators import select, select_one
from deskbot_server.dao.decorators import update as db_update
from deskbot_server.db.models import DeviceMemory


@select("SELECT * FROM device_memory WHERE device_id = :device_id ORDER BY created_at DESC", model=DeviceMemory)
def list_by_device(device_id: str) -> list[DeviceMemory]:
    """列出设备所有记忆条目。"""


@select("SELECT * FROM device_memory ORDER BY created_at DESC", model=DeviceMemory)
def list_all() -> list[DeviceMemory]:
    """列出所有记忆条目。"""


@select_one("SELECT * FROM device_memory WHERE id = :id", model=DeviceMemory)
def get_by_id(id: int) -> DeviceMemory | None:
    """根据主键查找记忆。"""


@select_one("SELECT * FROM device_memory WHERE device_id = :device_id AND title = :title", model=DeviceMemory)
def get_by_device_and_title(device_id: str, title: str) -> DeviceMemory | None:
    """按设备 + 标题查找记忆。"""


@db_insert(
    """
    INSERT OR IGNORE INTO device_memory (device_id, title, parent, text, created_at, updated_at)
    VALUES (:device_id, :title, :parent, :text, datetime('now'), datetime('now'))
    """
)
def insert(device_id: str, title: str, parent: str, text: str) -> int:
    """插入新记忆条目（同 device_id+title 已存在时忽略）。"""


@db_update(
    """
    UPDATE device_memory
    SET title = :title, parent = :parent, text = :text, updated_at = datetime('now')
    WHERE id = :id
    """
)
def update(id: int, title: str, parent: str, text: str) -> int:
    """更新记忆（标题 + 目录 + 内容）。"""


@db_update("UPDATE device_memory SET text = :text, updated_at = datetime('now') WHERE id = :id")
def update_text(id: int, text: str) -> int:
    """仅更新记忆内容。"""


@db_update("UPDATE device_memory SET retrieved_at = datetime('now') WHERE id = :id")
def touch_retrieved(id: int) -> int:
    """更新最后检索时间。"""


@db_delete("DELETE FROM device_memory WHERE id = :id")
def delete_by_id(id: int) -> int:
    """按主键删除记忆。"""


@db_delete("DELETE FROM device_memory WHERE device_id = :device_id")
def delete_by_device(device_id: str) -> int:
    """删除设备所有记忆。"""
