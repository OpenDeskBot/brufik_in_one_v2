"""text_split 单元测试。"""

from __future__ import annotations

from deskbot_server.utils.text_split import (
    split_tts_by_punctuation,
    split_tts_for_latency,
)


def test_split_single_sentence():
    assert split_tts_by_punctuation("你好") == ["你好"]
    assert split_tts_by_punctuation("你好。") == ["你好。"]


def test_split_by_commas_and_periods():
    assert split_tts_by_punctuation("你好，我是小歪。") == ["你好，我是小歪。"]
    assert split_tts_by_punctuation("好的！今天不错？") == ["好的！", "今天不错？"]
    assert split_tts_by_punctuation("画面里没人，我看不到你啦。") == ["画面里没人，我看不到你啦。"]


def test_split_empty():
    assert split_tts_by_punctuation("") == []
    assert split_tts_by_punctuation("   ") == []


def test_split_for_latency_disabled_keeps_old_behavior():
    text = "你好，我是小歪。"
    assert split_tts_for_latency(text, max_chars=0) == split_tts_by_punctuation(text)


def test_split_for_latency_short_sentence_unchanged():
    assert split_tts_for_latency("你好。", max_chars=16) == ["你好。"]


def test_split_for_latency_splits_long_sentence_at_comma():
    text = "小明你好，请问有什么我可以帮你的吗？"
    chunks = split_tts_for_latency(text, max_chars=16)
    assert len(chunks) >= 2
    assert all(len(c) <= 16 for c in chunks)
    assert "".join(chunks) == text


def test_split_for_latency_hard_splits_long_clause():
    text = "一二三四五六七八九十一二三四五六七八九十"
    chunks = split_tts_for_latency(text, max_chars=16)
    assert all(len(c) <= 16 for c in chunks)
    assert "".join(chunks) == text
