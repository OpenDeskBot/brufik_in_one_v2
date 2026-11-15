from __future__ import annotations

import asyncio
import base64
import json

import pytest

from deskbot_server.client import flecket_live
from deskbot_server.client.flecket_live import FlecketLiveClient, realtime_url
from deskbot_server.model.errors import ClientError
from deskbot_server.model.realtime import FLECKET_URL, FlecketConfig


class Socket:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.emit("session.updated")

    async def recv(self):
        return json.dumps(await self.events.get())

    async def close(self):
        self.closed = True

    def emit(self, kind, **data):
        self.events.put_nowait({"type": kind, **data})


def client(camera=False):
    value = FlecketLiveClient(FlecketConfig("private-key", camera=camera))
    value.ws = Socket()
    return value


def audio(socket, rid="r", ms=40):
    pcm = b"\x01\x00" * (ms * 48)
    socket.emit(
        "response.output_audio.delta", response_id=rid, item_id="item-" + rid, delta=base64.b64encode(pcm).decode()
    )
    return pcm


@pytest.mark.parametrize("url", ["http://127.0.0.1:9110/", "ws://127.0.0.1:9110/v1", FLECKET_URL])
def test_normalize_url(url):
    assert realtime_url(url) == FLECKET_URL


def test_saved_agent_and_cloud_endpoint_are_preserved():
    assert realtime_url("https://voice.example/v1?model=agent_265823d9ffd34dd7") == (
        "wss://voice.example/v1/realtime?model=agent_265823d9ffd34dd7"
    )


@pytest.mark.parametrize("query", ["model=", "model=a&model=b", "model=a%0Ab", "ticket=secret", "model=a&preview=x"])
def test_reject_ambiguous_or_credential_query(query):
    with pytest.raises(ValueError):
        realtime_url("wss://voice.example/v1/realtime?" + query)


@pytest.mark.parametrize(
    "url", ["file:///tmp/a", "ws://key:secret@host", "ws://host?ticket=secret"]
)
def test_reject_invalid_endpoint(url):
    with pytest.raises(ValueError):
        realtime_url(url)


def test_handshake_audio_format_and_upload(monkeypatch):
    async def run():
        ws = Socket()
        ws.emit("session.created")

        async def connect(url, **kwargs):
            assert url == FLECKET_URL
            assert kwargs["additional_headers"] == {"Authorization": "Bearer private-key"}
            return ws

        monkeypatch.setattr(flecket_live, "connect", connect)
        value = FlecketLiveClient(FlecketConfig("private-key", voice="Tingting"))
        await value.start()
        settings = ws.sent[0]["session"]
        assert settings["output_modalities"] == ["audio"]
        assert settings["audio"]["input"]["format"]["rate"] == 16000
        assert settings["audio"]["output"]["format"]["rate"] == 48000
        assert settings["audio"]["output"]["voice"] == "Tingting"
        assert settings["audio"]["input"]["turn_detection"]["interrupt_response"] is False
        assert "instructions" not in settings and "tools" not in settings
        await value.send_audio(b"\x01\x00" * 320)
        assert base64.b64decode(ws.sent[-1]["audio"]) == b"\x01\x00" * 320
        assert ws.sent[-1]["type"] == "input_audio_buffer.append"
        with pytest.raises(ValueError):
            await value.send_audio(b"x")
        await value.close()
        assert ws.closed and not value.responses

    asyncio.run(run())


def test_generation_done_waits_for_dma_playback():
    async def run():
        value = client()
        ws = value.ws
        pcm = audio(ws)
        assert (await value.receive()).audio == pcm
        ws.emit("response.output_audio.done", response_id="r", item_id="item-r")
        assert (await value.receive()).action == "response.audio.done"
        ws.emit("response.done", response={"id": "r", "status": "completed"})
        ws.emit("conversation.item.input_audio_transcription.completed", transcript="你好")
        assert (await value.receive()).data["text"] == "你好"
        assert "r" in value.responses and not ws.sent
        await value.send({"action": "playback.progress", "run_id": "r", "played_audio_ms": 20})
        assert ws.sent == [{"type": "flecket.playback", "response_id": "r", "phase": "first_audio"}]
        await value.send({"action": "playback.completed", "run_id": "r"})
        assert ws.sent[-1]["phase"] == "completed" and not value.responses
        await value.close()

    asyncio.run(run())


