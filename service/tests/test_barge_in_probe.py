from __future__ import annotations

import asyncio

import pytest

from deskbot_server.service.application.barge_in_probe import BargeInProbe, is_interrupt_command


@pytest.mark.parametrize('text', ['停一下，现在几点？', '请先停下', '小歪，别说了', '等一下', '停', '听一下', '听一下现在几点'])
def test_explicit_interrupt_commands(text):
    assert is_interrupt_command(text)


@pytest.mark.parametrize('text', ['因为手机停电了', '现在是下午一点', '你好', '这个笑话很好笑'])
def test_other_speech_is_not_an_early_stop(text):
    assert not is_interrupt_command(text)


def test_rolling_probe_finds_stop_without_a_second_vad_onset():
    async def run():
        texts = iter(['为啥小兔子不打游戏', '停一下现在几点'])
        confirmed = []
        token = (1, 0, 0, 'reply')
        async def transcribe(pcm, sr):
            return next(texts)
        async def confirm(tok, text):
            confirmed.append((tok, text))
        probe = BargeInProbe(transcribe, lambda: token, confirm, lambda *_: False)
        try:
            probe.feed(b'voice', 16000)
            await probe._task
            assert not confirmed
            await asyncio.sleep(.31)
            probe.feed(b'continued voice including stop', 16000)
            await probe._task
            assert confirmed == [(token, '停一下现在几点')]
        finally:
            await probe.aclose()
    asyncio.run(run())


def test_late_preview_cannot_cancel_a_new_response():
    async def run():
        token = [1]
        release = asyncio.Event()
        confirmed = []
        async def transcribe(*_):
            await release.wait()
            return '停一下'
        async def confirm(*args):
            confirmed.append(args)
        probe = BargeInProbe(transcribe, lambda: tuple(token), confirm, lambda *_: False)
        probe.feed(b'voice', 16000)
        token[0] = 2
        release.set()
        await probe._task
        assert not confirmed
        await probe.aclose()
    asyncio.run(run())


def test_speaker_saying_stop_is_still_echo():
    async def run():
        confirmed = []
        async def transcribe(*_):
            return '停一下'
        async def confirm(*_):
            confirmed.append(True)
        probe = BargeInProbe(transcribe, lambda: ('reply',), confirm, lambda *_: True)
        probe.feed(b'voice', 16000)
        await probe._task
        assert not confirmed
        await probe.aclose()
    asyncio.run(run())


def test_only_one_preview_runs_and_disconnect_joins_it():
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()
        calls = []
        async def transcribe(*_):
            calls.append(True)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def confirm(*_):
            raise AssertionError('no result')
        probe = BargeInProbe(transcribe, lambda: ('reply',), confirm, lambda *_: False)
        probe.feed(b'voice', 16000)
        await started.wait()
        probe.feed(b'more voice', 16000)
        assert len(calls) == 1
        await probe.aclose()
        assert cancelled.is_set() and probe._task.done()
        assert not probe.ready()
    asyncio.run(run())


def test_preview_http_deadline_does_not_change_full_asr_timeout(monkeypatch):
    import io

    from deskbot_server.client.asr.funasr_adapter import FunAsrAdapter
    from deskbot_server.model.settings import AsrTextFilterSettings
    calls = []
    def request(req, *, timeout):
        calls.append(timeout)
        return io.BytesIO(b'{"text":"stop"}')
    monkeypatch.setattr('urllib.request.urlopen', request)
    adapter = FunAsrAdapter('http://127.0.0.1:9102', AsrTextFilterSettings())
    async def run():
        assert await adapter.transcribe_preview(b'pcm', 16000) == 'stop'
        assert await adapter.transcribe(b'pcm', 16000) == 'stop'
    asyncio.run(run())
    assert calls == [.6, 30.0]


def test_raw_preview_detects_stop_when_processed_vad_is_silent():
    async def run():
        confirmed = []
        async def transcribe(pcm, sr):
            assert sr == 8000 and len(pcm) >= 8000
            return '听一下现在几点'
        async def confirm(*args): confirmed.append(args)
        token = [1, 0, 0, 'reply']
        probe = BargeInProbe(transcribe, lambda: tuple(token), confirm, lambda *_: False)
        try:
            for _ in range(13):
                probe.feed_raw(b'\x10\0' * 320)
            await probe._task
            assert len(confirmed) == 1
            assert len(probe.raw_snapshot()[0]) <= 14400
            token[1] += 1  # confirmed cancellation changes input revision
            await asyncio.sleep(.31)
            probe.feed_raw(b'\x10\0' * 320)
            assert len(confirmed) == 1  # same old playback must not reset the new utterance
        finally:
            await probe.aclose()
    asyncio.run(run())


def test_raw_preview_upsamples_before_external_asr(monkeypatch):
    import io

    from deskbot_server.client.asr.funasr_adapter import FunAsrAdapter
    from deskbot_server.model.settings import AsrTextFilterSettings
    observed = []
    def request(req, *, timeout):
        observed.append((req.data, req.get_header('X-sample-rate')))
        return io.BytesIO(b'{"text":"stop"}')
    monkeypatch.setattr('urllib.request.urlopen', request)
    adapter = FunAsrAdapter('http://127.0.0.1:9102', AsrTextFilterSettings())
    asyncio.run(adapter.transcribe_preview(b'\x10\0' * 4000, 8000))
    assert len(observed[0][0]) == 16000 and observed[0][1] == '16000'
