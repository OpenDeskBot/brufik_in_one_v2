"""会话及消息的直接 SQL 映射；会话规则位于 service.session_service。"""

from __future__ import annotations

from deskbot_server.dao.decorators import delete as db_delete
from deskbot_server.dao.decorators import insert as db_insert
from deskbot_server.dao.decorators import select, select_one
from deskbot_server.dao.decorators import update as db_update
from deskbot_server.db.models import DeviceSession, DeviceSessionMessage


@select(
    "SELECT * FROM device_session WHERE device_id = :device_id ORDER BY updated_at DESC LIMIT :limit",
    model=DeviceSession,
)
def list_sessions(device_id: str, limit: int = 10) -> list[DeviceSession]:
    """列出设备最近 Session。"""


@select_one("SELECT * FROM device_session WHERE id = :id", model=DeviceSession)
def get_session(id: str) -> DeviceSession | None:
    """根据主键查找 Session。"""


@select_one(
    "SELECT * FROM device_session WHERE device_id = :device_id ORDER BY updated_at DESC LIMIT 1", model=DeviceSession
)
def get_latest_session(device_id: str) -> DeviceSession | None:
    """获取设备最近一个 Session。"""


@db_insert(
    """
    INSERT INTO device_session (id, device_id, title, created_at, updated_at)
    VALUES (:id, :device_id, :title, COALESCE(:created_at, datetime('now')), COALESCE(:updated_at, datetime('now')))
    """,
    id_param="id",
)
def insert_session(
    id: str, device_id: str, title: str, created_at: str | None = None, updated_at: str | None = None
) -> str:
    """创建新 Session。"""


@db_update(
    "UPDATE device_session SET title = :title, updated_at = COALESCE(:updated_at, datetime('now')) WHERE id = :id"
)
def update_session_title(id: str, title: str, updated_at: str | None = None) -> int:
    """更新 Session 标题。"""


@db_update("UPDATE device_session SET updated_at = COALESCE(:updated_at, datetime('now')) WHERE id = :id")
def touch_session(id: str, updated_at: str | None = None) -> int:
    """刷新 Session 活跃时间。"""


@db_delete("DELETE FROM device_session WHERE id = :id")
def delete_session(id: str) -> int:
    """删除 Session（级联消息由 DB 外键或应用层处理）。"""


@db_delete(
    """
    DELETE FROM device_session_message
    WHERE session_id IN (SELECT id FROM device_session WHERE device_id = :device_id)
    """
)
def delete_messages_by_device(device_id: str) -> int:
    """删除设备全部 Session 的消息。**必须先于** ``delete_sessions_by_device`` 调用。

    ``device_session_message`` 没有 ``device_id`` 列，靠 ``session_id`` 关联；且
    ``PRAGMA foreign_keys`` 未开启，顺序反了会静默留下永远查不到的孤儿消息。
    """


@db_delete("DELETE FROM device_session WHERE device_id = :device_id")
def delete_sessions_by_device(device_id: str) -> int:
    """删除设备全部 Session 头（设备清除数据用）。"""


@select("SELECT * FROM device_session_message WHERE session_id = :session_id ORDER BY id", model=DeviceSessionMessage)
def list_messages(session_id: str) -> list[DeviceSessionMessage]:
    """列出 Session 所有消息。"""


@select_one("SELECT COUNT(*) FROM device_session_message WHERE session_id = :session_id")
def count_messages(session_id: str) -> int:
    """统计 Session 消息数。"""


@db_insert(
    """
    INSERT INTO device_session_message (session_id, role, content, created_at)
    VALUES (:session_id, :role, :content, COALESCE(:created_at, datetime('now')))
    """
)
def insert_message(session_id: str, role: str, content: str, created_at: str | None = None) -> int:
    """插入一条消息（created_at 为 ISO 时间字符串，缺省用数据库当前时间）。"""


@db_delete("DELETE FROM device_session_message WHERE session_id = :session_id")
def delete_messages(session_id: str) -> int:
    """删除 Session 所有消息。"""
