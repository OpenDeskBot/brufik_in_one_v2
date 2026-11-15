from __future__ import annotations

import asyncio

import pytest

from deskbot_server.service.application.streaming_speech import StreamingSpeech


def test_phrase_plays_before_final_and_next_synthesis_overlaps_playback():
    async def run():
        playing, release, next_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        spoken = []

        async def synth(text):
            if text.startswith("第二"):
                next_ready.set()
            return text

        async def play(text, _audio, index):
            spoken.append(text)
            if index == 0:
                playing.set()
                await release.wait()

        speech = StreamingSpeech(synth, play, str.strip)
        try:
            await speech.feed("第一句话已经准备好了。")
            await asyncio.wait_for(playing.wait(), 1)
            await speech.feed("第二句话也可以合成了。")
            await asyncio.wait_for(next_ready.wait(), 1)
            assert len(spoken) == 1
            release.set()
            await speech.finish()
            assert spoken == ["第一句话已经准备好了。", "第二句话也可以合成了。"]
        finally:
            await speech.cancel()

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    ['{"need_reply":true,"tts":"不能直接念 JSON"}', "（系统已识别，按规则保持安静。）", "<think>internal</think>"],
)
def test_structured_or_reasoning_prefix_waits_for_existing_parser(text):
    async def run():
        spoken = []

        async def synth(text):
            spoken.append(text)

        async def play(*_):
            raise AssertionError("must not play structured output")

        speech = StreamingSpeech(synth, play, str.strip)
        try:
            for character in text:
                await speech.feed(character)
            await speech.finish()
            assert not speech.used and not spoken
        finally:
            await speech.cancel()

    asyncio.run(run())


def test_timer_emits_short_phrase_without_waiting_for_next_model_delta():
    async def run():
        ready = asyncio.Event()

        async def synth(text):
            return text

        async def play(*_):
            ready.set()

        speech = StreamingSpeech(synth, play, str.strip, wait_seconds=0.01)
        try:
            await speech.feed("这是没有标点的短句")
            await asyncio.wait_for(ready.wait(), 1)
            await speech.finish()
        finally:
            await speech.cancel()

    asyncio.run(run())


def test_worker_failure_unblocks_backpressure_and_cancels_all_workers():
    async def run():
        async def synth(_):
            raise RuntimeError("TTS down")

        async def play(*_):
            raise AssertionError("failed synthesis must not play")

        speech = StreamingSpeech(synth, play, str.strip)
        try:
            with pytest.raises(RuntimeError, match="TTS down"):
                await asyncio.wait_for(speech.feed("这是一句很长的话。" * 40), 1)
        finally:
            await speech.cancel()
        assert all(task.done() for task in speech._tasks)

    asyncio.run(run())
