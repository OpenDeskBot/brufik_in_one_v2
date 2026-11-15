from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from deskbot_server.service.application.uplink_preview import split_uplink_preview


def test_processed_opus_is_unchanged_by_raw_trailer():
    opus = b'\x03\x00abc\x04\x00defg'
    raw = b'\x0a\x00' * 320
    assert split_uplink_preview({'raw_bytes': 640, 'opus_bytes': len(opus), 'raw_sr': 8000}, opus + raw) == (opus, raw)
    assert split_uplink_preview({}, opus) == (opus, b'')


@pytest.mark.parametrize('change', [
    {'raw_bytes': 642}, {'raw_bytes': 3}, {'raw_sr': 16000}, {'opus_bytes': 0}, {'opus_bytes': 10},
])
def test_corrupt_trailer_cannot_be_mistaken_for_opus(change):
    header = {'raw_bytes': 640, 'opus_bytes': 4, 'raw_sr': 8000}
    header.update(change)
    with pytest.raises(ValueError):
        split_uplink_preview(header, b'opus' + b'\0' * 640)


def test_confirmed_interruption_retains_raw_followup_instead_of_processed_echo():
    from deskbot_server.service.pipeline.audio import ConnectionSession
    class Vad:
        speech_starts = 0
        def __init__(self): self.seen = []
        def reset(self): self.seen.clear()
        def feed_pcm(self, pcm):
            self.seen.append(pcm)
            return pcm if len(self.seen) >= 2 else None
    async def run():
        session = ConnectionSession.__new__(ConnectionSession)
        session.lock = asyncio.Lock()
        session._vad = Vad()
        session.rom_sr, session.rom_ch, session.rom_codec = 16000, 1, 'pcm16'
        session._raw_barge_until = 0
        session._uplink_open = True
        session._pcm_buffer = bytearray()
        session._decode = lambda payload, *a, **kw: payload
        session.audio_cfg = SimpleNamespace(input_codec='pcm16')
        await session.begin_raw_barge(b'\x10\0' * 4000, 8000)
        raw_question = b'\x20\0' * 320
        utterance, _, _ = await session.feed_audio(b'\x70\0' * 640, 'pcm16', raw_preview=raw_question)
        assert utterance == b'\x20\0' * 640
        assert session._raw_barge_until > 0  # a short stop-only fragment must not discard the question
        session.end_raw_barge()  # final ASR accepted the new question
        assert session._raw_barge_until == 0
    asyncio.run(run())


def test_clock_query_streams_current_time_without_calling_model(monkeypatch):
    from deskbot_server.service import llm_routing
    from deskbot_server.service.application import chat_flow, local_clock_reply
    monkeypatch.setattr(llm_routing, 'resolve_device_llm_provider', lambda _: 'realtime')
    original = local_clock_reply.local_clock_reply
    monkeypatch.setattr(local_clock_reply, 'local_clock_reply', lambda text: original(text, now=datetime.datetime(2026, 11, 10, 14, 59)))
    class Chat:
        async def llm_tool_round(self, *a, **kw):
            raise AssertionError('clock must not wait on a model')
    async def run():
        spoken = []
        async def delta(text): spoken.append(text)
        async def end(): return True
        result = await chat_flow.complete_llm_with_tool_loop(Chat(), '现在几点了？', device_id='robot',
                  pipeline_source='asr', on_delta=delta, on_round_end=end)
        assert spoken == ['现在是14点59分。']
        assert result.reply_streamed and result.llm_calls[0]['model'] == 'local_clock'
    asyncio.run(run())


@pytest.mark.parametrize('text', ['明天几点提醒我', '现在几点开门', '你为什么总问几点了'])
def test_other_time_related_requests_keep_model_and_tools(text):
    from deskbot_server.service.application.local_clock_reply import local_clock_reply
    assert local_clock_reply(text) is None


@pytest.mark.parametrize(('heard', 'question', 'confirmed', 'engaged'), [
    ('猫去森林找星星，停一下，现在几点？', '现在几点？', True, False),
    ('停一下。', None, True, False),
    ('能给我讲一个笑话吗？', '能给我讲一个笑话吗？', False, False),
    ('讲笑话。', '讲笑话。', False, False),
    ('今天有点累。', '今天有点累。', False, True),
    ('明天我们去逛街吧。', None, False, False),
])
def test_addressed_input_keeps_question_despite_stale_camera(monkeypatch, heard, question, confirmed, engaged):
    import time

    from deskbot_server.service import asr_service, voiceprint_service
    from deskbot_server.service.application import (
        asr_attention_gate,
        capability_labels,
        interaction_arbiter,
        ws_chat_turn,
    )
    from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry
    from deskbot_server.ws import stages
    async def run():
        DeviceWsService.reset_instance()
        hub = DeviceWsService()
        ws = object()
        entry = _DeviceEntry(device_id='robot', ws=ws, speech_revision=3,
                             barge_until_mono=time.monotonic() + 8 if confirmed else 0,
                             user_engaged_until_mono=time.monotonic() + 30 if engaged else 0)
        hub._devices['robot'] = entry
        events = []
        async def transcribe(*a, **kw): return heard
        async def attention(*a, **kw): return asr_attention_gate.AttentionVerdict(False, 'no_face', '')
        async def emit(*a, **kw): pass
        async def interrupt(*a, **kw): events.append('interrupt')
        async def chat(_ws, _pipeline, text, **kw):
            events.append(text)
            return {'llm_text': '现在是14点59分。'}
        monkeypatch.setattr(asr_service.AsrService, 'transcribe', transcribe)
        monkeypatch.setattr(asr_service.AsrService, 'is_valid_text', lambda *a, **kw: True)
        monkeypatch.setattr(voiceprint_service.VoiceprintService, 'enabled', lambda _: False)
        monkeypatch.setattr(asr_attention_gate, 'decide_round', attention)
        monkeypatch.setattr(capability_labels, 'asr_model_label', lambda _: 'local')
        monkeypatch.setattr(stages, '_emit_stage', emit)
        monkeypatch.setattr(ws_chat_turn, 'run_ws_chat_turn', chat)
        monkeypatch.setattr(ws_chat_turn, 'publish_ws_chat_turn', emit)
        monkeypatch.setattr(interaction_arbiter.interaction_arbiter, 'interrupt', lambda _: None)
        monkeypatch.setattr(hub, 'interrupt_current_playback', interrupt)
        await hub._run_asr_turn(ws, object(), b'\0\0' * 16000, device_id='robot', speech_revision=3)
        assert events == (['interrupt', question] if question else [])
        DeviceWsService.reset_instance()
    asyncio.run(run())
