"""Persistent MOSS/Flecket voice bridges with bounded queues and device DMA receipts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

import opuslib_next

from deskbot_server.client.moss_live import MossLiveClient
from deskbot_server.model.pb_seq import PlaybackStatus
from deskbot_server.model.realtime import media_identity
from deskbot_server.pb.media_wire import CAPABILITIES as MEDIA_CAPABILITIES
from deskbot_server.service.application.uplink_preview import split_uplink_preview
from deskbot_server.service.conversation_service import moss_config
from deskbot_server.service.pipeline.opus_downlink import decode_opus_batch_to_pcm_s16le
from deskbot_server.utils.streaming_pcm import Pcm48To16

logger = logging.getLogger("deskbot-server")


@dataclass
class _Run:
    identity: tuple[str, str]
    req: str = field(default_factory=lambda: uuid.uuid4().hex)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=16))
    chunks: deque = field(default_factory=deque)  # (chunk_id, source end in ms)
    chunk_ids: set = field(default_factory=set)
    queued_bytes: int = 0
    source_bytes: int = 0
    played_ms: int = 0
    confirmed_chunk_end_ms: int = 0
    stopped: bool = False
    done: bool = False
    completed: bool = False
    stopped_sent: bool = False
    cancel_ack: asyncio.Event = field(default_factory=asyncio.Event)
    stream: object = None
    producer: asyncio.Task | None = None

    def context(self):
        return {"run_id": self.identity[0], "turn_id": self.identity[1]}


class MossLiveBridge:
    provider = "moss-live"

    def __init__(self, hub, device_id: str, websocket, *, client=None, prefill_ms=480):
        self.hub, self.device_id, self.websocket = hub, device_id, websocket
        self.client = client or MossLiveClient(moss_config(device_id))
        self.runs: dict[tuple[str, str], _Run] = {}
        self.retired = deque(maxlen=128)
        self.tasks: set[asyncio.Task] = set()
        self.audio_queue = asyncio.Queue(maxsize=40)  # 800ms, each entry is exactly 20ms PCM.
        self.uplink_pending = bytearray()
        self.uplink_dropped_frames = 0
        self.last_uplink_drop_log = 0.0
        self.prefill_bytes = max(0, min(int(prefill_ms), 1000)) * 32
        self.ack_queue = asyncio.Queue(maxsize=128)
        self.transcript_queue = asyncio.Queue(maxsize=32)
        self.latest_camera = None
        self.closed = False
        self.failed = False
        self.ready = False
        self.startup_audio_skipped = 0
        self.uploaded_frames = 0
        self.received_audio_chunks = 0
        self.last_response_event = {}
        self.generation = hub._devices[device_id].generation
        self.decoder = opuslib_next.Decoder(16000, 1)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception() is not None and not self.closed and not self.failed:
                self.failed = True
                # Upstream errors may contain credentials. Never log raw errors.
                logger.warning(
                    "[%s] session failed device_id=%s task=%s kind=%s code=%s",
                    self.provider,
                    self.device_id,
                    t.get_coro().__name__,
                    type(t.exception()).__name__,
                    getattr(t.exception(), "code", ""),
                )
                self._spawn(self.websocket.close(code=1011, reason=f"{self.provider} session ended"))

        task.add_done_callback(done)
        return task

    async def start(self):
        await self.client.start()
        entry = self.hub._devices.get(self.device_id)
        if self.closed or entry is None or entry.generation != self.generation or entry.ws is not self.websocket:
            await self.client.close()
            return
        self._spawn(self._receive())
        self._spawn(self._upload())
        self._spawn(self._receipts())
        self._spawn(self._camera_and_ping())
        self._spawn(self._transcripts())
        self.ready = True
        logger.info(
            "[%s] ready device_id=%s camera=%s startup_audio_skipped=%d",
            self.provider,
            self.device_id,
            self.client.config.camera,
            self.startup_audio_skipped,
        )

    def begin(self):
        # Never hold the ROM receive loop during the multi-second handshake.
        # Its live microphone is already running as soon as ready is sent.
        self._spawn(self.start())

    def offer_audio(self, data: dict, media: bytes):
        if not media:
            return  # ROM VAD flush does not terminate a persistent MOSS session.
        if not self.ready:
            self.startup_audio_skipped += 1
            return  # Keep startup latency bounded, rather than replay stale speech.
        codec = data.get("codec") or "opus"
        if data.get("sr", 16000) != 16000 or data.get("ch", 1) != 1 or codec not in {"opus", "s16le", "pcm"}:
            raise ValueError("MOSS bridge expects mono 16kHz ROM audio")
        count = data.get("opus_frames") or data.get("frames") or data.get("n_frames") or 1
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 50 or len(media) > 65536:
            raise ValueError("invalid ROM audio batch")
        compressed, _ = split_uplink_preview(data, media)
        # Decode every packet even when the network is slow: dropping compressed
        # packets would corrupt the state of the shared Opus decoder.
        pcm = (
            decode_opus_batch_to_pcm_s16le(self.decoder, compressed, sample_rate=16000, opus_frames=count)
            if codec == "opus"
            else compressed
        )
        if len(pcm) % 2:
            raise ValueError("unaligned uplink PCM")
        self.uplink_pending.extend(pcm)
        while len(self.uplink_pending) >= 640:
            frame = bytes(self.uplink_pending[:640])
            del self.uplink_pending[:640]
            if self.audio_queue.full():
                self.audio_queue.get_nowait()
                self.uplink_dropped_frames += 1
                now = time.monotonic()
                if now - self.last_uplink_drop_log >= 5:
                    self.last_uplink_drop_log = now
                    logger.warning(
                        "[%s] stale uplink dropped device_id=%s frames=%d",
                        self.provider,
                        self.device_id,
                        self.uplink_dropped_frames,
                    )
            self.audio_queue.put_nowait(frame)

    def offer_camera(self, jpeg: bytes):
        if self.client.config.camera and len(jpeg) <= 512 * 1024 and jpeg.startswith(b"\xff\xd8"):
            self.latest_camera = jpeg  # One latest frame, never a growing video queue.

    def offer_ack(self, ack: dict):
        if ack.get("ack_type") in {"pb_progress", "pb_audio_end", "pb_end", "pb_cancelled", "pb_error"} and any(
            run.stream and ack.get("req") == run.req and ack.get("epoch") == run.stream.seq.epoch
            for run in self.runs.values()
        ):
            self.ack_queue.put_nowait(dict(ack))

    async def _upload(self):
        while True:
            frame = await self.audio_queue.get()
            await self.client.send_audio(frame)
            self.uploaded_frames += 1
            if self.uploaded_frames == 1:
                logger.info("[%s] audio uplink started device_id=%s", self.provider, self.device_id)

    async def _camera_and_ping(self):
        from deskbot_server.dao.device_mapper import get_auto_reply

        last_ping = time.monotonic()
        while True:
            await asyncio.sleep(1)
            if not get_auto_reply(self.device_id):
                await self.client.send({"action": "mic_state", "enabled": False})
                await self.websocket.close(code=1000, reason="Automatic reply disabled")
                return
            frame, self.latest_camera = self.latest_camera, None
            if frame is not None:
                await self.client.send_camera(frame)
            if time.monotonic() - last_ping >= 20:
                await self.client.send({"action": "ping", "timestamp": int(time.time() * 1000)})
                last_ping = time.monotonic()

    def _run(self, data):
        key = media_identity(data)
        if key in self.retired:
            return None
        if key not in self.runs:
            if len(self.runs) >= 8:
                raise ValueError("too many pending MOSS responses")
            run = self.runs[key] = _Run(key, queue=asyncio.Queue(maxsize=768 if self.provider == "flecket" else 16))
            run.producer = self._spawn(self._play(run))
        return self.runs[key]

    def _note_control(self, action, data, run=None):
        # Keep enough protocol evidence to distinguish a model ending its turn
        # from playback cancellation or connection loss. Never log transcripts
        # or arbitrary upstream messages, which may contain user data/secrets.
        def label(value):
            value = str(value or "")
            secret = self.client.config.api_key
            if secret:
                value = value.replace(secret, "redacted")
            return re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:80]

        event = {
            "event": action,
            "status": label(data.get("status")),
            "reason": label(data.get("reason")),
            "req": run.req if run else "",
            "source_ms": (run.source_bytes + 95) // 96 if run else 0,
            "played_ms": run.played_ms if run else 0,
        }
        self.last_response_event = event
        logger.info(
            "[%s] response control device_id=%s event=%s status=%s reason=%s req=%s source_ms=%d played_ms=%d",
            self.provider,
            self.device_id,
            event["event"],
            event["status"],
            event["reason"],
            event["req"],
            event["source_ms"],
            event["played_ms"],
        )

    async def _receive(self):
        while True:
            event = await self.client.receive()
            action, data = event.action, event.data
            if action == "audio":
                self.received_audio_chunks += 1
                if self.received_audio_chunks == 1:
                    logger.info("[%s] audio downlink started device_id=%s", self.provider, self.device_id)
                run = self._run(data)
                if run is None or run.stopped:
                    continue
                if run.done:
                    raise ValueError("audio after response.audio.done")
                if run.queued_bytes + len(event.audio) > (2 * 1024 * 1024 if self.provider == "flecket" else 512 * 1024):
                    raise ValueError("realtime_downlink_buffer_limit")
                run.queue.put_nowait((dict(data), event.audio))
                run.queued_bytes += len(event.audio)
            elif action == "response.audio.done":
                run = self._run(data)
                self._note_control(action, data, run)
                if run and not run.stopped and not run.done:
                    run.done = True
                    run.queue.put_nowait(None)
            elif action in {"response.audio.stop", "response.interrupted"}:
                # A run-only interruption applies to every turn belonging to it.
                for run in list(self.runs.values()):
                    if run.identity[0] == data.get("run_id") and (
                        not data.get("turn_id") or run.identity[1] == data["turn_id"]
                    ):
                        self._note_control(action, data, run)
                        logger.info(
                            "[%s] upstream stop device_id=%s event=%s req=%s played_ms=%d",
                            self.provider,
                            self.device_id,
                            action,
                            run.req,
                            run.played_ms,
                        )
                        self._stop(run)
            elif action == "input.speech.started":
                # Activity is not an interruption decision. MOSS explicitly
                # requires response.audio.stop / response.interrupted for that.
                logger.info("[%s] input activity device_id=%s playing=%s", self.provider, self.device_id, bool(self.runs))
                self.hub.note_convo(self.device_id)
            elif action == "response.ended":
                self._note_control(action, data)
                for run in list(self.runs.values()):
                    if run.identity[0] == data.get("run_id") and data.get("status") == "failed":
                        self._stop(run)
            if action.startswith(("input.transcript.", "output.transcript.")):
                if self.transcript_queue.full():
                    self.transcript_queue.get_nowait()
                self.transcript_queue.put_nowait(
                    {"type": "realtime_transcript", "provider": self.provider, "action": action, "data": data}
                )

    async def _transcripts(self):
        while True:
            event = await self.transcript_queue.get()
            bus = self.hub.bus_service
            if bus:
                # Slow admin viewers must not delay an upstream interruption.
                await bus.broadcast(self.device_id, event)

    async def _play(self, run: _Run):
        resampler, pcm = Pcm48To16(), bytearray()
        prefilled = self.prefill_bytes == 0
        encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
        encoder.bitrate = 24000

        async def append(block: bytes):
            entry = self.hub._devices.get(self.device_id)
            if run.stopped or entry is None or entry.generation != self.generation or entry.ws is not self.websocket:
                raise asyncio.CancelledError
            if run.stream is None:
                # Deliberately omit voice_key: MOSS voices have no calibrated
                # template yet, so the ROM uses its existing RMS fallback.
                run.stream = await self.hub.open_speech_stream(self.device_id, run.req, sr=16000, fmt="opus", level=1)
                if run.stream is None:
                    raise RuntimeError("ROM lacks pb_stream_v1")
            bins = []
            for offset in range(0, len(block), 640):
                frame = block[offset : offset + 640].ljust(640, b"\x00")
                packet = encoder.encode(frame, 320)
                bins.append(struct.pack("!H", len(packet)) + packet)
            audio = b"".join(bins)
            await run.stream.append(
                [
                    (
                        {
                            "type": "pb_chunk",
                            "req": run.req,
                            "idx": 0,
                            "chunk_ms": len(bins) * 20,
                            "audio": {"next_bin_len": len(audio), "frames": len(bins)},
                        },
                        [audio],
                    )
                ]
            )

        while True:
            item = await run.queue.get()
            if item is None:
                break
            context, chunk = item
            run.queued_bytes -= len(chunk)
            chunk_id = context["chunk_id"]
            if chunk_id in run.chunk_ids or len(run.chunks) >= (1024 if self.provider == "flecket" else 256) or len(run.chunk_ids) >= 16384:
                raise ValueError("duplicate or excessive audio chunks")
            run.chunk_ids.add(chunk_id)
            run.source_bytes += len(chunk)
            run.chunks.append((chunk_id, (run.source_bytes + 95) // 96))
            pcm.extend(resampler.feed(chunk))
            if not prefilled:
                if len(pcm) < self.prefill_bytes:
                    continue
                prefilled = True
            # Keep Opus state continuous across arbitrary upstream chunks;
            # never zero-pad an intermediate chunk or wait for a whole response.
            while len(pcm) >= 2560:
                block = bytes(pcm[:2560])
                del pcm[:2560]
                await append(block)
        if pcm:
            while pcm:
                block = bytes(pcm[:2560])
                del pcm[:2560]
                await append(block)
        if run.stream:
            outcome = await run.stream.finish()
            if outcome.status != PlaybackStatus.COMPLETED:
                if not run.stopped:
                    self._stop(run)
                return
        if not run.stopped:
            await self._advance(run, (run.source_bytes + 95) // 96)
            await self.client.send({"action": "playback.completed", **run.context()})
            run.completed = True
            self._retire(run)

    async def _advance(self, run, played_ms):
        run.played_ms = max(run.played_ms, min(played_ms, (run.source_bytes + 95) // 96))
        while run.chunks and run.chunks[0][1] <= run.played_ms and not run.stopped_sent:
            chunk_id, end_ms = run.chunks.popleft()
            await self.client.send({"action": "playback.chunk.completed", **run.context(), "chunk_id": chunk_id})
            run.confirmed_chunk_end_ms = end_ms

    async def _receipts(self):
        while True:
            ack = await self.ack_queue.get()
            for run in list(self.runs.values()):
                if not run.stream or ack.get("req") != run.req or ack.get("epoch") != run.stream.seq.epoch:
                    continue
                if isinstance(ack.get("played_ms"), int):
                    await self._advance(run, max(0, ack["played_ms"]))
                    if self.provider == "flecket":
                        await self.client.send({"action": "playback.progress", **run.context(),
                                                "played_audio_ms": run.played_ms})
                if ack.get("ack_type") == "pb_cancelled":
                    run.cancel_ack.set()
                elif ack.get("ack_type") == "pb_error":
                    raise RuntimeError("ROM rejected MOSS playback")

    def _stop(self, run):
        if run.stopped or run.completed:
            return
        run.stopped = True  # Gate audio immediately, before waiting for cancellation.
        if run.producer and not run.producer.done():
            run.producer.cancel()
        self._spawn(self._cancel_and_report(run))

    async def _cancel_and_report(self, run):
        if run.stream:
            entry = self.hub._devices.get(self.device_id)
            was_sending = entry is not None and entry.sending_seq is run.stream.seq
            affected = await self.hub.interrupt_current_playback(
                self.device_id, reason=f"{self.provider}_interrupted", expected_req=run.req
            )
            if affected and was_sending:
                # pb_credit is acceptance, never proof of playback or stop.
                async with asyncio.timeout(3):
                    await run.cancel_ack.wait()
        if not self.closed and not run.stopped_sent:
            await self._advance(run, run.played_ms)
            # MOSS expects progress within the first unfinished chunk, not the
            # cumulative response time (completed chunks were acknowledged).
            if self.provider == "flecket":
                partial_ms = run.played_ms
            else:
                partial_ms = max(0, run.played_ms - run.confirmed_chunk_end_ms) if run.chunks else 0
            await self.client.send({"action": "playback.stopped", **run.context(), "played_audio_ms": partial_ms})
            run.stopped_sent = True
        self._retire(run)

    def _retire(self, run):
        self.retired.append(run.identity)
        self.runs.pop(run.identity, None)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        for run in list(self.runs.values()):
            if run.stream:
                await self.hub.interrupt_current_playback(
                    self.device_id, reason=f"{self.provider}_disconnected", expected_req=run.req
                )
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(1):
                        await run.stream.seq._done.wait()
        await self.client.close()
        logger.info(
            "[%s] closed device_id=%s uploaded_frames=%d received_audio_chunks=%d startup_audio_skipped=%d",
            self.provider,
            self.device_id,
            self.uploaded_frames,
            self.received_audio_chunks,
            self.startup_audio_skipped,
        )
        self.runs.clear()
        self.latest_camera = None


class FlecketBridge(MossLiveBridge):
    provider = "flecket"

    def __init__(self, hub, device_id, websocket, *, client=None, prefill_ms=320):
        from deskbot_server.client.flecket_live import FlecketLiveClient
        from deskbot_server.service.conversation_service import execute_agent_tool, flecket_config

        super().__init__(hub, device_id, websocket,
                         client=client or FlecketLiveClient(flecket_config(device_id), tool_handler=execute_agent_tool), prefill_ms=prefill_ms)


async def handle_moss_connection(hub, websocket, device_id, *, provider="moss-live"):

    """ROM receive loop stays independent of upstream generation and playback."""
    from deskbot_server.dao.device_mapper import get_auto_reply
    from deskbot_server.utils.util import _normalize_incoming_pb_ack
    from deskbot_server.utils.ws_utils import WsUtils
    from deskbot_server.ws.uplink_rate_stats import ensure_uplink_rate_stats_started, note_uplink_audio, remove_device

    ensure_uplink_rate_stats_started()
    await hub.register(device_id, websocket)
    entry = hub._devices[device_id]
    generation = entry.generation
    entry.conversation_provider = provider
    bridge_type = FlecketBridge if provider == "flecket" else MossLiveBridge
    bridge = None
    last_enable_check = 0.0
    try:
        await WsUtils.safe_send(websocket, json.dumps({"type": "ready", "capabilities": MEDIA_CAPABILITIES}))
        async for message in websocket:
            if entry.ws is not websocket or entry.generation != generation:
                break
            if not isinstance(message, (bytes, bytearray)):
                continue
            frame = entry.media_wire.uplink(bytes(message))
            if frame is None:
                continue
            data, media = frame.doc, frame.bin
            kind = data.get("type")
            if kind == "device_hello":
                caps = data.get("capabilities") or []
                entry.capabilities = {c for c in caps if isinstance(c, str)} if isinstance(caps, list) else set()
                entry.media_wire.capabilities = entry.capabilities.intersection(MEDIA_CAPABILITIES)
                entry.anim_version = str(data.get("anim_version") or "")
                if "pb_stream_v1" not in entry.capabilities or "pb_playback_progress_v1" not in entry.capabilities:
                    raise ValueError(f"{provider} requires stream/progress capable ROM")
                if bridge is None and get_auto_reply(device_id):
                    bridge = bridge_type(hub, device_id, websocket)
                    entry.conversation_session = bridge
                    bridge.begin()
            elif kind == "audio":
                if (
                    bridge is None
                    and "pb_playback_progress_v1" in entry.capabilities
                    and time.monotonic() - last_enable_check >= 1
                ):
                    last_enable_check = time.monotonic()
                    if get_auto_reply(device_id):
                        bridge = bridge_type(hub, device_id, websocket)
                        entry.conversation_session = bridge
                        bridge.begin()
                if bridge:
                    note_uplink_audio(device_id)
                    bridge.offer_audio(data, media)
            elif kind == "camera_frame" and media:
                hub._accept_camera_frame(device_id, media, source="asr_chat")
            elif kind == "pb_ack":
                ack = _normalize_incoming_pb_ack(data)
                if ack:
                    await hub.ack(device_id, ack)
                    await hub.record_pb_ack(device_id, ack)
            await hub.touch(device_id)
            # An already buffered WebSocket iterator, touch(), and put_nowait()
            # can all finish synchronously. Let media workers drain each burst.
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[%s] connection ended device_id=%s kind=%s", provider, device_id, type(exc).__name__)
    finally:
        if bridge:
            await bridge.close()
        if entry.conversation_session is bridge:
            entry.conversation_session = None
        if entry.generation == generation:
            remove_device(device_id)
        await hub.unregister(device_id, websocket)
        with contextlib.suppress(Exception):
            await websocket.close()