def test_saved_agent_clock_tool_continues_once_after_generation():
    from deskbot_server.service.conversation_service import execute_agent_tool

    async def run():
        value = client()
        value.tool_handler = execute_agent_tool
        ws = value.ws
        response = {"id": "tool-response", "status": "completed", "output": [
            {"type": "function_call", "call_id": "clock", "name": "get_current_time", "arguments": "{}"},
            {"type": "function_call", "call_id": "unknown", "name": "run_shell", "arguments": "{}"},
        ]}
        ws.emit("response.done", response=response)
        ws.emit("response.done", response=response)  # Duplicate events must not rerun tools.
        ws.emit("conversation.item.input_audio_transcription.completed", transcript="几点")
        assert (await value.receive()).data["text"] == "几点"
        results = [event["item"] for event in ws.sent if event["type"] == "conversation.item.create"]
        assert len(results) == 2
        clock = json.loads(results[0]["output"])
        assert clock["local_time"].endswith("+08:00")
        assert "error" in json.loads(results[1]["output"])
        assert sum(event["type"] == "response.create" for event in ws.sent) == 1
        await value.close()

    asyncio.run(run())


@pytest.mark.parametrize("status,arguments,expected", [
    ("completed", "[]", True), ("completed", "{", True), ("cancelled", "{}", False),
])
def test_tool_invalid_arguments_and_cancelled_generation(status, arguments, expected):
    async def run():
        value = client()
        await value._complete_tools({"status": status, "output": [
            {"type": "function_call", "call_id": "clock", "name": "get_current_time", "arguments": arguments},
        ]})
        assert bool(value.ws.sent) is expected
        if expected:
            assert "error" in json.loads(value.ws.sent[0]["item"]["output"])
        await value.close()

    asyncio.run(run())


def test_vad_stops_generated_audio_and_truncates_cumulative_played_time():
    async def run():
        value = client(camera=True)
        ws = value.ws
        audio(ws, ms=160)
        await value.receive()
        await value.send({"action": "playback.progress", "run_id": "r", "played_audio_ms": 75})
        await value.send_camera(b"\xff\xd8old")
        await value.send_camera(b"\xff\xd8latest")
        ws.emit("response.done", response={"id": "r", "status": "completed"})
        ws.emit("input_audio_buffer.speech_started")
        event = await value.receive()
        assert event.action == "response.interrupted" and event.data["run_id"] == "r"
        assert next(x for x in ws.sent if x["type"] == "conversation.item.truncate")["audio_end_ms"] == 75
        image = next(x for x in ws.sent if x["type"] == "conversation.item.create")
        assert image["item"]["content"][0]["image_url"].endswith(base64.b64encode(b"\xff\xd8latest").decode())
        assert (await value.receive()).action == "input.speech.started"
        await value.send({"action": "playback.stopped", "run_id": "r", "played_audio_ms": 95})
        assert ws.sent[-2]["audio_end_ms"] == 95
        assert ws.sent[-1]["phase"] == "interrupted"
        audio(ws)  # Retired response audio must never reach the next playback.
        audio(ws, rid="new")
        assert (await value.receive()).data["run_id"] == "new"
        await value.close()

    asyncio.run(run())


def test_error_does_not_expose_upstream_key():
    async def run():
        value = client()
        value.ws.emit("error", error={"message": "private-key"})
        with pytest.raises(ClientError) as exc:
            await value.receive()
        assert "private-key" not in str(exc.value)
        await value.close()
        with pytest.raises(ClientError, match="FLECKET_API_KEY"):
            await FlecketLiveClient(FlecketConfig("")).start()

    asyncio.run(run())


def test_device_routes_to_independent_voice_bridge(monkeypatch):
    from deskbot_server.service import conversation_service, moss_live_service
    from deskbot_server.service.device_ws_service import DeviceWsService

    async def run():
        calls = []
        monkeypatch.setattr(conversation_service, "conversation_provider", lambda _: "flecket")

        async def handle(hub, websocket, device_id, **kwargs):
            calls.append((device_id, kwargs))

        monkeypatch.setattr(moss_live_service, "handle_moss_connection", handle)
        hub = DeviceWsService()
        await hub.handle_asr_chat(object(), "robot")
        assert calls == [("robot", {"provider": "flecket"})]

    asyncio.run(run())


