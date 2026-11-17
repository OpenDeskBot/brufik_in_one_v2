from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from deskbot_server.client.flecket_live import FlecketLiveClient
from deskbot_server.model.pb_seq import PbType, PlaybackOutcome, PlaybackStatus
from deskbot_server.model.realtime import FlecketConfig
from deskbot_server.pb import llm_plan
from deskbot_server.service import robot_performance_service as performance
from deskbot_server.service.moss_live_service import FlecketBridge
from tests.test_flecket import Socket, audio, client
from tests.test_moss_live import cleanup, setup, until


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(performance, "list_llm_move_ids", lambda **kw: ["nod_head", "look_left"])
    monkeypatch.setattr(performance, "list_llm_expression_names", lambda **kw: ["happy"])
    monkeypatch.setattr(performance, "list_llm_expression_titles", lambda **kw: {"happy": "开心"})
    monkeypatch.setattr(llm_plan, "_resolve_servo_preset_steps", lambda *args, **kw: [
        {"x": 90, "y": 85, "xm": 0, "ym": 0, "ms": 400},
        {"x": 90, "y": 95, "xm": 0, "ym": 0, "ms": 400},
    ])
    monkeypatch.setattr(llm_plan, "resolve_anim_scene_frames", lambda *args, **kw: [
        {"ms": 400, "elements": {"eye_l": [{"shape": "circle", "x": 40, "y": 30, "r": 5}]}},
        {"ms": 400, "elements": {"eye_l": [{"shape": "circle", "x": 40, "y": 30, "r": 6}]}},
    ])
    return performance.RobotPerformanceService("robot")


def arguments(**overrides):
    return {"gesture": "nod_head", "expression": "happy", "duration_ms": 200, "timing": "with_speech", **overrides}


def tool_response(args, *, call_id="call-robot", rid="tool-response"):
    return {"id": rid, "status": "completed", "output": [{
        "type": "function_call", "call_id": call_id, "name": "robot_perform", "arguments": json.dumps(args),
    }]}


def outputs(ws):
    return [json.loads(e["item"]["output"]) for e in ws.sent if e["type"] == "conversation.item.create"]


def test_compile_uses_presets_and_explicit_duration(catalog):
    schema = catalog.schema()
    assert schema["parameters"]["properties"]["gesture"]["enum"] == ["none", "look_left", "nod_head"]
    assert schema["parameters"]["properties"]["expression"]["enum"] == ["none", "happy"]
    plan = catalog.compile(arguments())
    assert sum(x["ms"] for x in plan.servo) == sum(x["ms"] for x in plan.anim) == 200
    assert plan.servo[0]["y"] == 85 and plan.anim[0]["elements"]["eye_l"]
    assert plan.result("scheduled")["status"] == "scheduled"


@pytest.mark.parametrize("patch", [
    {"gesture": "__custom__"}, {"gesture": "unknown"}, {"expression": "unknown"},
    {"gesture": "none", "expression": "none"}, {"duration_ms": True},
    {"duration_ms": 199}, {"duration_ms": 3001}, {"duration_ms": 500.0},
    {"timing": "now"}, {"speak": "false"}, {"device_id": "someone-else"},
])
def test_invalid_arguments_never_compile(catalog, patch):
    with pytest.raises(ValueError):
        catalog.compile({**arguments(), **patch})


def test_removed_device_resource_is_rejected(catalog, monkeypatch):
    monkeypatch.setattr(performance, "list_llm_expression_names", lambda **kw: [])
    with pytest.raises(ValueError, match="不支持该表情"):
        catalog.compile(arguments())


def test_empty_catalog_does_not_advertise_tool(monkeypatch):
    monkeypatch.setattr(performance, "list_llm_move_ids", lambda **kw: [])
    monkeypatch.setattr(performance, "list_llm_expression_names", lambda **kw: [])
    assert performance.RobotPerformanceService("empty").schema() is None


@pytest.mark.parametrize("prompt", ["", "设备覆盖人设"])
def test_handshake_merges_saved_agent_tools_and_persona(catalog, monkeypatch, prompt):
    async def run():
        ws = Socket()
        clock = {"type": "function", "name": "get_current_time", "parameters": {"type": "object"}}
        ws.emit("session.created", session={"instructions": "原有 Agent 人设", "tools": [clock, {
            "type": "function", "name": "robot_perform", "description": "旧定义",
        }]})
        connect = AsyncMock(return_value=ws)
        monkeypatch.setattr("deskbot_server.client.flecket_live.connect", connect)
        value = FlecketLiveClient(FlecketConfig("test", prompt=prompt), robot_tool=catalog.schema(),
                                  robot_instructions=performance.ROBOT_TOOL_INSTRUCTIONS)
        await value.start()
        settings = ws.sent[0]["session"]
        assert settings["tools"] == [clock, catalog.schema()]
        assert settings["instructions"].startswith(prompt or "原有 Agent 人设")
        assert "speak=false" in settings["instructions"]
        assert "voice" not in settings["audio"]["output"]
        await value.close()
    asyncio.run(run())


