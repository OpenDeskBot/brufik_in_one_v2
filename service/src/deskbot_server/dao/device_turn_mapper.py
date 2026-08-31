"""调试模式对话轮次（device_turn）— SQL Mapper。

一行 = 一轮完整对话（用户气泡 + 机器人气泡），由 ``turn_recorder`` 写入。
音频/图像只存相对 ``data/device/{device_id}/`` 的路径。

公开 API：insert_turn / count_turns / list_turns / get_turn / delete_session_turns
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from deskbot_server.db.models import DeviceTurn
from deskbot_server.db.sql_decorators import execute, select, select_one

_TURN_FIELDS = (
    "id", "session_id", "device_id", "seq", "source",
    "user_text", "user_audio", "user_audio_ms", "asr_ms", "asr_model",
    "fr_image", "fr_ms", "fr_result", "fr_model",
    "vpr_ms", "vpr_result", "vpr_model",
    "bot_text", "bot_audio", "bot_audio_ms", "llm_ms", "llm_model",
    "tools", "tts_ms", "tts_model", "system_prompt", "status", "error",
)


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


def _turn_to_dict(turn: DeviceTurn) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in _TURN_FIELDS:
        out[field] = getattr(turn, field)
    out["created_at"] = _dt_to_ts(turn.created_at)
    for key in ("tools", "fr_result", "vpr_result"):
        raw = out.get(key)
        if raw:
            try:
                out[key] = json.loads(raw)
            except (TypeError, ValueError):
                pass
    return out


@execute(
    """
    INSERT INTO device_turn (
        id, session_id, device_id, seq, source,
        user_text, user_audio, user_audio_ms, asr_ms, asr_model,
        fr_image, fr_ms, fr_result, fr_model,
        vpr_ms, vpr_result, vpr_model,
        bot_text, bot_audio, bot_audio_ms, llm_ms, llm_model,
        tools, tts_ms, tts_model, system_prompt, status, error, created_at
    ) VALUES (
        :id, :session_id, :device_id, :seq, :source,
        :user_text, :user_audio, :user_audio_ms, :asr_ms, :asr_model,
        :fr_image, :fr_ms, :fr_result, :fr_model,
        :vpr_ms, :vpr_result, :vpr_model,
        :bot_text, :bot_audio, :bot_audio_ms, :llm_ms, :llm_model,
        :tools, :tts_ms, :tts_model, :system_prompt, :status, :error,
        datetime('now')
    )
    """
)
def insert_turn(
    id: str,
    session_id: str,
    device_id: str,
    seq: int,
    source: str,
    user_text: str | None = None,
    user_audio: str | None = None,
    user_audio_ms: int | None = None,
    asr_ms: int | None = None,
    asr_model: str | None = None,
    fr_image: str | None = None,
    fr_ms: int | None = None,
    fr_result: str | None = None,
    fr_model: str | None = None,
    vpr_ms: int | None = None,
    vpr_result: str | None = None,
    vpr_model: str | None = None,
    bot_text: str | None = None,
    bot_audio: str | None = None,
    bot_audio_ms: int | None = None,
    llm_ms: int | None = None,
    llm_model: str | None = None,
    tools: str | None = None,
    tts_ms: int | None = None,
    tts_model: str | None = None,
    system_prompt: str | None = None,
    status: str = "ok",
    error: str | None = None,
) -> int:
    """插入一条对话轮次记录。"""


@select_one("SELECT COUNT(*) FROM device_turn WHERE session_id = :session_id")
def count_turns(session_id: str) -> int:
    """统计 Session 轮次数。"""


@select("SELECT * FROM device_turn WHERE session_id = :session_id ORDER BY seq ASC LIMIT :limit", model=DeviceTurn)
def list_turns(session_id: str, limit: int = 200) -> list[DeviceTurn]:
    """列出 Session 全部轮次（按 seq 升序）。"""


@select_one("SELECT * FROM device_turn WHERE id = :id", model=DeviceTurn)
def get_turn(id: str) -> DeviceTurn | None:
    """按主键查轮次。"""


@execute("DELETE FROM device_turn WHERE session_id = :session_id")
def delete_session_turns(session_id: str) -> int:
    """删除 Session 的轮次记录（会话删除级联）。"""


def list_turns_as_dicts(session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    return [_turn_to_dict(t) for t in list_turns(session_id, limit=limit)]
