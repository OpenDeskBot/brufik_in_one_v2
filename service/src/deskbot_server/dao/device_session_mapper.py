"""设备对话 Session — SQL Mapper + 业务逻辑。

公开 API：create_session / load_session / ensure_active_session /
         session_history_for_llm / append_turn / list_recent_sessions /
         get_current_session / execute_session_tool
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from deskbot_server.db.models import DeviceSession, DeviceSessionMessage
from deskbot_server.db.sql_decorators import execute, select, select_one

SESSION_IDLE_SECONDS = 10 * 60
_MAX_HISTORY_TURNS = 30
_MAX_TITLE_LEN = 48


# ─────────────────────── 内部工具 ───────────────────────


def _now_ts() -> float:
    return time.time()


def _truncate_title(text: str) -> str:
    raw = " ".join(str(text or "").split())
    if not raw:
        return "新对话"
    if len(raw) <= _MAX_TITLE_LEN:
        return raw
    return raw[: _MAX_TITLE_LEN - 1] + "…"


def _dt_to_ts(dt: Any) -> float:
    if dt is None:
        return 0.0
    if isinstance(dt, (int, float)):
        return float(dt)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    try:
        return float(dt)
    except (TypeError, ValueError):
        return 0.0


def _session_to_dict(session: DeviceSession, messages: list[DeviceSessionMessage] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "session_id": session.id,
        "device_id": session.device_id,
        "title": session.title or "新对话",
        "created_at": _dt_to_ts(session.created_at),
        "updated_at": _dt_to_ts(session.updated_at),
    }
    if messages is not None:
        out["messages"] = [_msg_to_dict(m) for m in messages]
    return out


def _msg_to_dict(msg: DeviceSessionMessage) -> dict[str, Any]:
    return {
        "role": msg.role,
        "message": msg.content,
        "ts": _dt_to_ts(msg.created_at),
    }


def _session_summary_dict(session: DeviceSession, message_count: int) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "title": session.title or "新对话",
        "created_at": _dt_to_ts(session.created_at),
        "updated_at": _dt_to_ts(session.updated_at),
        "message_count": message_count,
    }


def _session_tool_summary(session: dict[str, Any], *, include_messages: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "session_id": session.get("session_id"),
        "title": session.get("title"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "message_count": len(session.get("messages") or []),
    }
    if include_messages:
        out["messages"] = list(session.get("messages") or [])
    return out


# ─────────────────────── SQL Mapper — Session 查询 ───────────────────────


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
    "SELECT * FROM device_session WHERE device_id = :device_id ORDER BY updated_at DESC LIMIT 1",
    model=DeviceSession,
)
def get_latest_session(device_id: str) -> DeviceSession | None:
    """获取设备最近一个 Session。"""


# ─────────────────────── Session 写操作 ───────────────────────


@execute(
    """
    INSERT INTO device_session (id, device_id, title, created_at, updated_at)
    VALUES (:id, :device_id, :title, COALESCE(:created_at, datetime('now')), COALESCE(:updated_at, datetime('now')))
    """
)
def insert_session(id: str, device_id: str, title: str, created_at: str | None = None, updated_at: str | None = None) -> int:
    """创建新 Session。"""


@execute("UPDATE device_session SET title = :title, updated_at = COALESCE(:updated_at, datetime('now')) WHERE id = :id")
def update_session_title(id: str, title: str, updated_at: str | None = None) -> int:
    """更新 Session 标题。"""


@execute("UPDATE device_session SET updated_at = COALESCE(:updated_at, datetime('now')) WHERE id = :id")
def touch_session(id: str, updated_at: str | None = None) -> int:
    """刷新 Session 活跃时间。"""


@execute("DELETE FROM device_session WHERE id = :id")
def delete_session(id: str) -> int:
    """删除 Session（级联消息由 DB 外键或应用层处理）。"""


# ─────────────────────── Message 查询 ───────────────────────


@select(
    "SELECT * FROM device_session_message WHERE session_id = :session_id ORDER BY id",
    model=DeviceSessionMessage,
)
def list_messages(session_id: str) -> list[DeviceSessionMessage]:
    """列出 Session 所有消息。"""


@select_one("SELECT COUNT(*) FROM device_session_message WHERE session_id = :session_id")
def count_messages(session_id: str) -> int:
    """统计 Session 消息数。"""


# ─────────────────────── Message 写操作 ───────────────────────


@execute(
    """
    INSERT INTO device_session_message (session_id, role, content, created_at)
    VALUES (:session_id, :role, :content, COALESCE(:created_at, datetime('now')))
    """
)
def insert_message(session_id: str, role: str, content: str, created_at: str | None = None) -> int:
    """插入一条消息（created_at 为 ISO 时间字符串，缺省用数据库当前时间）。"""


@execute("DELETE FROM device_session_message WHERE session_id = :session_id")
def delete_messages(session_id: str) -> int:
    """删除 Session 所有消息。"""


# ─────────────────────── 公开业务 API ───────────────────────


def create_session(device_id: str, *, title: str | None = None, now: float | None = None) -> dict[str, Any]:
    from deskbot_server.db.models import _new_id

    dev = str(device_id).strip()
    sid = _new_id()
    ts_str = datetime.fromtimestamp(now, tz=timezone.utc).isoformat() if now is not None else None
    insert_session(sid, dev, _truncate_title(title or ""), created_at=ts_str, updated_at=ts_str)
    session = get_session(sid)
    if session is None:
        raise RuntimeError(f"创建 session 失败: {sid}")
    return _session_to_dict(session, messages=[])


def load_session(device_id: str, session_id: str) -> dict[str, Any] | None:
    session = get_session(str(session_id or "").strip())
    if session is None:
        return None
    messages = list_messages(session.id)
    return _session_to_dict(session, messages=messages)


def ensure_active_session(device_id: str, *, user_text: str | None = None, now: float | None = None) -> dict[str, Any]:
    """返回当前可用 session；距上次对话超过 10 分钟则新建。"""
    dev = str(device_id or "").strip()
    if not dev:
        raise ValueError("device_id required")
    ts = float(now if now is not None else _now_ts())

    latest = get_latest_session(dev)
    if latest is not None:
        updated_ts = _dt_to_ts(latest.updated_at)
        if updated_ts > 0 and (ts - updated_ts) <= SESSION_IDLE_SECONDS:
            messages = list_messages(latest.id)
            return _session_to_dict(latest, messages=messages)

    title = _truncate_title(user_text or "") if user_text else "新对话"
    return create_session(dev, title=title, now=ts)


def session_history_for_llm(
    device_id: str, session_id: str, *, max_turns: int = _MAX_HISTORY_TURNS
) -> list[dict[str, str]]:
    """将已存 session 消息转为 LLM ``history_messages``（role/content）。"""
    messages = list_messages(str(session_id or "").strip())
    cap = max(0, int(max_turns)) * 2
    if cap > 0:
        messages = messages[-cap:]
    out: list[dict[str, str]] = []
    for msg in messages:
        role = msg.role
        content = (msg.content or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


def session_context_window(
    device_id: str, session_id: str, *, max_gap_seconds: float, max_history_turns: int = _MAX_HISTORY_TURNS
) -> list[dict[str, Any]]:
    """自最近一条消息向上追溯，返回时间上连续的上下文段。

    - 相邻两条消息时间间隔超过 ``max_gap_seconds`` 即截断（丢弃更旧的段）；
    - 时间未知（ts=0）的消息不参与断点判断；
    - 最多取 ``max_history_turns`` 轮。

    返回按时间正序的 ``[{role, content, ts}]``；token 预算由调用方另行裁剪。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return []
    messages = list_messages(sid)
    cap = max(0, int(max_history_turns)) * 2
    if cap > 0 and len(messages) > cap:
        messages = messages[-cap:]
    rows: list[dict[str, Any]] = []
    for m in messages:
        content = (m.content or "").strip()
        if m.role not in ("user", "assistant") or not content:
            continue
        rows.append({"role": m.role, "content": content, "ts": _dt_to_ts(m.created_at)})
    if not rows:
        return []
    keep_from = 0
    for i in range(len(rows) - 1, 0, -1):
        t_new = rows[i]["ts"]
        t_old = rows[i - 1]["ts"]
        if t_new > 0 and t_old > 0 and (t_new - t_old) > float(max_gap_seconds):
            keep_from = i
            break
    return rows[keep_from:]


