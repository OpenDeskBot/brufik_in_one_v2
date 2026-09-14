from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace

from deskbot_server.model.chat import ChatTurnResult
from deskbot_server.model.pb_seq import PbAction, PlaybackOutcome, PlaybackStatus
from deskbot_server.service.application.chat_flow import _voice_was_played
from deskbot_server.service.application.interaction_arbiter import DeviceInteractionArbiter, InteractionKind


def test_proactive_event_is_dropped_while_device_busy():
    async def _run() -> None:
        arbiter = DeviceInteractionArbiter()
        lease = await arbiter.acquire("dev1", kind=InteractionKind.SCHEDULED, request_id="timer")
        assert lease is not None
        dropped = await arbiter.acquire("dev1", kind=InteractionKind.SOCIAL, request_id="social")
        assert dropped is None
        lease.release()

    asyncio.run(_run())


def test_scheduled_events_are_serialized_fifo():
    async def _run() -> None:
        arbiter = DeviceInteractionArbiter()
        first = await arbiter.acquire("dev1", kind=InteractionKind.SCHEDULED, request_id="one")
        assert first is not None

        second_task = asyncio.create_task(
            arbiter.acquire("dev1", kind=InteractionKind.SCHEDULED, request_id="two")
        )
        await asyncio.sleep(0)
        assert not second_task.done()

        first.release()
        second = await asyncio.wait_for(second_task, timeout=1)
        assert second is not None and second.request_id == "two"
        second.release()

    asyncio.run(_run())


def test_scheduled_event_cancels_unspoken_proactive_turn():
    async def _run() -> None:
        arbiter = DeviceInteractionArbiter()
        proactive_started = asyncio.Event()
        proactive_cancelled = asyncio.Event()

        async def _proactive() -> None:
            lease = await arbiter.acquire("dev1", kind=InteractionKind.SOCIAL, request_id="social")
            assert lease is not None
            try:
                proactive_started.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                proactive_cancelled.set()
                raise
            finally:
                lease.release()

        proactive_task = asyncio.create_task(_proactive())
        await proactive_started.wait()
        timer = await asyncio.wait_for(
            arbiter.acquire("dev1", kind=InteractionKind.SCHEDULED, request_id="timer"), timeout=1
        )
        assert timer is not None
        assert proactive_cancelled.is_set()
        timer.release()
        with suppress(asyncio.CancelledError):
            await proactive_task

    asyncio.run(_run())


def test_scheduled_event_waits_for_proactive_audio_already_started():
    async def _run() -> None:
        arbiter = DeviceInteractionArbiter()
        release_proactive = asyncio.Event()
        proactive_started = asyncio.Event()

        async def _proactive() -> None:
            lease = await arbiter.acquire("dev1", kind=InteractionKind.QUEST, request_id="quest")
            assert lease is not None
            try:
                arbiter.mark_current_speaking("dev1", request_id="quest")
                proactive_started.set()
                await release_proactive.wait()
            finally:
                lease.release()

        proactive_task = asyncio.create_task(_proactive())
        await proactive_started.wait()
        timer_task = asyncio.create_task(
            arbiter.acquire("dev1", kind=InteractionKind.SCHEDULED, request_id="timer")
        )
        await asyncio.sleep(0)
        assert not timer_task.done()
        assert not proactive_task.cancelled()

        release_proactive.set()
        await proactive_task
        timer = await asyncio.wait_for(timer_task, timeout=1)
        assert timer is not None
        timer.release()

    asyncio.run(_run())


def test_user_turn_cancels_system_turn_and_goes_next():
    async def _run() -> None:
        arbiter = DeviceInteractionArbiter()
        scheduled_started = asyncio.Event()

        async def _scheduled() -> None:
            lease = await arbiter.acquire("dev1", kind=InteractionKind.SCHEDULED, request_id="timer")
            assert lease is not None
            try:
                scheduled_started.set()
                await asyncio.Event().wait()
            finally:
                lease.release()

        scheduled_task = asyncio.create_task(_scheduled())
        await scheduled_started.wait()
        user = await asyncio.wait_for(
            arbiter.acquire("dev1", kind=InteractionKind.USER, request_id="user"), timeout=1
        )
        assert user is not None
        assert scheduled_task.cancelled() or scheduled_task.done()
        user.release()
        with suppress(asyncio.CancelledError):
            await scheduled_task

    asyncio.run(_run())


def test_preempted_playback_is_not_reported_as_voice_success():
    result = ChatTurnResult(
        t_llm_end=1.0,
        t_tts_synth_end=2.0,
        t_tts_end=3.0,
        playback_completed=False,
        playback_status="preempted",
    )
    assert _voice_was_played(result) is False


def test_ordinary_playback_appends_and_records_real_outcome(monkeypatch):
    import deskbot_server.service.application.chat_flow as chat_flow

    captured: dict = {}

    def _fake_build(_segs, _cfg, **kwargs):
        captured["action"] = kwargs["action"]
        req = kwargs["request_id"]
        return [({"type": "pb_single", "req": req, "idx": 0, "chunk_ms": 40, "action": kwargs["action"]}, [])], req, 1, 16000

    monkeypatch.setattr(chat_flow, "build_pb_wire_pairs", _fake_build)

    class _Chat:
        tts_cfg = {"sample_rate": 16000}
        settings = SimpleNamespace(pb_random_servo_cfg=lambda: None)

        async def tts_phoneme_segments(self, _text, **_kwargs):
            return 16000, [{"pcm": b"\0" * 1280, "ms": 40, "phoneme": "a"}]

    class _Device:
        async def send_and_wait(self, _device_id, seq):
            captured["seq_action"] = seq.action
            return PlaybackOutcome(PlaybackStatus.COMPLETED, req=seq.req)

    async def _run() -> ChatTurnResult:
        result = ChatTurnResult(t_llm_end=1.0)
        await chat_flow._run_pb_playback(
            _Chat(),
            reply_text="你好",
            parsed={"moves": [], "anims": [], "servo": []},
            llm_scenes=[],
            request_id="reply",
            device_id="dev1",
            result=result,
            t_asr_start=None,
            device_ws=_Device(),
        )
        return result

    result = asyncio.run(_run())
    assert captured["action"] == "append"
    assert captured["seq_action"] == PbAction.APPEND
    assert result.playback_completed is True
    assert result.playback_status == "completed"