def test_slow_tool_does_not_block_receiving_and_stale_turn_does_not_continue():
    async def run():
        value = client()
        gate = asyncio.Event()

        async def handler(*args, **kwargs):
            await gate.wait()
            return {"ok": True, "status": "scheduled", "performance_id": "old-plan"}

        value.performance_handler = handler
        value.ws.emit("response.done", response=tool_response(arguments()))
        value.ws.emit("input_audio_buffer.speech_started")
        assert (await value.receive()).action == "input.speech.started"
        gate.set()
        await until(lambda: bool(outputs(value.ws)))
        assert not any(e["type"] == "response.create" for e in value.ws.sent)
        await value.close()
    asyncio.run(run())


async def live_bridge(monkeypatch):
    value = client()
    monkeypatch.setattr(value, "start", AsyncMock())
    hub, entry, bridge, _, sent, worker = await setup(monkeypatch, client_override=value, bridge_type=FlecketBridge)
    return value, hub, entry, bridge, sent, worker


def acknowledge(entry, block):
    entry.ack_queue.put_nowait({"req": block.req, "epoch": block.epoch, "idx": block.idx, "ack_type": "pb_end"})


@pytest.mark.parametrize("timing", ["before_speech", "with_speech", "after_speech"])
def test_performance_plays_at_requested_phase_and_completion_waits_for_rom(catalog, monkeypatch, timing):
    async def run():
        value, _, entry, bridge, sent, worker = await live_bridge(monkeypatch)
        ws = value.ws
        try:
            ws.emit("response.done", response=tool_response({**arguments(), "timing": timing}))
            await until(lambda: any(e["type"] == "response.create" for e in ws.sent))
            assert outputs(ws)[0]["status"] == "scheduled"
            continuation = next(e for e in ws.sent if e["type"] == "response.create")["response"]
            assert continuation["tool_choice"] == "none"
            ws.emit("response.created", response={"id": "voice", "metadata": continuation["metadata"]})
            audio(ws, rid="voice", ms=80)
            ws.emit("response.output_audio.done", response_id="voice", item_id="item-voice")
            ws.emit("response.done", response={"id": "voice", "status": "completed", "metadata": continuation["metadata"]})
            if timing == "before_speech":
                await until(lambda: any(b.type == PbType.SINGLE for b, _ in sent))
                assert not any(b.audio for b, _ in sent)
                acknowledge(entry, next(b for b, _ in sent if b.type == PbType.SINGLE))
            await until(lambda: any(b.type == PbType.END for b, _ in sent))
            audio_blocks = [b for b, _ in sent if b.audio]
            if timing == "with_speech":
                assert audio_blocks[0].servo and audio_blocks[0].anim
                assert sum(b.chunk_ms for b in audio_blocks) == 200
                assert all(not b.anim and not b.servo for b in audio_blocks[1:])
            else:
                assert all(not b.anim and not b.servo for b in audio_blocks)
            assert not any(e.get("phase") == "completed" for e in ws.sent)
            run_state = bridge.runs[("voice", "voice")]
            # Padding is not reported as additional upstream speech.
            bridge.offer_ack({"req": run_state.req, "epoch": run_state.stream.seq.epoch,
                              "ack_type": "pb_progress", "played_ms": 200})
            await until(lambda: run_state.played_ms == 80)
            acknowledge(entry, next(b for b, _ in sent if b.type == PbType.END))
            if timing == "after_speech":
                await until(lambda: any(b.type == PbType.SINGLE for b, _ in sent))
                assert not any(e.get("phase") == "completed" for e in ws.sent)
                acknowledge(entry, next(b for b, _ in sent if b.type == PbType.SINGLE))
            await until(lambda: any(e.get("phase") == "completed" for e in ws.sent))
            assert not bridge._plans and not bridge._response_plans
        finally:
            await cleanup(bridge, worker)
    asyncio.run(run())


@pytest.mark.parametrize("interrupt", [False, True])
def test_silent_performance_is_deduplicated_and_never_generates_speech(catalog, monkeypatch, interrupt):
    async def run():
        value, _, entry, bridge, sent, worker = await live_bridge(monkeypatch)
        ws = value.ws
        response = tool_response(arguments(speak=False))
        try:
            ws.emit("response.done", response=response)
            ws.emit("response.done", response=response)
            await until(lambda: any(b.type == PbType.SINGLE for b, _ in sent))
            assert not outputs(ws)
            assert len([b for b, _ in sent if b.type == PbType.SINGLE]) == 1
            if interrupt:
                ws.emit("input_audio_buffer.speech_started")
                await until(lambda: any(b.type == PbType.CANCEL for b, _ in sent))
            else:
                acknowledge(entry, next(b for b, _ in sent if b.type == PbType.SINGLE))
            await until(lambda: bool(outputs(ws)))
            assert outputs(ws)[0]["status"] == ("cancelled" if interrupt else "completed")
            assert not any(e["type"] == "response.create" for e in ws.sent)
            assert not any(b.audio for b, _ in sent)
        finally:
            await cleanup(bridge, worker)
    asyncio.run(run())