@pytest.mark.parametrize("interrupt", [False, True])
def test_flecket_pcm_to_rom_opus_and_actual_playback_receipts(monkeypatch, interrupt):
    from deskbot_server.model.pb_seq import PbType
    from deskbot_server.service.moss_live_service import FlecketBridge
    from tests.test_moss_live import cleanup, setup, until

    async def run():
        value = client()
        ws = value.ws

        async def started():
            pass

        monkeypatch.setattr(value, "start", started)
        _, entry, bridge, _, sent, worker = await setup(monkeypatch, client_override=value, bridge_type=FlecketBridge)
        try:
            audio(ws)
            audio(ws)
            ws.emit("response.output_audio.done", response_id="r", item_id="item-r")
            ws.emit("response.done", response={"id": "r", "status": "completed"})
            await until(lambda: any(block.type == PbType.END for block, _ in sent))
            run = bridge.runs[("r", "r")]
            assert not any(x["type"] == "flecket.playback" for x in ws.sent)
            ack = {"req": run.req, "epoch": run.stream.seq.epoch, "ack_type": "pb_progress", "played_ms": 55}
            bridge.offer_ack(ack)
            await until(lambda: any(x.get("phase") == "first_audio" for x in ws.sent))
            if interrupt:
                ws.emit("input_audio_buffer.speech_started")
                await until(lambda: any(x.get("phase") == "interrupted" for x in ws.sent))
                assert any(block.type == PbType.CANCEL for block, _ in sent)
                assert [x["audio_end_ms"] for x in ws.sent if x["type"] == "conversation.item.truncate"] == [55, 55]
                assert not any(x.get("phase") == "completed" for x in ws.sent)
            else:
                end = next(block for block, _ in sent if block.type == PbType.END)
                entry.ack_queue.put_nowait({**ack, "idx": end.idx, "ack_type": "pb_end", "played_ms": 80})
                await until(lambda: any(x.get("phase") == "completed" for x in ws.sent))
            assert not value.responses
        finally:
            await cleanup(bridge, worker)
        assert ws.closed

    asyncio.run(run())


@pytest.mark.parametrize("allow", [False, True])
def test_microphone_echo_is_silenced_during_playback_and_tail(monkeypatch, allow):
    from dataclasses import replace
    now = [100.0]
    monkeypatch.setattr(flecket_live.time, "monotonic", lambda: now[0])

    async def run():
        value = client()
        value.config = replace(value.config, allow_barge_in=allow)
        ws = value.ws
        pcm = b"\x01\x00" * 320
        await value.send_audio(pcm)
        assert base64.b64decode(ws.sent[-1]["audio"]) == pcm
        audio(ws)
        await value.receive()
        await value.send_audio(pcm)
        assert base64.b64decode(ws.sent[-1]["audio"]) == (pcm if allow else bytes(640))
        await value.send({"action": "playback.completed", "run_id": "r"})
        await value.send_audio(pcm)
        assert base64.b64decode(ws.sent[-1]["audio"]) == (pcm if allow else bytes(640))
        now[0] += .5
        await value.send_audio(pcm)
        assert base64.b64decode(ws.sent[-1]["audio"]) == pcm
        await value.close()

    asyncio.run(run())


def test_long_phrase_burst_exceeding_old_five_second_buffer(monkeypatch):
    from deskbot_server.model.pb_seq import PbType
    from deskbot_server.service.moss_live_service import FlecketBridge
    from tests.test_moss_live import cleanup, pcm, setup, until

    async def run():
        _, entry, bridge, value, sent, worker = await setup(monkeypatch, bridge_type=FlecketBridge)
        ctx = {"run_id": "long", "turn_id": "long"}
        try:
            # TTS can deliver six seconds of PCM faster than the speaker plays.
            for i in range(3):
                value.emit("audio", {**ctx, "chunk_id": i}, pcm(2000))
            value.emit("response.audio.done", ctx)
            await until(lambda: any(block.type == PbType.END for block, _ in sent))
            assert not bridge.failed
            run = bridge.runs[("long", "long")]
            assert run.source_bytes == 6000 * 96
            end = next(block for block, _ in sent if block.type == PbType.END)
            entry.ack_queue.put_nowait({"req": run.req, "epoch": run.stream.seq.epoch,
                "idx": end.idx, "ack_type": "pb_end", "played_ms": 6000})
            await until(lambda: any(event["action"] == "playback.completed" for event in value.sent))
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())
