from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from deskbot_server.model.pb_seq import PlaybackOutcome, PlaybackStatus
from deskbot_server.service.application import action_decision, chat_flow
from deskbot_server.service.llm_service import LlmToolRoundResult


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    path = tmp_path / "test.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(path))
    reset_engine()
    init_engine(path)
    init_database()
    yield
    reset_engine()


class _Downlink:
    async def emit_stage(self, *_args, **_kwargs):
        pass


class _Device:
    def __init__(self):
        self.sent = []
        self.first_audio = asyncio.Event()
        self.interrupts = []

    async def send_and_wait(self, _device_id, seq):
        self.sent.append(seq)
        if any(block.binaries for block in seq.entries):
            self.first_audio.set()
        return PlaybackOutcome(PlaybackStatus.COMPLETED, req=seq.req)

    async def interrupt_current_playback(self, _device_id, *, reason):
        self.interrupts.append(reason)

    async def pb_ack_llm_context(self, _device_id):
        return None


class _Chat:
    supports_streaming = True
    settings = SimpleNamespace(pb_random_servo_cfg=lambda: None)
    tts_cfg = {"sample_rate": 16000}

    async def tts_phoneme_segments(self, text, **_kwargs):
        return 16000, [{"pcm": b"\x10\x00" * 3200, "ms": 200, "phoneme": "a"}]


def test_trailing_scenes_keep_append_policy_instead_of_replacing_each_other(isolated_db, monkeypatch):
    from deskbot_server.model.chat import ChatTurnResult
    from deskbot_server.model.pb_seq import PbAction

    async def run():
        device = _Device()

        async def send(_dev, seq):
            device.sent.append(seq)
            return 1

        device.send = send
        monkeypatch.setattr(chat_flow, "_pb_scene_entry_by_name", lambda *_a, **_kw: {})
        monkeypatch.setattr(chat_flow, "_prepare_pb_scene_chain_frames", lambda _scene, runtime_req, **_kw: [
            {"type": "pb_single", "req": runtime_req, "idx": 0, "chunk_ms": 100, "action": "append"}
        ])
        await chat_flow._run_pb_playback(
            _Chat(), reply_text="", parsed={}, llm_scenes=["happy", "curious"],
            request_id="scenes", device_id="dev_stream", result=ChatTurnResult(),
            t_asr_start=None, device_ws=device,
        )
        assert len(device.sent) == 2
        assert all(seq.action == PbAction.APPEND for seq in device.sent)
        assert device.sent[0].req != device.sent[1].req

    asyncio.run(run())


def _voice_kwargs(device):
    return dict(
        device_id="dev_stream",
        request_id="req_stream",
        device_ws=device,
        t_asr_start=time.monotonic(),
        t_asr_text=time.monotonic(),
    )


@pytest.mark.parametrize("valid, respond, text", [
    (True, True, "聊聊天"),
    (False, True, "在吗？"),
    (False, False, "现在几点啦？"),
])
def test_first_audio_contains_motion_without_waiting_for_separate_motion_ack(isolated_db, monkeypatch, valid, respond, text):
    async def run():
        release = asyncio.Event()
        device = _Device()

        class Chat(_Chat):
            async def llm_tool_round(self, _text, *, on_delta, **_kwargs):
                await on_delta("今天天气很好，我们出去走走吧。")
                await release.wait()
                await on_delta("记得带上水。")
                return LlmToolRoundResult("今天天气很好，我们出去走走吧。记得带上水。", [], {})

        async def decide(*_args, **_kwargs):
            return {
                "valid": valid,
                "respond": respond,
                "moves": [{"move": "__custom__", "x": 5, "y": 0, "xm": 1, "ym": 2, "ms": 200}],
                "anims": [],
                "elapsed_ms": 1,
            }

        monkeypatch.setattr(action_decision, "decide_turn_actions", decide)

        def separate_motion(*_args, **_kwargs):
            raise AssertionError("speaking turn must not enqueue a separate decision-motion sequence")

        monkeypatch.setattr(chat_flow, "_spawn_decision_motion", separate_motion)
        task = asyncio.create_task(chat_flow.run_chat_turn(_Downlink(), Chat(), text, **_voice_kwargs(device)))
        try:
            await asyncio.wait_for(device.first_audio.wait(), 2)
            assert not task.done(), "first audio must precede model completion"
            assert all("_dec" not in seq.req for seq in device.sent)
            first = device.sent[0]
            assert first.entries[0].binaries, "first PB must carry speech, not leading silence"
            assert any(block.servo for block in first.entries)
            release.set()
            turn = await asyncio.wait_for(task, 2)
            assert turn.playback_completed
            assert turn.t_tts_first_audio < turn.t_llm_end
            assert len(device.sent) >= 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_user_interrupt_cancels_pending_synthesis_and_never_plays_old_audio(isolated_db, monkeypatch):
    async def run():
        old_synth, old_cancelled = asyncio.Event(), asyncio.Event()
        device = _Device()

        class Chat(_Chat):
            async def llm_tool_round(self, text, *, on_delta, **_kwargs):
                phrase = "这是旧的回答，不应该继续播放。" if text == "旧问题" else "这是新的回答，已经开始播放。"
                await on_delta(phrase)
                if text == "旧问题":
                    await asyncio.Event().wait()
                return LlmToolRoundResult(phrase, [], {})

            async def tts_phoneme_segments(self, text, **kwargs):
                if "旧的" in text:
                    old_synth.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        old_cancelled.set()
                return await super().tts_phoneme_segments(text, **kwargs)

        async def decide(*_args, **_kwargs):
            return {"valid": True, "respond": True, "moves": [], "anims": []}

        monkeypatch.setattr(action_decision, "decide_turn_actions", decide)
        kwargs = _voice_kwargs(device)
        old = asyncio.create_task(chat_flow.run_chat_turn(_Downlink(), Chat(), "旧问题", **kwargs))
        await asyncio.wait_for(old_synth.wait(), 2)
        kwargs["request_id"] = "req_new"
        new = asyncio.create_task(chat_flow.run_chat_turn(_Downlink(), Chat(), "新问题", **kwargs))
        try:
            result = await asyncio.wait_for(new, 2)
            assert old.cancelled() and old_cancelled.is_set()
            assert result.playback_completed
            assert device.sent and all(seq.req.startswith("req_new_") for seq in device.sent)
            assert device.interrupts.index("turn_cancelled:req_stream") < device.interrupts.index("user_turn:req_new")
        finally:
            old.cancel()
            new.cancel()
            await asyncio.gather(old, new, return_exceptions=True)

    asyncio.run(run())