def append_turn(
    device_id: str, session_id: str, user_text: str, assistant_text: str, *, now: float | None = None
) -> dict[str, Any]:
    """追加一轮 user/assistant 消息并更新 session 时间戳。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id required")

    session = get_session(sid)
    if session is None:
        created = create_session(device_id, title=_truncate_title(user_text), now=now)
        sid = created["session_id"]

    ts_str = datetime.fromtimestamp(now, tz=timezone.utc).isoformat() if now is not None else None
    user_msg = str(user_text or "").strip()
    assistant_msg = str(assistant_text or "").strip()
    if user_msg:
        insert_message(sid, "user", user_msg, created_at=ts_str)
    if assistant_msg:
        insert_message(sid, "assistant", assistant_msg, created_at=ts_str)
    touch_session(sid, updated_at=ts_str)

    msg_count = count_messages(sid)
    if msg_count <= 2 and user_msg:
        update_session_title(sid, _truncate_title(user_msg), updated_at=ts_str)

    refreshed = get_session(sid)
    messages = list_messages(sid)
    if refreshed is None:
        raise RuntimeError(f"session 消失: {sid}")
    return _session_to_dict(refreshed, messages=messages)


def list_recent_sessions(device_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    sessions = list_sessions(str(device_id or "").strip(), limit=min(int(limit), 50))
    out: list[dict[str, Any]] = []
    for s in sessions:
        cnt = count_messages(s.id)
        out.append(_session_summary_dict(s, cnt))
    return out


def get_current_session(device_id: str) -> dict[str, Any] | None:
    latest = get_latest_session(str(device_id or "").strip())
    if latest is None:
        return None
    messages = list_messages(latest.id)
    return _session_to_dict(latest, messages=messages)


def execute_session_tool(raw: dict[str, Any], *, device_id: str) -> dict[str, Any]:
    """LLM ``session`` 工具：查询当前与最近 session。"""
    dev = str(device_id or "").strip()
    if not dev:
        raise ValueError("session 需要 device_id")
    action = str(raw.get("action") or raw.get("op") or "current").strip().lower()
    if action in ("current", "now", "active"):
        session = get_current_session(dev)
        if session is None:
            return {"tool": "session", "action": "current", "ok": True, "session": None}
        return {
            "tool": "session",
            "action": "current",
            "ok": True,
            "session": _session_tool_summary(session, include_messages=True),
        }
    if action in ("list", "ls", "recent"):
        limit = raw.get("limit") or raw.get("max") or 10
        sessions = list_recent_sessions(dev, limit=int(limit))
        return {"tool": "session", "action": "list", "ok": True, "sessions": sessions, "count": len(sessions)}
    sid = str(raw.get("session_id") or raw.get("id") or "").strip()
    if action in ("get", "read", "query"):
        if not sid:
            session = get_current_session(dev)
            if session is None:
                return {"tool": "session", "action": "get", "ok": True, "session": None}
            return {
                "tool": "session",
                "action": "get",
                "ok": True,
                "session": _session_tool_summary(session, include_messages=True),
            }
        session = load_session(dev, sid)
        if session is None:
            raise ValueError(f"未找到 session id={sid}")
        return {
            "tool": "session",
            "action": "get",
            "ok": True,
            "session": _session_tool_summary(session, include_messages=True),
        }
    raise ValueError(f"未知 session action: {action}")
