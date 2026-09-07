"""complete_llm_with_tool_loop(恒原生 function calling 通道)测试。

legacy 文本 tools 通道已移除：每轮走 ``llm_tool_round``（API tools 参数），
模型工具调用经 ``tool_calls`` 往返；输出 JSON envelope 不含 ``tools`` 字段。
"""

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


def _env(tts: str) -> str:
    return json.dumps(
        {"need_reply": True, "tts": tts, "gesture": [], "expression": []}, ensure_ascii=False
    )


def _round(content: str = "", tool_calls: list | None = None, raw: dict | None = None):
    from deskbot_server.infrastructure.llm.openai_compat import LlmToolRoundResult

    return LlmToolRoundResult(
        content=content,
        tool_calls=list(tool_calls or []),
        meta={"raw_response": raw} if raw is not None else {},
    )


def test_complete_llm_with_tool_loop_two_rounds(temp_db):
    """原生双轮：工具轮（tool_calls=memory_add）执行后回灌，收尾轮 content 即最终 envelope。"""
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    round1_raw = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "memory_add", "arguments": json.dumps({"text": "喜欢猫"})}}],
            }
        }]
    }
    answer2 = _env("已记住你喜欢猫")
    round2_raw = {"choices": [{"message": {"role": "assistant", "content": answer2}}]}

    chat = AsyncMock()
    chat.llm_tool_round = AsyncMock(
        side_effect=[
            _round(tool_calls=[{"id": "c1", "name": "memory_add", "arguments": json.dumps({"text": "喜欢猫"})}],
                   raw=round1_raw),
            _round(content=answer2, raw=round2_raw),
        ]
    )

    async def _run():
        return await complete_llm_with_tool_loop(chat, "记住我喜欢猫", device_id="deskbot_a", request_id="req1")

    turn = asyncio.run(_run())
    assert turn.parsed["reply"] == "已记住你喜欢猫"
    assert len(turn.tools) == 1
    assert turn.tools[0]["tool"] == "memory_add"
    assert len(turn.tool_results) == 1
    assert turn.tool_results[0]["ok"] is True
    assert chat.llm_tool_round.call_count == 2
    assert turn.answer == answer2
    assert turn.system_prompt is None
    # 逐次 LLM 调用明细：两次调用都要记录模型/耗时/结果
    assert len(turn.llm_calls) == 2
    assert [c["n"] for c in turn.llm_calls] == [1, 2]
    assert turn.llm_calls[0]["text"] == "[tool_calls: memory_add]"  # 工具轮 content 空 → 摘要占位
    assert turn.llm_calls[1]["text"] == answer2
    for call in turn.llm_calls:
        assert call["ms"] >= 0
        assert call["truncated"] is False
        assert set(call.keys()) == {"n", "model", "ms", "text", "truncated", "raw", "raw_truncated"}
        assert call["raw_truncated"] is False
    assert json.loads(turn.llm_calls[0]["raw"]) == round1_raw
    assert json.loads(turn.llm_calls[1]["raw"]) == round2_raw


def test_loop_pins_user_message_override_across_rounds(temp_db):
    """语音轮的 user_message_override 应整轮锁定：每次 LLM 调用都收到同一份。"""
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    answer2 = _env("已记住你喜欢猫")
    chat = AsyncMock()
    chat.llm_tool_round = AsyncMock(
        side_effect=[
            _round(tool_calls=[{"id": "c1", "name": "memory_add", "arguments": json.dumps({"text": "喜欢猫"})}]),
            _round(content=answer2),
        ]
    )
    override = "[图像识别:\n   faceid=2…\n]\n\n用户正文（语音转写，声纹判定：小明, 说话人识别置信度=0.87）：记住我喜欢猫"

    async def _run():
        return await complete_llm_with_tool_loop(
            chat,
            "记住我喜欢猫",
            device_id="deskbot_a",
            request_id="req_ovr",
            user_message_override=override,
        )

    asyncio.run(_run())
    assert chat.llm_tool_round.call_count == 2
    for call in chat.llm_tool_round.await_args_list:
        assert call.kwargs.get("user_message_override") == override
        # override 提供时不要求 device_context（第 0 轮仍在，语义不变）
        assert "user_message_override" in call.kwargs


def test_loop_no_override_keeps_plain_kwargs(temp_db):
    """未提供 override（文本/定时轮）时不应带该 kwarg，兼容旧调用与测试假对象。"""
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    answer = _env("你好")
    chat = AsyncMock()
    chat.llm_tool_round = AsyncMock(return_value=_round(content=answer))

    async def _run():
        return await complete_llm_with_tool_loop(chat, "你好", device_id="deskbot_a")

    asyncio.run(_run())
    assert chat.llm_tool_round.call_count == 1
    assert "user_message_override" not in chat.llm_tool_round.await_args.kwargs


def test_complete_llm_with_tool_loop_single_round(monkeypatch):
    from deskbot_server.infrastructure.llm.openai_compat import LlmToolRoundResult
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    answer = _env("你好")

    class _FakeChat:
        async def llm_tool_round(
            self,
            text,
            *,
            device_context=None,
            device_id=None,
            history_messages=None,
            extra_messages=None,
            tools=None,
            tool_choice="auto",
            on_system_prompt=None,
            user_message_override=None,
        ):
            if on_system_prompt:
                on_system_prompt("system")
            return LlmToolRoundResult(
                content=answer,
                tool_calls=[],
                meta={"raw_response": {"choices": [{"message": {"role": "assistant", "content": answer}}], "usage": {}}},
            )

    async def _run():
        return await complete_llm_with_tool_loop(_FakeChat(), "你好", device_id="deskbot_a")

    turn = asyncio.run(_run())
    assert turn.parsed["reply"] == "你好"
    assert turn.tools == []
    assert turn.tool_results == []
    assert turn.system_prompt == "system"
    # on_raw_response 语义现由 llm_tool_round 的 meta.raw_response 承担：序列化进该轮 llm_calls.raw
    assert len(turn.llm_calls) == 1
    assert json.loads(turn.llm_calls[0]["raw"])["choices"][0]["message"]["content"] == answer
    assert turn.llm_calls[0]["raw_truncated"] is False


def test_round_raw_response_in_llm_calls(temp_db):
    """每轮引擎原始返回体应序列化进 llm_calls.raw（工具轮/收尾轮同语义）。"""
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    answer = _env("已记住")
    raw_response = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    }

    chat = AsyncMock()
    chat.llm_tool_round = AsyncMock(return_value=_round(content=answer, raw=raw_response))

    async def _run():
        return await complete_llm_with_tool_loop(chat, "记住", device_id="deskbot_a", request_id="req_raw")

    turn = asyncio.run(_run())
    assert len(turn.llm_calls) == 1
    call = turn.llm_calls[0]
    assert call["text"] == answer
    assert json.loads(call["raw"]) == raw_response
    assert call["raw_truncated"] is False
