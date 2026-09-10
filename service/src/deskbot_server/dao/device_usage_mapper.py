"""设备每日用量表 SQL Mapper。"""

from __future__ import annotations

from deskbot_server.db.sql_decorators import execute


@execute("DELETE FROM device_usage WHERE device_id = :device_id")
def delete_by_device(device_id: str) -> int:
    """删除设备全部用量统计行（设备清除数据用）。"""
