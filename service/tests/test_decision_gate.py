"""语音轮决策前置门控：invalid/无需响应时跳过 LLM/TTS，动作仍直发 PB。"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

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
        reset_engine()


class _FakeDownlink:
    def __init__(self) -> None:
        self.stages: list[tuple[str, dict]] = []

    async def emit_stage(self, stage, **_kwargs):
        self.stages.append((stage, _kwargs.get("event_fields") or {}))


class _FakeChat:
    """门控跳过时不应调用任何 LLM/TTS；一旦调用直接失败。"""

    settings = SimpleNamespace(pb_random_servo_cfg=lambda: None)
    tts_cfg = {"sample_rate": 16000}

    async def llm_tool_round(self, *_a, **_k):  # pragma: no cover
        raise AssertionError("invalid/no-response turn must not call LLM")

    async def tts_phoneme_segments(self, *_a, **_k):  # pragma: no cover
        raise AssertionError("invalid/no-response turn must not call TTS")


def test_asr_turn_skips_llm_tts_when_decision_says_invalid(temp_db, monkeypatch):
    from deskbot_server.service.application import action_decision
    from deskbot_server.service.application.chat_flow import _run_chat_turn_unarbitrated

    async def fake_decide(device_id, user_text, reply_text=None, **kwargs):
        assert kwargs.get("judge_input") is True
        return {
            "valid": False,
            "respond": False,
            "valid_prob": 0.1,
            "respond_prob": 0.2,
            "moves": [],
            "anims": [],
            "model": "unee",
            "raw": "",
            "elapsed_ms": 12,
            "error": None,
        }

    monkeypatch.setattr(action_decision, "decide_turn_actions", fake_decide)

    downlink = _FakeDownlink()
    turn = asyncio.run(
        _run_chat_turn_unarbitrated(
            downlink,
            _FakeChat(),
            "第3。",
            device_id="dev_gate",
            t_asr_start=time.monotonic(),
            t_asr_text=time.monotonic(),
        )
    )

    assert turn.need_reply is False
    assert turn.dialogue_act == "silent"
    assert turn.decision_valid is False
    assert turn.decision_respond is False
    assert turn.llm_text == ""
    # 门控阶段事件（页面可看到为何没响应）
    gate = [fields for stage, fields in downlink.stages if stage == "decision_gate"]
    assert gate and gate[0]["skip_llm"] is True
    assert gate[0]["valid"] is False and gate[0]["respond"] is False


def test_asr_turn_sends_decision_motion_even_when_skipping_llm(temp_db, monkeypatch):
    """决策输出动作/表情后立即走 PB（即使本轮不开口）。"""
    from deskbot_server.service.application import action_decision
    from deskbot_server.service.application import chat_flow as cf

    async def fake_decide(*_a, **_k):
        return {
            "valid": False,
            "respond": False,
            "moves": ["nod_head"],
            "anims": ["idle"],
            "model": "unee",
            "raw": "",
            "elapsed_ms": 5,
            "error": None,
        }

    sent: dict = {}

    def fake_spawn(chat, **kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(action_decision, "decide_turn_actions", fake_decide)
    monkeypatch.setattr(cf, "_spawn_decision_motion", fake_spawn)

    turn = asyncio.run(
        cf._run_chat_turn_unarbitrated(
            _FakeDownlink(),
            _FakeChat(),
            "嗯",
            device_id="dev_gate",
            t_asr_start=time.monotonic(),
            t_asr_text=time.monotonic(),
        )
    )
    assert turn.moves == ["nod_head"]
    assert turn.anims == ["idle"]
    assert sent.get("moves") == ["nod_head"]
    assert sent.get("anims") == ["idle"]
    assert turn.need_reply is False


def test_decision_failure_defaults_to_proceed(monkeypatch):
    """决策模型故障时不能把对话变哑：valid/respond 默认放行。"""
    from deskbot_server.service.application import action_decision as ad

    def boom(_device_id):
        raise RuntimeError("llm-unee down")

    monkeypatch.setattr(ad, "resolve_decision_llm_config", boom)
    out = asyncio.run(ad.decide_turn_actions("dev_gate", "你好", None, judge_input=True))
    assert out["valid"] is True and out["respond"] is True
    assert "llm-unee" in (out["error"] or "")
