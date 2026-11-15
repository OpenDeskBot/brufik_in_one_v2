from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from deskbot_server.model.pb_seq import PbType, PlaybackStatus
from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry
from deskbot_server.utils.util import _normalize_incoming_pb_ack


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


def phrase(req, count=2):
    return [({"type": "pb_start" if i == 0 else "pb_end", "req": req, "idx": i,
              "chunk_ms": 500, "sr": 16000, "ch": 1, "fmt": "s16le",
              "audio": {"next_bin_len": 16000}, "servo": [{"x": 5, "ms": 100}],
              "anim": [{"ms": 100, "elements": {"mouth": []}}]}, [b'\x00\x00'*8000])
            for i in range(count)]


def setup(monkeypatch):
    DeviceWsService.reset_instance()
    svc = DeviceWsService()
    entry = _DeviceEntry(device_id="physical", capabilities={"pb_stream_v1"})
    svc._devices[entry.device_id] = entry
    sent = []

    async def send(dev, block, **kw):
        sent.append(block)
        return True

    monkeypatch.setattr(svc, "_do_send_to_device", send)
    return svc, entry, sent


def ack(stream, idx, kind="pb_credit"):
    return {"type": "pb_ack", "req": stream.seq.req, "epoch": stream.seq.epoch,
            "idx": idx, "ack_type": kind}


