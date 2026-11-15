from __future__ import annotations

import asyncio
import json
import struct

import numpy as np
import opuslib_next
import pytest

from deskbot_server.client.moss_live import MossLiveClient
from deskbot_server.model.errors import ClientError
from deskbot_server.model.pb_seq import PbType
from deskbot_server.model.realtime import MossLiveConfig, RealtimeEvent
from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry
from deskbot_server.service.moss_live_service import MossLiveBridge
from deskbot_server.utils.streaming_pcm import Pcm48To16


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def pcm(ms=40):
    return (np.sin(2 * np.pi * 440 * np.arange(ms * 48) / 48000) * 10000).astype("<i2").tobytes()


class FakeClient:
    def __init__(self, camera=False):
        self.config = MossLiveConfig("test-key", camera=camera)
        self.incoming = asyncio.Queue()
        self.sent = []
        self.uploaded = []
        self.cameras = []
        self.closed = False

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    async def receive(self):
        return await self.incoming.get()

    async def send(self, value):
        self.sent.append(value)

    async def send_audio(self, value):
        self.uploaded.append(value)

    async def send_camera(self, value):
        self.cameras.append(value)

    def emit(self, action, data=None, audio=b""):
        self.incoming.put_nowait(RealtimeEvent(action, data or {}, audio))


class FakeRom:
    def __init__(self):
        self.closed = False

    async def close(self, **kw):
        self.closed = True


async def setup(monkeypatch, camera=False, prefill_ms=0, *, client_override=None, bridge_type=MossLiveBridge):
    DeviceWsService.reset_instance()
    hub = DeviceWsService()
    rom = FakeRom()
    entry = _DeviceEntry(device_id="robot", ws=rom, generation=1, capabilities={"pb_stream_v1"})
    hub._devices["robot"] = entry
    sent = []
    client = client_override or FakeClient(camera)
    bridge = bridge_type(hub, "robot", rom, client=client, prefill_ms=prefill_ms)
    entry.conversation_session = bridge

    async def send(dev, block, **kw):
        sent.append((block, kw))
        ack = {"type": "pb_ack", "req": block.req, "epoch": block.epoch, "idx": block.idx}
        if block.type == PbType.CANCEL:
            bridge.offer_ack({**ack, "ack_type": "pb_cancelled"})
        elif block.type != PbType.END:
            entry.ack_queue.put_nowait({**ack, "ack_type": "pb_credit"})
        return True

    monkeypatch.setattr(hub, "_do_send_to_device", send)
    worker = asyncio.create_task(hub._device_loop("robot", 1))
    await bridge.start()
    return hub, entry, bridge, client, sent, worker


async def cleanup(bridge, worker):
    await bridge.close()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


def test_receipts_follow_dma_not_credit_and_epoch_is_checked(monkeypatch):
    async def run():
        _, entry, bridge, client, sent, worker = await setup(monkeypatch)
        ctx = {"run_id": "r", "turn_id": "t"}
        try:
            for i in (1, 2):
                client.emit("audio", {**ctx, "chunk_id": i}, pcm())
            client.emit("response.audio.done", ctx)
            await until(lambda: any(b.type == PbType.END for b, _ in sent))
            r = bridge.runs[("r", "t")]
            assert not client.sent
            assert all(not b.anim and not b.servo and not kw.get("voice_key") for b, kw in sent)
            from deskbot_server.service.pipeline.opus_downlink import decode_opus_batch_to_pcm_s16le

            decoder = opuslib_next.Decoder(16000, 1)
            decoded = b"".join(
                decode_opus_batch_to_pcm_s16le(decoder, b.binaries[0], sample_rate=16000, opus_frames=b.audio.frames)
                for b, _ in sent
                if b.audio
            )
            assert len(decoded) == 80 * 32
            ack = {"req": r.req, "epoch": r.stream.seq.epoch, "idx": 0, "ack_type": "pb_progress"}
            bridge.offer_ack({**ack, "played_ms": 45})
            await until(lambda: len(client.sent) == 1)
            assert client.sent[0] == {"action": "playback.chunk.completed", **ctx, "chunk_id": 1}
            bridge.offer_ack({**ack, "epoch": 999, "played_ms": 200})
            await asyncio.sleep(0.01)
            assert len(client.sent) == 1
            bridge.offer_ack({**ack, "played_ms": 80})
            await until(lambda: len(client.sent) == 2)
            assert not any(x["action"] == "playback.completed" for x in client.sent)
            end = next(b for b, _ in sent if b.type == PbType.END)
            entry.ack_queue.put_nowait({**ack, "idx": end.idx, "ack_type": "pb_end"})
            await until(lambda: client.sent[-1]["action"] == "playback.completed")
            assert len(client.sent) == 3 and not bridge.runs
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


