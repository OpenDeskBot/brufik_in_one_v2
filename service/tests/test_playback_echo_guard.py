from __future__ import annotations

import pytest

from deskbot_server.service.application import playback_echo_guard as module
from deskbot_server.service.application.playback_echo_guard import PlaybackEchoGuard


@pytest.fixture
def playback(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    guard = PlaybackEchoGuard()
    guard.note_text("reply", "不太清楚你说的阿里是啥意思，能再说一遍吗？")
    guard.acknowledge({"req": "reply", "ack_type": "pb_started"})
    return guard, clock


@pytest.mark.parametrize("text", ["清楚你说的。", "阿里。", "我看清楚。", "晚上。"])
def test_actual_feedback_fragments_cannot_start_another_reply(playback, text):
    guard, _ = playback
    assert guard.defer_onset()
    assert guard.reject(text, segment_start=100.1, segment_end=101.2)


@pytest.mark.parametrize("text", ["停", "停一下，现在几点？", "不是，我想听音乐。", "今天的天气怎么样？", "现在几点", "小歪你好"])
def test_real_interruption_is_retained_during_playback(playback, text):
    guard, _ = playback
    assert guard.reject(text, segment_start=100.1, segment_end=101.2) is None


def test_short_normal_input_after_acoustic_tail_is_not_filtered(playback):
    guard, clock = playback
    clock[0] = 102.0
    guard.acknowledge({"req": "reply", "ack_type": "pb_audio_end"})
    clock[0] = 102.6
    assert not guard.defer_onset()
    assert guard.reject("好的", segment_start=102.5, segment_end=103) is None
    # A transcription arriving later still carries the overlap interval.
    assert guard.reject("阿里", segment_start=101, segment_end=102.3)


def test_stale_cancel_ack_does_not_disable_current_echo_protection(playback):
    guard, clock = playback
    clock[0] = 101
    guard.acknowledge({"req": "new", "ack_type": "pb_started"})
    guard.acknowledge({"req": "reply", "ack_type": "pb_cancelled"})
    clock[0] = 102
    assert guard.defer_onset()


def test_missing_end_ack_cannot_leave_microphone_guard_on_forever(playback):
    guard, clock = playback
    clock[0] = 146
    assert not guard.defer_onset()


def test_long_feedback_with_asr_substitutions_does_not_become_a_new_question(playback):
    guard, _ = playback
    guard.note_text('reply', '为啥小兔子从不打游戏？')
    guard.note_text('reply', '因为一按“开始”就蹦出来了！')
    assert guard.reject('为小兔子从不打游戏，因为一旦开始就不出来掩盖效益。',
                        segment_start=100, segment_end=105) == 'speaker_text_partial'
    assert guard.reject('你刚才说的小兔子，我想养一只，应该怎么照顾它？',
                        segment_start=100, segment_end=105) is None


def test_homophone_in_speakers_own_stop_phrase_is_still_feedback(playback):
    guard, _ = playback
    guard.note_text('reply', '我们先停一下，休息休息。')
    assert guard.spoken_fragment('听一下', 'reply')
    assert guard.reject('听一下', segment_start=100, segment_end=101)
    assert guard.reject('听一下现在几点', segment_start=100, segment_end=101) is None


def test_time_reply_prefix_cannot_cancel_itself(playback):
    guard, _ = playback
    guard.note_text('reply', '现在是14点03分。')
    assert guard.reject('现在。', segment_start=100, segment_end=101) == 'speaker_text'
    assert guard.reject('现在几点？', segment_start=100, segment_end=101) is None


def test_greeting_feedback_is_not_mistaken_for_a_new_direct_request(playback):
    guard, clock = playback
    guard.note_text('reply', '在呢，怎么了？')
    assert guard.reject('在吗。', segment_start=100, segment_end=101)
    clock[0] = 146
    assert guard.reject('在吗。', segment_start=146, segment_end=147) is None