async def stop(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_phrases_share_chain_and_credit_is_not_completion(monkeypatch):
    async def run():
        svc, entry, sent = setup(monkeypatch)
        stream = await svc.open_speech_stream("physical", "response", sr=16000, fmt="s16le")
        worker = asyncio.create_task(svc._device_loop("physical"))
        try:
            await stream.append(phrase("phrase-one"))
            for i in range(2):
                await until(lambda i=i: len(sent) > i)
                entry.ack_queue.put_nowait(ack(stream, i))
            # No playback completion between phrases, even with slow display.
            await stream.append(phrase("phrase-two"))
            for i in range(2, 4):
                await until(lambda i=i: len(sent) > i)
                entry.ack_queue.put_nowait(ack(stream, i))
            waiter = asyncio.create_task(stream.finish())
            await until(lambda: len(sent) == 5)
            assert [b.type for b in sent] == [PbType.START, PbType.CHUNK, PbType.CHUNK, PbType.CHUNK, PbType.END]
            assert [b.pts_ms for b in sent] == [0, 500, 1000, 1500, 2000]
            assert {b.req for b in sent} == {"response"}
            assert sent[0].anim and sent[0].servo and sent[0].binaries
            entry.ack_queue.put_nowait(ack(stream, 4, "pb_audio_end"))
            await asyncio.sleep(.02)
            assert not waiter.done()
            entry.ack_queue.put_nowait(ack(stream, 4, "pb_end"))
            assert (await waiter).completed
        finally:
            await stop(worker)
    asyncio.run(run())


def test_stale_epoch_and_old_credit_cannot_advance(monkeypatch):
    async def run():
        svc, entry, sent = setup(monkeypatch)
        stream = await svc.open_speech_stream("physical", "response", sr=16000, fmt="s16le")
        worker = asyncio.create_task(svc._device_loop("physical"))
        try:
            await stream.append(phrase("p", count=4))
            await until(lambda: len(sent) == 2)
            entry.ack_queue.put_nowait({**ack(stream, 1), "epoch": 0})
            entry.ack_queue.put_nowait(ack(stream, 1, "pb_started"))
            await asyncio.sleep(.02)
            assert len(sent) == 2
            entry.ack_queue.put_nowait(ack(stream, 1))
            await until(lambda: len(sent) == 4)
            entry.ack_queue.put_nowait(ack(stream, 1))
            await asyncio.sleep(.02)
            assert not stream.seq._done.is_set()
            await svc.interrupt_current_playback("physical", reason="test")
            await until(stream.seq._done.is_set)
            assert stream.seq.outcome.status == PlaybackStatus.PREEMPTED
            assert sent[-1].type == PbType.CANCEL and sent[-1].epoch == stream.seq.epoch
        finally:
            await stop(worker)
    asyncio.run(run())


def test_cancel_wakes_idle_producer_and_renews_epoch(monkeypatch):
    async def run():
        svc, entry, sent = setup(monkeypatch)
        stream = await svc.open_speech_stream("physical", "old", sr=16000, fmt="s16le")
        worker = asyncio.create_task(svc._device_loop("physical"))
        try:
            await until(lambda: entry.sending_seq is stream.seq)
            await svc.interrupt_current_playback("physical", reason="speech_started")
            await until(stream.seq._done.is_set)
            assert sent[-1].type == PbType.CANCEL
            newer = await svc.open_speech_stream("physical", "new", sr=16000, fmt="s16le")
            assert newer.seq.epoch > stream.seq.epoch
            with pytest.raises(RuntimeError):
                await stream.append(phrase("stale"))
        finally:
            await stop(worker)
    asyncio.run(run())


def test_device_error_unblocks_full_producer(monkeypatch):
    async def run():
        svc, entry, sent = setup(monkeypatch)
        stream = await svc.open_speech_stream("physical", "error", sr=16000, fmt="s16le")
        worker = asyncio.create_task(svc._device_loop("physical"))
        producer = asyncio.create_task(stream.append(phrase("p", count=12)))
        try:
            await until(lambda: len(sent) >= 1)
            entry.ack_queue.put_nowait(ack(stream, 0, "pb_error"))
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(producer, 1)
            assert stream.seq.outcome.status == PlaybackStatus.DROPPED
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            await stop(worker)
    asyncio.run(run())


def test_legacy_fallback(monkeypatch):
    async def run():
        svc, entry, _ = setup(monkeypatch)
        entry.capabilities.clear()
        assert await svc.open_speech_stream("physical", "old", sr=16000, fmt="s16le") is None
    asyncio.run(run())


def test_wire_preserves_progress():
    raw = {"type": "pb_ack", "ack_type": "pb_started", "req": "r", "idx": 3,
           "epoch": 42, "queue_wait_ms": 17, "played_ms": 20}
    assert _normalize_incoming_pb_ack(raw) == {**raw, "space": 0}


def test_vad_reports_onset_before_utterance_and_only_once(monkeypatch):
    import numpy as np

    from deskbot_server.service.pipeline.silero_vad import SileroVadConfig, SileroVadStream
    # Exercise onset timing/state independent of acoustic model accuracy.
    vad = object.__new__(SileroVadStream)
    vad.cfg = SileroVadConfig(model_path="unused", min_speech_ms=250, min_silence_ms=200)
    vad.sample_rate = 16000
    vad._speech_pcm = bytearray()
    vad._pre_roll = bytearray()
    vad._pre_roll_cap = 0
    vad._client_have_voice = False
    vad._onset_reported = False
    vad._voiced_ms = 0
    vad.speech_starts = 0
    vad._chunk_ms = 32
    vad._audio_ms_processed = vad._last_voice_audio_ms = 0
    voiced = True
    monkeypatch.setattr(vad, "_classify_chunk", lambda _: voiced)
    pcm = np.zeros(512, dtype=np.int16).tobytes()
    for _ in range(7):
        assert vad._process_chunk(pcm) is None
    assert vad.speech_starts == 0
    assert vad._process_chunk(pcm) is None
    assert vad.speech_starts == 1  # 256ms, before ASR or end-of-speech.
    for _ in range(15):
        assert vad._process_chunk(pcm) is None
    assert vad.speech_starts == 1
    voiced = False
    utterance = None
    for _ in range(8):
        utterance = vad._process_chunk(pcm) or utterance
    assert utterance and vad.speech_starts == 1


def test_onset_interrupt_cancels_generation_without_new_turn():
    from deskbot_server.service.application.interaction_arbiter import DeviceInteractionArbiter, InteractionKind

    async def run():
        arbiter = DeviceInteractionArbiter()
        acquired = asyncio.Event()

        async def owner():
            lease = await arbiter.acquire("robot", kind=InteractionKind.USER)
            async with lease:
                acquired.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(owner())
        await acquired.wait()
        arbiter.interrupt("robot")
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert not arbiter.is_busy("robot")
    asyncio.run(run())


def test_open_wakes_already_idle_worker(monkeypatch):
    async def run():
        svc, entry, sent = setup(monkeypatch)
        worker = asyncio.create_task(svc._device_loop("physical"))
        try:
            await asyncio.sleep(.02)
            stream = await svc.open_speech_stream("physical", "wake", sr=16000, fmt="s16le")
            await stream.append(phrase("p"))
            await until(lambda: len(sent) > 0)
        finally:
            await stop(worker)
    asyncio.run(run())


def test_old_database_columns_do_not_break_device_mapping():
    from types import SimpleNamespace

    from deskbot_server.dao.decorators import _map_row
    from deskbot_server.db.models import Device
    row = SimpleNamespace(_mapping={"device_id": "robot", "owner_user_id": "owner", "pin_code": "obsolete"})
    device = _map_row(row, Device)
    assert device.device_id == "robot"
    assert device.owner_user_id == "owner"
    assert not hasattr(device, "pin_code")


@pytest.mark.parametrize("tool_round", [False, True])
def test_real_service_passes_resolved_config_to_model_client(monkeypatch, tool_round):
    from types import SimpleNamespace

    from deskbot_server.service import llm_service, llm_tools_schema
    cfg = SimpleNamespace(api_base="http://127.0.0.1:9106/v1", model="fake")
    service = llm_service.OpenAiLlmAdapter.__new__(llm_service.OpenAiLlmAdapter)
    service._http = None
    monkeypatch.setattr(service, "_build_system_prompt", lambda **kw: "reply briefly")
    monkeypatch.setattr(llm_service, "resolve_llm_config", lambda _: cfg)
    monkeypatch.setattr(llm_tools_schema, "build_native_tool_schemas", lambda **kw: [])
    captured = []

    async def complete(messages, **kwargs):
        captured.append(kwargs["config"])
        return ("在呢。", [], {}) if tool_round else ("在呢。", {})

    monkeypatch.setattr(llm_service, "tool_acompletion" if tool_round else "chat_acompletion", complete)
    method = service.llm_tool_round if tool_round else service.complete
    asyncio.run(method("在吗？", user_message_override="在吗？"))
    assert captured == [cfg]


def test_old_producer_cleanup_cannot_cancel_newer_playback(monkeypatch):
    from deskbot_server.model.pb_seq import PbSeq

    async def run():
        svc, entry, _ = setup(monkeypatch)
        old = PbSeq(req="old")
        newer = PbSeq(req="new")
        entry.queue[:] = [old]
        entry.sending_seq = newer
        assert await svc.interrupt_current_playback("physical", reason="old_failed", expected_req="old") == 1
        assert old.outcome.status == PlaybackStatus.PREEMPTED
        assert newer.outcome is None
        assert entry.ack_queue.empty()
    asyncio.run(run())


@pytest.mark.parametrize("local_idle", [False, True])
def test_local_idle_capability_suppresses_legacy_idle_scheduler(monkeypatch, local_idle):
    from types import SimpleNamespace
    from deskbot_server.service.device_ws_service import _IDLE

    async def run():
        svc, entry, sent = setup(monkeypatch)
        if local_idle:
            entry.capabilities.add("local_idle_v1")
        calls = []
        svc.bind_live_service(SimpleNamespace(get_live_pb_seq=lambda dev: calls.append(dev)))
        values = iter([_IDLE, None])

        async def dequeue(*args, **kwargs):
            return next(values)

        monkeypatch.setattr(svc, "_dequeue", dequeue)
        await svc._device_loop(entry.device_id)
        assert calls == ([] if local_idle else [entry.device_id])
        assert not sent

    asyncio.run(run())


def test_invalid_playback_is_dropped_and_worker_can_play_next_sequence(monkeypatch):
    from deskbot_server.model.pb_seq import PbBlock, PbSeq

    async def run():
        svc, entry, sent = setup(monkeypatch)
        bad = PbSeq(req="bad", entries=(PbBlock(type=PbType.SINGLE, req="bad"),))
        good = PbSeq(req="good", entries=(PbBlock(type=PbType.SINGLE, req="good"),))

        async def send(dev, block, **kw):
            if block.req == "bad":
                raise ValueError("invalid optional payload")
            sent.append(block)
            entry.ack_queue.put_nowait({"type": "pb_ack", "req": "good", "idx": 0, "ack_type": "pb_end"})
            return True

        monkeypatch.setattr(svc, "_do_send_to_device", send)
        entry.queue.extend([bad, good])
        entry.event.set()
        worker = asyncio.create_task(svc._device_loop(entry.device_id))
        try:
            await until(lambda: good._done.is_set())
            assert bad.outcome.status == PlaybackStatus.DROPPED
            assert "invalid_playback" in bad.outcome.reason
            assert good.outcome.completed
            assert not entry.stopped
        finally:
            await stop(worker)

    asyncio.run(run())