def test_interrupt_late_audio_and_next_run(monkeypatch):
    async def run():
        _, entry, bridge, client, sent, worker = await setup(monkeypatch)
        ctx = {"run_id": "old", "turn_id": "t"}
        try:
            client.emit("audio", {**ctx, "chunk_id": "one"}, pcm(80))
            await until(lambda: bool(sent))
            old = bridge.runs[("old", "t")]
            bridge.offer_ack(
                {"req": old.req, "epoch": old.stream.seq.epoch, "ack_type": "pb_progress", "played_ms": 35}
            )
            await until(lambda: old.played_ms == 35)
            client.emit("input.speech.started")
            await asyncio.sleep(0.02)
            assert not old.stopped and not any(b.type == PbType.CANCEL for b, _ in sent)
            client.emit("response.audio.stop", ctx)
            await until(lambda: any(x["action"] == "playback.stopped" for x in client.sent))
            assert next(x for x in client.sent if x["action"] == "playback.stopped")["played_audio_ms"] == 35
            assert sent[-1][0].type == PbType.CANCEL
            client.emit("audio", {**ctx, "chunk_id": "late"}, pcm())
            client.emit("response.audio.done", ctx)
            newctx = {"run_id": "new", "turn_id": "t2"}
            client.emit("audio", {**newctx, "chunk_id": "one"}, pcm())
            client.emit("response.audio.done", newctx)
            await until(lambda: any(b.type == PbType.END and b.req != old.req for b, _ in sent))
            newer = bridge.runs[("new", "t2")]
            assert newer.stream.seq.epoch > old.stream.seq.epoch
            assert sum(x["action"] == "playback.stopped" for x in client.sent) == 1
            assert not any(x["action"] == "playback.completed" and x["run_id"] == "old" for x in client.sent)
            end = next(b for b, _ in sent if b.type == PbType.END)
            entry.ack_queue.put_nowait(
                {"req": newer.req, "epoch": newer.stream.seq.epoch, "idx": end.idx, "ack_type": "pb_end"}
            )
            await until(lambda: client.sent[-1]["action"] == "playback.completed")
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


def test_uplink_opus_pcm_and_flush(monkeypatch):
    async def run():
        _, _, bridge, client, _, worker = await setup(monkeypatch)
        try:
            encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
            packets = [encoder.encode(b"\x00" * 640, 320) for _ in range(2)]
            bridge.offer_audio(
                {"codec": "opus", "sr": 16000, "ch": 1, "opus_frames": 2},
                b"".join(struct.pack("!H", len(p)) + p for p in packets),
            )
            for _ in range(2):
                bridge.offer_audio({"codec": "s16le", "sr": 16000, "ch": 1}, b"\x00" * 320)
            bridge.offer_audio({"flush": 1}, b"")
            await until(lambda: len(client.uploaded) == 3)
            assert all(len(x) == 640 for x in client.uploaded)
            with pytest.raises(ValueError):
                bridge.offer_audio({"sr": 48000}, b"1234")
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_camera_optional_latest_only(monkeypatch, enabled):
    async def run():
        _, _, bridge, client, _, worker = await setup(monkeypatch, enabled)
        try:
            bridge.offer_camera(b"\xff\xd8first")
            bridge.offer_camera(b"\xff\xd8latest")
            await asyncio.sleep(1.05)
            assert client.cameras == ([b"\xff\xd8latest"] if enabled else [])
            assert bridge.latest_camera is None
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


def test_bounded_failure_closes_session(monkeypatch):
    async def run():
        _, _, bridge, client, _, worker = await setup(monkeypatch)
        try:
            client.emit("audio", {"run_id": "r", "turn_id": "t", "chunk_id": 1}, b"\x00" * (512 * 1024 + 2))
            await until(lambda: bridge.websocket.closed)
            assert bridge.failed
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


def test_old_connection_cannot_enqueue_into_replacement(monkeypatch):
    async def run():
        _, entry, bridge, client, sent, worker = await setup(monkeypatch)
        try:
            entry.generation += 1
            client.emit("audio", {"run_id": "r", "turn_id": "t", "chunk_id": 1}, pcm())
            await asyncio.sleep(0.02)
            assert not sent and not entry.queue
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


