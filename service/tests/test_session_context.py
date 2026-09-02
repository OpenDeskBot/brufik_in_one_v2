"""会话上下文窗口测试：5 分钟间隔截断（mapper）+ token 预算裁剪（chat_flow）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(db_path)
        init_database()
        yield db_path


def _seed_turns(device_id: str, *, t0: float, turns: list[tuple[float, str, str]]):
    """turns: (now_ts, user_text, assistant_text)，按序落库到同一 session。"""
    from deskbot_server.dao.device_session_mapper import append_turn, ensure_active_session

    first_ts = turns[0][0]
    session_id = str(ensure_active_session(device_id, user_text=turns[0][1], now=first_ts)["session_id"])
    for now, user_text, assistant_text in turns:
        append_turn(device_id, session_id, user_text, assistant_text, now=now)
    return session_id


def test_context_window_cuts_at_gap_over_limit(temp_db):
    from deskbot_server.dao.device_session_mapper import session_context_window

    t0 = 1_700_000_000.0
    sid = _seed_turns(
        "dev_ctx_1",
        t0=t0,
        turns=[
            (t0, "第一轮", "嗯"),
            (t0 + 60, "第二轮", "好"),
            # 与上一轮间隔 > 300s → 更旧段全部丢弃
            (t0 + 60 + 400, "第三轮", "来了"),
        ],
    )
    rows = session_context_window("dev_ctx_1", sid, max_gap_seconds=300)
    assert len(rows) == 2  # 只保留最新一轮（user+assistant）
    assert [r["content"] for r in rows] == ["第三轮", "来了"]
    assert all(r["ts"] > t0 + 60 for r in rows)


def test_context_window_keeps_continuous_turns(temp_db):
    from deskbot_server.dao.device_session_mapper import session_context_window

    t0 = 1_700_000_000.0
    sid = _seed_turns(
        "dev_ctx_2",
        t0=t0,
        turns=[
            (t0, "你好", "你好呀"),
            (t0 + 60, "今天天气", "还不错"),
            (t0 + 120, "那出门吗", "可以呀"),
        ],
    )
    rows = session_context_window("dev_ctx_2", sid, max_gap_seconds=300)
    assert [r["content"] for r in rows] == ["你好", "你好呀", "今天天气", "还不错", "那出门吗", "可以呀"]
    assert all(r["role"] in ("user", "assistant") for r in rows)


def test_build_history_messages_token_budget_keeps_newest():
    from deskbot_server.service.application.chat_flow import build_history_messages

    old_big = {"role": "user", "content": "旧" * 100, "ts": 0}
    mid = {"role": "assistant", "content": "中" * 100, "ts": 0}
    newest = {"role": "user", "content": "新", "ts": 0}

    # 预算充裕 → 全保留（时间正序）
    out = build_history_messages([old_big, mid, newest], token_budget=10_000)
    assert [m["content"] for m in out] == ["旧" * 100, "中" * 100, "新"]

    # 预算吃紧 → 保近弃远：只保留最新一条
    out2 = build_history_messages([old_big, mid, newest], token_budget=10)
    assert [m["content"] for m in out2] == ["新"]
    assert out2[0]["role"] == "user"

    # 空输入
    assert build_history_messages([]) == []
