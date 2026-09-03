from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

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


def _force_legacy_channel(monkeypatch):
    """钉死 legacy 文本 tools 通道（设备显式关 native 时的兜底语义）。

    config 默认已开 native_tools；本用例模拟旧文本 tools 双轮路径，需显式关掉。
    """
    import deskbot_server.service.application.chat_flow as cf

    monkeypatch.setattr(cf, "native_tools_enabled", lambda dev: False)


def test_complete_llm_with_tool_loop_two_rounds(temp_db, monkeypatch):
    _force_legacy_channel(monkeypatch)
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    round1 = json.dumps(
        {"tts": "", "tools": [{"tool": "memory_add", "text": "喜欢猫"}], "moves": [], "anims": []}, ensure_ascii=False
    )
    round2 = json.dumps({"tts": "已记住你喜欢猫", "tools": [], "moves": [], "anims": []}, ensure_ascii=False)

    chat = AsyncMock()
    chat.llm = AsyncMock(side_effect=[round1, round2])

    async def _run():
        return await complete_llm_with_tool_loop(chat, "记住我喜欢猫", device_id="deskbot_a", request_id="req1")

    turn = asyncio.run(_run())
    assert turn.parsed["reply"] == "已记住你喜欢猫"
    assert len(turn.tools) == 1
    assert turn.tools[0]["tool"] == "memory_add"
    assert len(turn.tool_results) == 1
    assert turn.tool_results[0]["ok"] is True
    assert chat.llm.call_count == 2
    assert turn.answer == round2
    assert turn.system_prompt is None
    # 逐次 LLM 调用明细：两次调用都要记录模型/耗时/结果
    assert len(turn.llm_calls) == 2
    assert [c["n"] for c in turn.llm_calls] == [1, 2]
    for call, expected_text in zip(turn.llm_calls, [round1, round2], strict=True):
        assert call["text"] == expected_text
        assert call["ms"] >= 0
        assert call["truncated"] is False
        assert set(call.keys()) == {"n", "model", "ms", "text", "truncated"}


def test_loop_pins_user_message_override_across_rounds(temp_db, monkeypatch):
    """语音轮的 user_message_override 应整轮锁定：每次 LLM 调用都收到同一份。"""
    _force_legacy_channel(monkeypatch)
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    round1 = json.dumps(
        {"tts": "", "tools": [{"tool": "memory_add", "text": "喜欢猫"}], "moves": [], "anims": []}, ensure_ascii=False
    )
    round2 = json.dumps({"tts": "已记住你喜欢猫", "tools": [], "moves": [], "anims": []}, ensure_ascii=False)

    chat = AsyncMock()
    chat.llm = AsyncMock(side_effect=[round1, round2])
    override = "[图像识别:\n   faceid=2…\n]\n\n用户正文: 记住我喜欢猫"

    async def _run():
        return await complete_llm_with_tool_loop(
            chat,
            "记住我喜欢猫",
            device_id="deskbot_a",
            request_id="req_ovr",
            user_message_override=override,
        )

    asyncio.run(_run())
    assert chat.llm.call_count == 2
    for call in chat.llm.await_args_list:
        assert call.kwargs.get("user_message_override") == override
        # override 提供时不要求 device_context（第 0 轮仍在，语义不变）
        assert "user_message_override" in call.kwargs


def test_loop_no_override_keeps_plain_kwargs(temp_db):
    """未提供 override（文本/定时轮）时不应带该 kwarg，兼容旧调用与测试假对象。"""
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    answer = json.dumps({"tts": "你好", "tools": [], "moves": [], "anims": []})
    chat = AsyncMock()
    chat.llm = AsyncMock(return_value=answer)

    async def _run():
        return await complete_llm_with_tool_loop(chat, "你好", device_id="deskbot_a")

    asyncio.run(_run())
    assert chat.llm.call_count == 1
    assert "user_message_override" not in chat.llm.await_args.kwargs


def test_complete_llm_with_tool_loop_single_round(monkeypatch):
    _force_legacy_channel(monkeypatch)
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    answer = json.dumps({"tts": "你好", "tools": [], "moves": [], "anims": []})

    class _FakeChat:
        async def llm(
            self,
            text,
            *,
            device_context=None,
            device_id=None,
            history_messages=None,
            extra_messages=None,
            on_tts_ready=None,
            on_system_prompt=None,
        ):
            if on_system_prompt:
                on_system_prompt("system")
            return answer

    async def _run():
        return await complete_llm_with_tool_loop(_FakeChat(), "你好", device_id="deskbot_a")

    turn = asyncio.run(_run())
    assert turn.parsed["reply"] == "你好"
    assert turn.tools == []
    assert turn.tool_results == []
    assert turn.system_prompt == "system"