def test_resampler_chunk_invariance_and_alias_rejection():
    samples = np.arange(48000)
    x = (np.sin(2 * np.pi * 1000 * samples / 48000) * 10000).astype("<i2").tobytes()
    a = Pcm48To16().feed(x)
    b = Pcm48To16()
    assert b"".join(b.feed(x[i : i + 74]) for i in range(0, len(x), 74)) == a
    assert len(a) == 32000
    high = (np.sin(2 * np.pi * 12000 * samples / 48000) * 10000).astype("<i2").tobytes()
    assert (
        np.std(np.frombuffer(Pcm48To16().feed(high), dtype="<i2")[100:])
        < np.std(np.frombuffer(a, dtype="<i2")[100:]) * 0.005
    )


def test_client_handshake_context_and_secret_boundary(monkeypatch):
    async def run():
        class Socket:
            def __init__(self):
                self.sent = []
                self.closed = False
                self.incoming = asyncio.Queue()
                for a in ["session_attached", "voice_changed", "session.updated"]:
                    self.incoming.put_nowait(json.dumps({"action": a, "data": {}}))

            async def send(self, v):
                self.sent.append(v)

            async def recv(self):
                return await self.incoming.get()

            async def close(self):
                self.closed = True

        ws = Socket()
        connection = {}

        async def connect(url, **kw):
            connection.update(url=url, **kw)
            return ws

        monkeypatch.setattr("deskbot_server.client.moss_live.connect", connect)
        client = MossLiveClient(MossLiveConfig("secret-not-in-payload"))
        await client.start()
        try:
            assert connection["additional_headers"]["Authorization"] == "Bearer secret-not-in-payload"
            assert "secret-not-in-payload" not in "".join(ws.sent)
            assert [json.loads(x)["action"] for x in ws.sent] == [
                "attach_session",
                "change_voice",
                "session.update",
                "media.mode",
                "mic_state",
                "video.state",
            ]
            ws.incoming.put_nowait(
                json.dumps({"action": "response.audio.context", "data": {"run_id": "r", "turn_id": "t", "chunk_id": 0}})
            )
            ws.incoming.put_nowait(pcm())
            assert (await client.receive()).action == "response.audio.context"
            assert (await client.receive()).audio == pcm()
            ws.incoming.put_nowait(pcm())
            with pytest.raises(ClientError):
                await client.receive()
        finally:
            await client.close()
        assert ws.closed

    asyncio.run(run())


def test_interrupt_is_received_while_downlink_producer_is_blocked(monkeypatch):
    async def run():
        hub, _, bridge, client, sent, worker = await setup(monkeypatch)
        original = hub._do_send_to_device

        async def stalled(dev, block, **kw):
            if block.type == PbType.CANCEL:
                return await original(dev, block, **kw)
            sent.append((block, kw))
            return True  # ROM gives no queue credits.

        monkeypatch.setattr(hub, "_do_send_to_device", stalled)
        try:
            ctx = {"run_id": "blocked", "turn_id": "t"}
            client.emit("audio", {**ctx, "chunk_id": 1}, pcm(3000))
            await until(lambda: len(sent) == 4)
            client.emit("response.audio.stop", ctx)
            await until(lambda: any(x["action"] == "playback.stopped" for x in client.sent))
            assert sent[-1][0].type == PbType.CANCEL
            assert not bridge.failed
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


