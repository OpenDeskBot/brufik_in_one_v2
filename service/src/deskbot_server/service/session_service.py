"""会话生命周期、历史窗口与消息保存；事务由本层组织。"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from deskbot_server.dao.device_session_mapper import (
    count_messages,
    get_latest_session,
    get_session,
    insert_message,
    insert_session,
    list_messages,
    list_sessions,
    touch_session,
    update_session_title,
)
from deskbot_server.db.models import DeviceSession, DeviceSessionMessage
from deskbot_server.db.transaction import transaction as db_transaction

SESSION_IDLE_SECONDS = 10 * 60
_MAX_HISTORY_TURNS = 30
_MAX_TITLE_LEN = 48


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
            dt = dt.replace(tzinfo=UTC)
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
    return {"role": msg.role, "message": msg.content, "ts": _dt_to_ts(msg.created_at)}


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


def create_session(
    device_id: str, *, title: str | None = None, now: float | None = None, transaction=None
) -> dict[str, Any]:
    from deskbot_server.db.models import _new_id

    dev = str(device_id).strip()
    sid = _new_id()
    ts_str = datetime.fromtimestamp(now, tz=UTC).isoformat() if now is not None else None
    with db_transaction(transaction) as tx:
        insert_session(sid, dev, _truncate_title(title or ""), created_at=ts_str, updated_at=ts_str, transaction=tx)
        session = get_session(sid, transaction=tx)
        if session is None:
            raise RuntimeError("创建 session 失败")
        return _session_to_dict(session, messages=[])


def load_session(device_id: str, session_id: str) -> dict[str, Any] | None:
    dev = str(device_id or "").strip()
    if not dev:
        return None
    session = get_session(str(session_id or "").strip())
    if session is None or str(session.device_id or "").strip() != dev:
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
    session = load_session(device_id, session_id)
    if session is None:
        return []
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
    owned = get_session(sid)
    if owned is None or str(owned.device_id or "").strip() != str(device_id or "").strip():
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

    with db_transaction() as tx:
        session = get_session(sid, transaction=tx)
        if session is None:
            created = create_session(device_id, title=_truncate_title(user_text), now=now, transaction=tx)
            sid = created["session_id"]
        elif str(session.device_id or "").strip() != str(device_id or "").strip():
            raise ValueError("session not found")

        ts_str = datetime.fromtimestamp(now, tz=UTC).isoformat() if now is not None else None
        user_msg = str(user_text or "").strip()
        assistant_msg = str(assistant_text or "").strip()
        if user_msg:
            insert_message(sid, "user", user_msg, created_at=ts_str, transaction=tx)
        if assistant_msg:
            insert_message(sid, "assistant", assistant_msg, created_at=ts_str, transaction=tx)
        touch_session(sid, updated_at=ts_str, transaction=tx)

        msg_count = count_messages(sid, transaction=tx)
        if msg_count <= 2 and user_msg:
            update_session_title(sid, _truncate_title(user_msg), updated_at=ts_str, transaction=tx)

        refreshed = get_session(sid, transaction=tx)
        messages = list_messages(sid, transaction=tx)
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
