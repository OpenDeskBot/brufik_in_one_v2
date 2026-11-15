from __future__ import annotations

import asyncio
import wave
from types import SimpleNamespace

import pytest

from deskbot_server.client.tts import macos_adapter
from deskbot_server.client.tts.macos_adapter import MacOsTtsAdapter


def settings():
    return SimpleNamespace(tts=SimpleNamespace(sample_rate=16000))


@pytest.fixture(autouse=True)
def empty_phrase_cache():
    macos_adapter._PCM_CACHE.clear()
    yield
    macos_adapter._PCM_CACHE.clear()


def test_system_voice_preserves_pcm_duration_and_robot_phonemes(monkeypatch):
    async def run():
        class Process:
            returncode = 0
            async def communicate(self, text):
                assert text == '你好。'.encode()
                return b'', b''

        async def spawn(*args, **kwargs):
            assert args[0] == '/usr/bin/say' and '-f' in args
            output = args[args.index('-o')+1]
            with wave.open(output, 'wb') as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(24000)
                target.writeframes(b'\x01\x00' * 24000)
            return Process()

        monkeypatch.setattr(macos_adapter, 'sys', SimpleNamespace(platform='darwin'))
        monkeypatch.setattr(macos_adapter.asyncio, 'create_subprocess_exec', spawn)
        sr, segments = await MacOsTtsAdapter(settings()).synthesize_phoneme_segments('你好。')
        assert sr == 16000
        assert sum(len(segment.pcm) for segment in segments) == 32000
        assert sum(segment.ms for segment in segments) == pytest.approx(1000, abs=2)
        assert any(segment.phoneme for segment in segments)
    asyncio.run(run())


def test_barge_in_kills_and_joins_running_system_speech(monkeypatch):
    async def run():
        begun = asyncio.Event()
        class Process:
            returncode = None
            killed = False
            joined = False
            async def communicate(self, text):
                begun.set()
                await asyncio.Event().wait()
            def kill(self):
                self.killed = True
                self.returncode = -9
            async def wait(self):
                self.joined = True

        process = Process()
        async def spawn(*args, **kwargs):
            return process
        monkeypatch.setattr(macos_adapter, 'sys', SimpleNamespace(platform='darwin'))
        monkeypatch.setattr(macos_adapter.asyncio, 'create_subprocess_exec', spawn)
        task = asyncio.create_task(MacOsTtsAdapter(settings()).synthesize_phoneme_segments('被打断的一句话'))
        await begun.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and process.killed and process.joined
    asyncio.run(run())


def test_system_voice_rejects_invalid_rate():
    with pytest.raises(ValueError):
        MacOsTtsAdapter(settings(), overrides={'rate': 500})


def test_short_phrase_cache_preserves_audio_and_separates_voice_settings(monkeypatch):
    async def run():
        calls = []
        class Process:
            returncode = 0
            async def communicate(self, text):
                return b'', b''

        async def spawn(*args, **kwargs):
            calls.append(args)
            with wave.open(args[args.index('-o') + 1], 'wb') as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(24000)
                target.writeframes(b'\x01\x00' * 24000)
            return Process()

        monkeypatch.setattr(macos_adapter, 'sys', SimpleNamespace(platform='darwin'))
        monkeypatch.setattr(macos_adapter.asyncio, 'create_subprocess_exec', spawn)
        adapter = MacOsTtsAdapter(settings())
        first = await adapter.synthesize_phoneme_segments('在呢。')
        second = await adapter.synthesize_phoneme_segments('在呢。')
        assert len(calls) == 1
        assert [(s.pcm, s.ms, s.phoneme) for s in first[1]] == [(s.pcm, s.ms, s.phoneme) for s in second[1]]
        await MacOsTtsAdapter(settings(), {'rate': 240}).synthesize_phoneme_segments('在呢。')
        assert len(calls) == 2
    asyncio.run(run())


def test_realtime_model_routes_to_independent_local_service():
    from deskbot_server.service.llm_routing import _local_device_llm_config
    cfg = _local_device_llm_config('realtime')
    assert cfg.model == 'realtime-local'
    assert cfg.api_base == 'http://127.0.0.1:9108/v1'
    assert cfg.context_window == 8192
    assert not cfg.api_key


def test_realtime_prompt_keeps_clock_out_of_reusable_system_prefix(monkeypatch):
    from deskbot_server.client.llm.runtime import ResolvedLlmConfig
    from deskbot_server.config import load_config
    from deskbot_server.model.settings import AppSettings
    from deskbot_server.service import llm_context, llm_service

    cfg = ResolvedLlmConfig(model='realtime-local', api_key='', api_base='http://127.0.0.1:9108/v1',
                            protocol='openai', source='device', display_name='local')
    monkeypatch.setattr(llm_service, 'resolve_llm_config', lambda *a, **kw: cfg)
    monkeypatch.setattr(llm_context, 'llm_user_last_talk_prompt_appendix', lambda **kw: '')
    times = iter(['2026-11-10 12:00:00', '2026-11-10 12:00:05'])
    monkeypatch.setattr(llm_context, 'beijing_time_str', lambda: next(times))
    captured = []

    async def completion(messages, **kwargs):
        captured.append(messages)
        return '在呢。', [], {}

    monkeypatch.setattr(llm_service, 'tool_acompletion', completion)
    service = llm_service.OpenAiLlmAdapter(AppSettings.from_config(load_config()))
    # Keep this test focused on placement, leaving the real system builder
    # contract covered by the llm_context tests.
    monkeypatch.setattr(service, '_build_system_prompt', lambda **kw: '固定规则' if kw['cacheable'] else 'volatile')

    async def run():
        await service.llm_tool_round('在吗？', user_message_override='用户正文：在吗？')
        await service.llm_tool_round('现在几点？', user_message_override='用户正文：现在几点？')
    asyncio.run(run())
    assert captured[0][0] == captured[1][0]
    assert '12:00:00' in captured[0][-1]['content']
    assert '12:00:05' in captured[1][-1]['content']


def test_model_warmup_only_populates_prefix_without_tools_or_speech(monkeypatch):
    from deskbot_server.client.llm.runtime import ResolvedLlmConfig
    from deskbot_server.config import load_config
    from deskbot_server.model.settings import AppSettings
    from deskbot_server.service import llm_service, llm_tools_schema

    cfg = ResolvedLlmConfig(model='realtime-local', api_key='', api_base='http://127.0.0.1:9108/v1',
                            protocol='openai', source='device', display_name='local')
    monkeypatch.setattr(llm_service, 'resolve_llm_config', lambda _: cfg)
    monkeypatch.setattr(llm_tools_schema, 'build_native_tool_schemas', lambda **kw: [])
    service = llm_service.OpenAiLlmAdapter(AppSettings.from_config(load_config()))
    monkeypatch.setattr(service, '_build_system_prompt', lambda **kw: 'fixed-prefix')
    calls = []

    async def request(messages, **kwargs):
        calls.append((messages, kwargs))
        return '好', [], {}

    monkeypatch.setattr(llm_service, 'tool_acompletion', request)
    asyncio.run(service.warmup_realtime('robot'))
    assert calls[0][0][0]['content'] == 'fixed-prefix'
    assert calls[0][1]['config'].extra_body['max_tokens'] == 1
    assert 'on_delta' not in calls[0][1]