def test_client_connection_failure_redacts_upstream_detail(monkeypatch):
    async def run():
        async def rejected(*a, **kw):
            raise RuntimeError("Authorization: Bearer sensitive-test-key")

        monkeypatch.setattr("deskbot_server.client.moss_live.connect", rejected)
        client = MossLiveClient(MossLiveConfig("sensitive-test-key"))
        with pytest.raises(ClientError) as exc:
            await client.start()
        assert "sensitive-test-key" not in str(exc.value)
        assert client.ws is None

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["moss-live", "flecket"])
@pytest.mark.parametrize("delayed_handshake", [False, True])
@pytest.mark.parametrize("binary", [False, True])
def test_moss_route_uses_packed_frames_without_local_pipeline(monkeypatch, delayed_handshake, binary, provider):
    from deskbot_server.dao import device_mapper, user_mapper
    from deskbot_server.dao.conversation_mapper import set_conversation
    from deskbot_server.pb.media_wire import ACK_HEADER, ACK_TYPES, AUDIO_HEADER, CAPABILITIES
    from deskbot_server.service.application.asr_chat_uplink import parse_packed_frame

    async def run():
        user_mapper.create("u", "packed-moss@example.com", "unused", False)
        device_mapper.insert("pk", "robot", "u", "robot")
        set_conversation("robot", provider, "{}")
        client = FakeClient()
        handshake_gate = asyncio.Event()
        if delayed_handshake:

            async def slow_start():
                await handshake_gate.wait()

            monkeypatch.setattr(client, "start", slow_start)
        monkeypatch.setattr("deskbot_server.service.moss_live_service.MossLiveClient", lambda cfg: client)
        monkeypatch.setattr("deskbot_server.client.flecket_live.FlecketLiveClient", lambda cfg, **kwargs: client)

        class Rom(FakeRom):
            def __init__(self):
                super().__init__()
                self.incoming = asyncio.Queue()
                self.sent = []

            audio_index = 0
            encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
            head = None
            binary_downlinks = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.incoming.get()
                if item is None:
                    raise StopAsyncIteration
                return item

            async def send(self, value):
                self.sent.append(value)
                if not isinstance(value, bytes):
                    return
                if value[:4] == b"DB\x01\x01":
                    _, _, _, epoch, idx, pts, duration, frames, n = AUDIO_HEADER.unpack_from(value)
                    assert self.head is not None and epoch == self.head["epoch"] and len(value) == 24+n
                    d = {**self.head, "type": "pb_chunk", "idx": idx, "pts_ms": pts, "chunk_ms": duration}
                    self.binary_downlinks += 1
                else:
                    frame = parse_packed_frame(value)
                    if not frame:
                        return
                    d = frame.doc
                    if d["type"] == "pb_start":
                        self.head = d
                ack = {"type": "pb_ack", "req": d["req"], "epoch": d["epoch"], "idx": d["idx"]}
                if d["type"] == "pb_end":
                    self.put({**ack, "ack_type": "pb_end"})
                elif d["type"] != "pb_cancel":
                    self.put({**ack, "ack_type": "pb_credit"})
                    self.put({**ack, "ack_type": "pb_progress", "played_ms": d.get("pts_ms", 0) + d.get("chunk_ms", 0)})

            def put(self, doc, media=b""):
                if binary and doc.get("type") == "audio":
                    encoded = self.encoder.encode(media, 320)
                    self.incoming.put_nowait(AUDIO_HEADER.pack(b"DB",1,2,0,self.audio_index,
                                             self.audio_index*20,20,1,len(encoded))+encoded)
                    self.audio_index += 1
                    return
                if binary and doc.get("type") == "pb_ack":
                    self.incoming.put_nowait(ACK_HEADER.pack(b"DB",1,3,doc["epoch"],doc["idx"],
                        doc.get("played_ms",0),0,1,10,ACK_TYPES.index(doc["ack_type"]),
                        int("played_ms" in doc),0))
                    return
                if media:
                    doc = {**doc, "next_bin_len": len(media)}
                raw = json.dumps(doc).encode()
                self.incoming.put_nowait(struct.pack("!I", len(raw)) + raw + media)

            async def close(self, **kw):
                self.closed = True
                self.incoming.put_nowait(None)

        rom = Rom()
        DeviceWsService.reset_instance()
        hub = DeviceWsService()
        handler = asyncio.create_task(hub.handle_asr_chat(rom, "robot"))
        try:
            rom.put(
                {
                    "type": "device_hello",
                    "capabilities": ["pb_stream_v1", "pb_playback_progress_v1", *(CAPABILITIES if binary else [])],
                    "anim_version": "test",
                }
            )
            await until(lambda: hub._devices.get("robot") and hub._devices["robot"].conversation_session is not None)
            bridge = hub._devices["robot"].conversation_session
            assert bridge.provider == provider
            if delayed_handshake:
                for _ in range(120):
                    rom.put({"type": "audio", "codec": "s16le", "sr": 16000, "ch": 1}, b"\x00" * 640)
                await until(lambda: bridge.startup_audio_skipped == 120)
                assert not rom.closed and not client.uploaded
                handshake_gate.set()
            await until(lambda: bridge.ready)
            # Buffered ROM messages must yield to the uploader even when every
            # socket read/touch operation completes without suspension.
            for _ in range(120):
                rom.put({"type": "audio", "codec": "s16le", "sr": 16000, "ch": 1}, b"\x00" * 640)
            await until(lambda: len(client.uploaded) == 120)
            assert not rom.closed and bridge.audio_queue.empty()
            client.emit("audio", {"run_id": "r", "turn_id": "t", "chunk_id": 1}, pcm(240))
            client.emit("response.audio.done", {"run_id": "r", "turn_id": "t"})
            await until(lambda: any(x["action"] == "playback.completed" for x in client.sent))
            assert (rom.binary_downlinks > 0) == binary
            assert hub._pipeline is None  # No local ASR/LLM/TTS dependency.
            assert any(isinstance(x, bytes) for x in rom.sent)
        finally:
            await rom.close()
            await asyncio.wait_for(handler, 3)
            await hub.shutdown()
        assert client.closed

    asyncio.run(run())


