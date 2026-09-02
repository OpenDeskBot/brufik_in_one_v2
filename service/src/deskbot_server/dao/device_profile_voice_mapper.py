"""声纹档案表 SQL Mapper — MyBatis 注解风格。"""

from __future__ import annotations

from deskbot_server.db.models import DeviceProfileVoice
from deskbot_server.db.sql_decorators import execute, select, select_one, sql_insert

# ────────────────────────── 查询 ──────────────────────────


@select("SELECT * FROM device_profile_voice WHERE device_id = :device_id ORDER BY id", model=DeviceProfileVoice)
def list_by_device(device_id: str) -> list[DeviceProfileVoice]:
    """列出设备所有声纹档案。"""


@select_one("SELECT * FROM device_profile_voice WHERE id = :id", model=DeviceProfileVoice)
def get_by_id(id: int) -> DeviceProfileVoice | None:
    """根据主键查找声纹档案。"""


@select("SELECT * FROM device_profile_voice WHERE device_id = :device_id AND name = :name", model=DeviceProfileVoice)
def list_by_device_and_name(device_id: str, name: str) -> list[DeviceProfileVoice]:
    """按设备 + 姓名查找档案（用于同名合并判断）。"""


# ────────────────────────── 写操作 ──────────────────────────


@sql_insert(
    """
    INSERT INTO device_profile_voice (device_id, name, descriptor, descriptor_kind, created_at, updated_at)
    VALUES (:device_id, :name, :descriptor, :descriptor_kind, datetime('now'), datetime('now'))
    """
)
def insert(device_id: str, name: str, descriptor: str, descriptor_kind: str) -> int:
    """插入新声纹档案，返回新行自增 id。"""


@execute(
    """
    UPDATE device_profile_voice
    SET name = :name, descriptor = :descriptor, descriptor_kind = :descriptor_kind, updated_at = datetime('now')
    WHERE id = :id
    """
)
def update(id: int, name: str, descriptor: str, descriptor_kind: str) -> int:
    """更新声纹档案（姓名 + 向量）。"""


@execute("UPDATE device_profile_voice SET name = :name, updated_at = datetime('now') WHERE id = :id")
def update_name(id: int, name: str) -> int:
    """仅更新姓名。"""


@execute("DELETE FROM device_profile_voice WHERE id = :id")
def delete_by_id(id: int) -> int:
    """按主键删除声纹档案。"""


@execute("DELETE FROM device_profile_voice WHERE device_id = :device_id")
def delete_by_device(device_id: str) -> int:
    """删除设备所有声纹档案。"""
