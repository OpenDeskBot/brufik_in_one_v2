"""say 过渡语工具：schema 首轮注入 + 执行层拦截。"""

from __future__ import annotations

import asyncio

from deskbot_server.infrastructure.llm.tool_schema import NATIVE_TOOL_NAMES_BATCH1, build_native_tool_schemas
from deskbot_server.service.application.llm_tool_runner import execute_llm_tools


def _names(**kw: object) -> list[str]:
    return [s["function"]["name"] for s in build_native_tool_schemas(**kw)]  # type: ignore[arg-type]


def test_say_only_on_first_tool_round():
    """say 只在首轮工具轮注入：后续轮模型看不到它，自然不会重复插话。"""
    assert "say" in _names(is_tool_round=True)
    assert "say" not in _names()
    assert "say" not in _names(device_id=None, include_batch2=False)


def test_say_appended_keeps_batch1_prefix_and_export():
    first = _names(is_tool_round=True)
    assert first[:6] == NATIVE_TOOL_NAMES_BATCH1
    assert first[-1] == "say"  # 附在末尾，不改既有顺序
    assert NATIVE_TOOL_NAMES_BATCH1 == [
        "memory_add", "memory_delete", "schedule_task", "webfetch", "websearch", "session",
    ]


def test_say_schema_contract():
    sch = next(s for s in build_native_tool_schemas(is_tool_round=True) if s["function"]["name"] == "say")
    fn = sch["function"]
    assert fn["parameters"]["required"] == ["text"]
    assert set(fn["parameters"]["properties"]) == {"text"}
    # 触发规则承载在 description：必须同时调用 + 禁止预报结果
    assert "同时" in fn["description"]
    assert "单独调用" in fn["description"]


def test_runner_say_returns_text_without_side_effect():
    """与真工具同时调用：say 立即回哨兵（文本回传调用方），不阻塞、不下发。"""
    results = asyncio.run(
        execute_llm_tools(
            [{"tool": "say", "text": "我帮你查一下"}, {"tool": "websearch", "query": "天气"}],
            device_id="dev_say",
        )
    )
    say = next(r for r in results if r["tool"] == "say")
    assert say["ok"] is True and say["reply"] == "我帮你查一下"


def test_runner_say_alone_is_rejected():
    """单独调用 say 不产生任何动作 → 回错误，让模型下一轮补上真正的工具调用。"""
    results = asyncio.run(execute_llm_tools([{"tool": "say", "text": "我记一下"}], device_id="dev_say"))
    assert results[0]["ok"] is False
    assert "单独调用" in results[0]["error"]
