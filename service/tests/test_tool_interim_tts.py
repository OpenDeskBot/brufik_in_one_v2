"""tool 兜底过渡 TTS。"""

from __future__ import annotations

from deskbot_server.service.application.tool_interim_tts import (
    build_tool_interim_tts,
    interim_text_ok,
    phrase_for_tool,
    resolve_interim_tts,
)


def test_build_tool_interim_tts_single():
    text = build_tool_interim_tts([{"tool": "websearch", "query": "天气"}])
    assert text == "我帮你搜一下。"


def test_build_tool_interim_tts_merge_dedupe():
    tools = [{"tool": "websearch", "query": "a"}, {"tool": "capture_camera"}, {"name": "websearch", "query": "b"}]
    text = build_tool_interim_tts(tools)
    assert "搜一下" in text
    assert "看一下画面" in text
    assert not text.startswith("稍等")


def test_build_tool_interim_tts_unknown_tool():
    """未知工具兜底句不再叠加「稍等」前缀（旧版会拼成「稍等，稍等一下。」）。"""
    text = build_tool_interim_tts([{"tool": "unknown_xyz"}])
    assert text == "我想一下啊。"


def test_fast_tools_stay_silent():
    """本地毫秒级工具不插话：这些调用在被感知为「刚刚发生」的窗口里完成。"""
    for tool in ("memory_add", "update_daily_task", "session", "schedule_task", "update_user_info"):
        assert phrase_for_tool(tool) == ""
        assert build_tool_interim_tts([{"tool": tool}]) == ""
    assert build_tool_interim_tts([{"tool": "memory_add"}, {"tool": "session"}]) == ""


def test_build_tool_interim_tts_empty():
    assert build_tool_interim_tts([]) == ""


# ────────────────── 三层降级与文本门禁 ──────────────────


def test_resolve_prefers_say_tool():
    out = resolve_interim_tts(say_text="我帮你搜一下", model_text="我看看", tools=[{"tool": "websearch"}])
    assert out == "我帮你搜一下"


def test_resolve_falls_back_to_model_envelope():
    out = resolve_interim_tts(model_text="让我翻一下资料", tools=[{"tool": "websearch"}])
    assert out == "让我翻一下资料"


def test_resolve_falls_back_to_table():
    out = resolve_interim_tts(tools=[{"tool": "websearch", "query": "天气"}])
    assert out == "我帮你搜一下。"


def test_resolve_rejects_result_prediction():
    """模型预报还没发生的结果 → 丢弃，退到词表（宁可不说也不说错）。"""
    assert interim_text_ok("我查到了，明天22度") is False
    out = resolve_interim_tts(say_text="我查到了，明天22度", tools=[{"tool": "websearch"}])
    assert out == "我帮你搜一下。"


def test_resolve_rejects_malformed_text():
    assert interim_text_ok("") is False
    assert interim_text_ok("好") is True
    assert interim_text_ok("我帮你查一下这段很长的过渡语超过二十个字就太啰嗦了不该播") is False
    assert interim_text_ok('{"tts": "查一下"}') is False
    assert interim_text_ok("**我查一下**") is False


def test_resolve_silent_when_no_source():
    """模型没说 + 全是快工具 → 静默（不插话）。"""
    assert resolve_interim_tts(say_text="我查到了", tools=[{"tool": "memory_add"}]) == ""
    assert resolve_interim_tts(tools=[]) == ""
