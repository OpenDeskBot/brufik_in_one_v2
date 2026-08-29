"""utils/phoneme_duration 单元测试：文本 → 音素 + 时长（默认补充）。"""

from __future__ import annotations

from deskbot_server.utils.phoneme_duration import (
    TimedPhoneme,
    text_to_phoneme_duration_dicts,
    text_to_phoneme_durations,
)


def test_chinese_pinyin_split_with_durations():
    timed = text_to_phoneme_durations("你好", 1000)
    assert [(t.phoneme, t.start_ms, t.end_ms) for t in timed] == [
        ("n", 0, 175),
        ("i", 175, 500),
        ("h", 500, 675),
        ("ao", 675, 1000),
    ]
    assert sum(t.duration_ms for t in timed) == 1000


def test_english_g2p_mouth_keys():
    timed = text_to_phoneme_durations("hello", 1000)
    assert [t.phoneme for t in timed] == ["h", "a", "l", "o"]
    assert sum(t.duration_ms for t in timed) == 1000


def test_punctuation_becomes_pause():
    timed = text_to_phoneme_durations("你好，world！", 1500)
    assert sum(t.duration_ms for t in timed) == 1500
    assert any(t.phoneme == "_" for t in timed)
    assert timed[0].start_ms == 0
    assert timed[-1].end_ms == 1500


def test_zero_or_negative_duration_returns_empty():
    assert text_to_phoneme_durations("你好", 0) == []
    assert text_to_phoneme_durations("你好", -5) == []


def test_empty_text_returns_single_pause_segment():
    assert text_to_phoneme_durations("", 1000) == [TimedPhoneme("_", 0, 1000)]


def test_doubao_style_api_dicts():
    rows = text_to_phoneme_duration_dicts("你好", 1000)
    assert rows[0]["phoneme"] == "n"
    assert rows[0]["start_time"] == 0
    assert rows[-1]["end_time"] == 1000
    assert all(r["duration_ms"] == r["end_time"] - r["start_time"] for r in rows)
    assert rows[0]["duration_ms"] == 175


def test_durations_always_conserved():
    for text in ("你好", "hello world", "你好，world！", "测试一下功能是否正常。"):
        timed = text_to_phoneme_durations(text, 2345)
        assert sum(t.duration_ms for t in timed) == 2345
        assert timed[0].start_ms == 0
        assert timed[-1].end_ms == 2345