def test_unrelated_response_cannot_consume_staged_plan(catalog, monkeypatch):
    async def run():
        value, _, _, bridge, sent, worker = await live_bridge(monkeypatch)
        try:
            result = await bridge._handle_performance(arguments(), call_id="plan", response_id="tool", generation=0)
            assert result["status"] == "scheduled"
            audio(value.ws, rid="unrelated", ms=80)
            await until(lambda: any(b.audio for b, _ in sent))
            assert all(not b.anim and not b.servo for b, _ in sent)
            assert "plan" in bridge._plans
            value.ws.emit("input_audio_buffer.speech_started")
            await until(lambda: not bridge._plans)
        finally:
            await cleanup(bridge, worker)
    asyncio.run(run())


@pytest.mark.parametrize("timing", ["before_speech", "with_speech", "after_speech"])
def test_interruption_cancels_motion_and_does_not_attach_it_to_next_turn(catalog, monkeypatch, timing):
    async def run():
        value, _, entry, bridge, sent, worker = await live_bridge(monkeypatch)
        ws = value.ws
        try:
            ws.emit("response.done", response=tool_response(arguments(timing=timing)))
            await until(lambda: any(e["type"] == "response.create" for e in ws.sent))
            marker = next(e for e in ws.sent if e["type"] == "response.create")["response"]["metadata"]
            ws.emit("response.created", response={"id": "old", "metadata": marker})
            audio(ws, rid="old", ms=80)
            ws.emit("response.output_audio.done", response_id="old", item_id="item-old")
            if timing == "after_speech":
                await until(lambda: any(b.type == PbType.END for b, _ in sent))
                acknowledge(entry, next(b for b, _ in sent if b.type == PbType.END))
            await until(lambda: any(b.anim or b.servo for b, _ in sent))
            ws.emit("input_audio_buffer.speech_started")
            await until(lambda: any(e.get("phase") == "interrupted" for e in ws.sent))
            await until(lambda: any(b.type == PbType.CANCEL for b, _ in sent))
            assert not any(e.get("phase") == "completed" for e in ws.sent)
            assert not bridge._plans
            audio(ws, rid="new", ms=80)
            await until(lambda: any(b.audio and b.req != sent[0][0].req for b, _ in sent))
            new_run = bridge.runs[("new", "new")]
            await until(lambda: any(b.req == new_run.req and b.audio for b, _ in sent))
            assert all(not b.anim and not b.servo for b, _ in sent if b.req == new_run.req)
        finally:
            await cleanup(bridge, worker)
    asyncio.run(run())


def test_connection_change_during_compilation_prevents_device_execution(catalog, monkeypatch):
    async def run():
        _, _, entry, bridge, sent, worker = await live_bridge(monkeypatch)
        started, release = threading.Event(), threading.Event()
        original = bridge.performances.compile

        def slow(args):
            started.set()
            release.wait(2)
            return original(args)

        monkeypatch.setattr(bridge.performances, "compile", slow)
        task = asyncio.create_task(bridge._handle_performance(arguments(speak=False), call_id="stale", response_id="r", generation=0))
        try:
            await until(started.is_set)
            entry.generation += 1
            release.set()
            assert (await task)["status"] == "cancelled"
            assert not sent
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await cleanup(bridge, worker)
    asyncio.run(run())


def test_concurrent_tool_responses_cannot_schedule_two_performances(catalog, monkeypatch):
    async def run():
        _, _, _, bridge, _, worker = await live_bridge(monkeypatch)
        try:
            results = await asyncio.gather(*[
                bridge._handle_performance(arguments(), call_id=f"plan-{i}", response_id=f"r-{i}", generation=0)
                for i in range(2)
            ])
            assert sum(bool(r.get("ok")) for r in results) == 1
            assert len(bridge._plans) == 1
        finally:
            await cleanup(bridge, worker)
    asyncio.run(run())


def test_device_failure_is_not_reported_as_completed(catalog, monkeypatch):
    async def run():
        value, hub, _, bridge, _, worker = await live_bridge(monkeypatch)
        monkeypatch.setattr(hub, "send_and_wait", AsyncMock(return_value=PlaybackOutcome(PlaybackStatus.DROPPED)))
        try:
            value.ws.emit("response.done", response=tool_response(arguments(speak=False)))
            await until(lambda: bool(outputs(value.ws)))
            assert outputs(value.ws)[0]["status"] == "dropped"
            assert outputs(value.ws)[0]["ok"] is False
            assert not any(e["type"] == "response.create" for e in value.ws.sent)
        finally:
            await cleanup(bridge, worker)
    asyncio.run(run())