def test_json_final_after_streamed_tool_transition_is_still_spoken(isolated_db, monkeypatch):
    async def run():
        device = _Device()
        spoken = []

        class Chat(_Chat):
            def __init__(self):
                self.rounds = 0

            async def llm_tool_round(self, _text, *, on_delta, **_kwargs):
                self.rounds += 1
                if self.rounds == 1:
                    await on_delta("我先帮你算一下。")
                    return LlmToolRoundResult(
                        "我先帮你算一下。", [{"id": "c1", "name": "calculate", "arguments": "{}"}], {}
                    )
                return LlmToolRoundResult('{"need_reply":true,"tts":"计算结果是四。"}', [], {})

            async def tts_phoneme_segments(self, text, **kwargs):
                spoken.append(text)
                return await super().tts_phoneme_segments(text, **kwargs)

        async def execute(*_args, **_kwargs):
            return [{"ok": True, "result": 4}]

        async def decide(*_args, **_kwargs):
            return {"valid": True, "respond": True, "moves": [], "anims": []}

        monkeypatch.setattr(chat_flow, "_execute_tools_round", execute)
        monkeypatch.setattr(action_decision, "decide_turn_actions", decide)
        turn = await chat_flow.run_chat_turn(_Downlink(), Chat(), "算一下", **_voice_kwargs(device))
        assert turn.playback_completed
        assert spoken.count("我先帮你算一下。") == 1
        assert "计算结果是四。" in spoken

    asyncio.run(run())


def test_realtime_voice_does_not_wait_for_optional_decision(isolated_db, monkeypatch):
    from deskbot_server.service import llm_routing

    async def run():
        decision_started = asyncio.Event()
        decision_release = asyncio.Event()
        model_release = asyncio.Event()
        device = _Device()
        calls = []

        async def decide(*args, **kwargs):
            calls.append(True)
            decision_started.set()
            await decision_release.wait()
            return {"moves": [], "anims": [], "model": "unee"}

        class Chat(_Chat):
            async def llm_tool_round(self, _text, *, on_delta, **kwargs):
                await on_delta("在呢，我听着，你慢慢说。")
                await model_release.wait()
                return LlmToolRoundResult("在呢，我听着，你慢慢说。", [], {})

        monkeypatch.setattr(llm_routing, 'resolve_device_llm_provider', lambda _: 'realtime')
        monkeypatch.setattr(action_decision, 'decide_turn_actions', decide)
        task = asyncio.create_task(chat_flow.run_chat_turn(_Downlink(), Chat(), '在吗？', **_voice_kwargs(device)))
        try:
            await asyncio.wait_for(decision_started.wait(), 1)
            await asyncio.wait_for(device.first_audio.wait(), 1)
            assert not decision_release.is_set()
            decision_release.set()
            model_release.set()
            result = await asyncio.wait_for(task, 2)
            assert result.playback_completed
            assert len(calls) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_emoji_tail_is_not_synthesized_as_spoken_symbol_names(isolated_db, monkeypatch):
    async def run():
        spoken = []
        class Chat(_Chat):
            async def llm_tool_round(self, _text, *, on_delta, **kwargs):
                await on_delta('这个笑话真有趣。')
                await on_delta('！ 😄')
                return LlmToolRoundResult('这个笑话真有趣。！ 😄', [], {})
            async def tts_phoneme_segments(self, text, **kwargs):
                spoken.append(text)
                return await super().tts_phoneme_segments(text, **kwargs)

        async def decide(*args, **kwargs):
            return {'valid': True, 'respond': True, 'moves': [], 'anims': []}

        monkeypatch.setattr(action_decision, 'decide_turn_actions', decide)
        turn = await chat_flow.run_chat_turn(_Downlink(), Chat(), '说个笑话', **_voice_kwargs(_Device()))
        assert turn.playback_completed
        assert spoken == ['这个笑话真有趣。']
        assert '😄' not in turn.llm_text
    asyncio.run(run())