def test_stop_progress_is_partial_current_chunk_not_response_total(monkeypatch):
    async def run():
        _, _, bridge, client, sent, worker = await setup(monkeypatch)
        ctx = {"run_id": "partial", "turn_id": "t"}
        try:
            for i in range(3):
                client.emit("audio", {**ctx, "chunk_id": str(i)}, pcm(100))
            await until(lambda: len(sent) >= 3)
            r = bridge.runs[("partial", "t")]
            bridge.offer_ack({"req": r.req, "epoch": r.stream.seq.epoch, "ack_type": "pb_progress", "played_ms": 235})
            await until(lambda: len(client.sent) == 2)
            client.emit("response.audio.stop", ctx)
            await until(lambda: any(x["action"] == "playback.stopped" for x in client.sent))
            assert client.sent[-1]["played_audio_ms"] == 35
            assert [x["chunk_id"] for x in client.sent if x["action"] == "playback.chunk.completed"] == ["0", "1"]
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


def test_jitter_prefill_and_fixed_blocks_preserve_short_reply(monkeypatch):
    async def run():
        _, entry, bridge, client, sent, worker = await setup(monkeypatch, prefill_ms=480)
        ctx = {"run_id": "prefill", "turn_id": "t"}
        try:
            for i in range(4):
                client.emit("audio", {**ctx, "chunk_id": str(i)}, pcm(100))
            await until(
                lambda: bridge.runs.get(("prefill", "t")) and bridge.runs[("prefill", "t")].source_bytes == 400 * 96
            )
            assert not sent
            client.emit("audio", {**ctx, "chunk_id": "4"}, pcm(100))
            await until(lambda: len(sent) >= 6)
            assert all(b.chunk_ms == 80 for b, _ in sent)
            client.emit("response.audio.done", ctx)
            await until(lambda: any(b.type == PbType.END for b, _ in sent))
            end = next(b for b, _ in sent if b.type == PbType.END)
            assert sum(b.chunk_ms for b, _ in sent) == 500
            r = bridge.runs[("prefill", "t")]
            entry.ack_queue.put_nowait(
                {"req": r.req, "epoch": r.stream.seq.epoch, "idx": end.idx, "ack_type": "pb_end"}
            )
            await until(lambda: not bridge.runs)
            # A short reply flushes as soon as done; it never waits to fill 480ms.
            sent.clear()
            short = {"run_id": "short", "turn_id": "t2"}
            client.emit("audio", {**short, "chunk_id": "0"}, pcm(100))
            client.emit("response.audio.done", short)
            await until(lambda: any(b.type == PbType.END for b, _ in sent))
            assert [b.chunk_ms for b, _ in sent] == [80, 20, 0]
        finally:
            await cleanup(bridge, worker)

    asyncio.run(run())


def test_slow_uplink_retains_fresh_pcm_without_resetting_connection(monkeypatch):
    async def run():
        _, _, bridge, client, _, worker = await setup(monkeypatch)
        blocked, release = asyncio.Event(), asyncio.Event()

        async def send_audio(value):
            blocked.set()
            await release.wait()
            client.uploaded.append(value)

        monkeypatch.setattr(client, "send_audio", send_audio)
        try:
            bridge.offer_audio({"codec": "s16le"}, bytes(640))
            await blocked.wait()
            for i in range(200):
                bridge.offer_audio({"codec": "s16le"}, struct.pack("<h", i + 1) * 320)
            assert bridge.audio_queue.qsize() == 40
            assert bridge.uplink_dropped_frames == 160
            assert not bridge.failed and not bridge.websocket.closed
            release.set()
            await until(lambda: len(client.uploaded) == 41)
            assert struct.unpack_from("<h", client.uploaded[1])[0] == 161
            assert struct.unpack_from("<h", client.uploaded[-1])[0] == 200
        finally:
            release.set()
            await cleanup(bridge, worker)

    asyncio.run(run())
