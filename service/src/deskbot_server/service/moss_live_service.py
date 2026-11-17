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
from deskbot_server.model.pb_seq import PbAction, PbBlock, PbSeq, PlaybackOutcome, PlaybackStatus
from deskbot_server.model.realtime import media_identity
from deskbot_server.model.robot_performance import RobotPerformance
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
    performance: RobotPerformance | None = None
    output_ms: int = 0
    performance_attached: bool = False
    media_plan: dict | None = None
    announcement: str | None = None

    def context(self):
        return {"run_id": self.identity[0], "turn_id": self.identity[1]}


class MossLiveBridge:
    provider = "moss-live"

    def __init__(self, hub, device_id: str, websocket, *, client=None, prefill_ms=480):
        self.hub, self.device_id, self.websocket = hub, device_id, websocket
        from deskbot_server.service.application.realtime_voiceprint import RealtimeVoiceprint
        self.voiceprint = RealtimeVoiceprint(device_id)
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
            self.voiceprint.feed(frame, playback=(bool(self.runs) or getattr(self.client, "external_audio_active", False)
                                                 or time.monotonic() < getattr(self.client, "_mute_until", 0)))
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
            self._configure_run(run)
            run.producer = self._spawn(self._play(run))
        return self.runs[key]

    def _configure_run(self, run):
        pass

    async def _on_event(self, action, data):
        pass

    async def _before_audio(self, run):
        pass

    async def _after_audio(self, run):
        pass

    def _audio_tracks(self, run):
        return {}

    def _minimum_audio_ms(self, run):
        return 0

    def _playback_finished(self, run, completed):
        pass

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
            await self._on_event(action, data)
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
                await self._before_audio(run)
                if run.stopped:
                    raise asyncio.CancelledError
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
                            **self._audio_tracks(run),
                        },
                        [audio],
                    )
                ]
            )
            run.output_ms += len(bins) * 20

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
        # Keep the ROM's audio clock running until a concurrent performance
        # finishes. This tail is silence, never additional generated speech.
        while run.stream and run.output_ms < self._minimum_audio_ms(run):
            await append(bytes(min(80, self._minimum_audio_ms(run) - run.output_ms) * 32))
        if run.stream:
            outcome = await run.stream.finish()
            if outcome.status != PlaybackStatus.COMPLETED:
                if not run.stopped:
                    self._stop(run)
                return
        if not run.stopped:
            await self._after_audio(run)
            await self._advance(run, (run.source_bytes + 95) // 96)
            await self.client.send({"action": "playback.completed", **run.context()})
            run.completed = True
            self._playback_finished(run, True)
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
        self._playback_finished(run, False)
        self._retire(run)

    def _retire(self, run):
        self.retired.append(run.identity)
        self.runs.pop(run.identity, None)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.voiceprint.close()
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
        from deskbot_server.service.agent_tools import AgentTools
        from deskbot_server.service.application.device_media import DeviceMedia
        from deskbot_server.service.conversation_service import execute_agent_tool, flecket_config
        from deskbot_server.service.robot_performance_service import ROBOT_TOOL_INSTRUCTIONS, RobotPerformanceService

        self.performances = RobotPerformanceService(device_id)
        self._plans: dict[str, tuple[RobotPerformance, int, float]] = {}
        self._response_plans: dict[str, str] = {}
        self._motion_tasks: set[asyncio.Task] = set()
        self._performance_generation = 0
        self._accepted_generation: int | None = None
        self._performance_closed = False
        self._media_plans = {}
        self._response_media = {}
        self._announcements = {}
        self._response_announcements = {}
        super().__init__(hub, device_id, websocket,
                         client=client or FlecketLiveClient(flecket_config(device_id), tool_handler=execute_agent_tool), prefill_ms=prefill_ms)
        self.client.robot_tool = self.performances.schema()
        self.client.performance_handler = self._handle_performance
        self.client.robot_instructions = ROBOT_TOOL_INSTRUCTIONS
        self.agent_tools = AgentTools(device_id, self)
        self.client.agent_tools = self.agent_tools.schemas()
        self.client.agent_instructions = self.agent_tools.instructions()
        self.client.agent_tool_handler = self.agent_tools.execute
        self.media = DeviceMedia(self)

    async def announce(self, text, *, timeout=45):
        """An external Agent reminder succeeds only after the ROM played it."""
        if self.closed or not self.ready or not callable(getattr(self.client, "idle", None)) or not self.client.idle():
            return {"ok": False, "error": "设备离线或正在交互"}
        if self._announcements:
            return {"ok": False, "error": "已有提醒正在播报"}
        token = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._announcements[token] = future
        self._plans.clear()
        self._response_plans.clear()
        self._accepted_generation = None
        try:
            if not await self.client.send_announcement(str(text)[:1000], token):
                return {"ok": False, "error": "设备正在交互"}
            async with asyncio.timeout(timeout):
                return await future
        except (TimeoutError, asyncio.CancelledError) as exc:
            for run in list(self.runs.values()):
                if run.announcement == token:
                    self._stop(run)
            await self.client.cancel_announcement(token)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"ok": False, "error": "提醒未收到播放完成回执"}
        finally:
            self._announcements.pop(token, None)

    def _playback_finished(self, run, completed):
        future = self._announcements.get(run.announcement)
        if future is not None and not future.done():
            future.set_result({"ok": completed, "status": "completed" if completed else "interrupted"})

    async def rom_command(self, op, arguments):
        return await self.hub.rom_command(self.device_id, op, arguments, expected_generation=self.generation)

    async def media_tool(self, name, arguments, *, generation=None):
        from deskbot_server.dao import media_asset_store as store
        generation = getattr(self.client, "_input_generation", self._performance_generation) if generation is None else generation
        if name == "media_list":
            kind = arguments.get("kind")
            if kind not in (None, "music", "image"):
                raise ValueError("媒体类型无效")
            return {"ok": True, "assets": await asyncio.to_thread(store.list_assets, self.device_id, kind, str(arguments.get("query") or "")[:100])}
        if name == "stop_media":
            self._media_plans.clear()
            self._response_media.clear()
            return await self.media.stop()
        speak = arguments.get("speak", True)
        if not isinstance(speak, bool):
            raise ValueError("speak 必须为布尔值")
        asset_id = arguments.get("asset_id")
        item = await asyncio.to_thread(store.get, self.device_id, asset_id)
        if not self._current(generation):
            return {"ok": False, "status": "cancelled", "error": "本轮已被打断"}
        if name == "show_image":
            if item["kind"] != "image":
                raise ValueError("请选择图片资源")
            duration = arguments.get("duration_ms", 5000)
            if type(duration) is not int or not 1000 <= duration <= 30000:
                raise ValueError("图片展示时间需要 1000～30000 毫秒")
            if not speak:
                return {**await self.media.show_image(asset_id, duration, generation=generation), "speak": False}
        elif name != "play_music" or item["kind"] != "music":
            raise ValueError("请选择音乐资源")
        elif not speak:
            return {**await self.media.start_music(asset_id, generation=generation), "speak": False}
        if self._media_plans:
            raise ValueError("本轮已经安排了媒体播放")
        token = uuid.uuid4().hex
        self._media_plans[token] = {"asset_id": asset_id, "kind": item["kind"], "duration_ms": arguments.get("duration_ms", 5000),
                                    "generation": generation, "expires": time.monotonic()+20}
        return {"ok": True, "status": "scheduled", "media_id": token, "title": item["title"]}

    def offer_ack(self, ack):
        self.media.offer_ack(ack)
        super().offer_ack(ack)

    def _current(self, generation):
        entry = self.hub._devices.get(self.device_id)
        return (not self.closed and not self._performance_closed and entry is not None and entry.generation == self.generation
                and entry.ws is self.websocket
                and generation == getattr(self.client, "_input_generation", self._performance_generation))

    async def _handle_performance(self, arguments, *, call_id, response_id, generation):
        if not self._current(generation):
            return {"ok": False, "status": "cancelled", "error": "设备连接或对话轮次已变更"}
        if self._accepted_generation == generation:
            return {"ok": False, "error": "本轮已安排过身体表现，请勿重复调用"}
        try:
            plan = await asyncio.to_thread(self.performances.compile, arguments)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not self._current(generation):
            return {"ok": False, "status": "cancelled", "error": "本轮已被打断"}
        if self._accepted_generation == generation:
            return {"ok": False, "error": "本轮已安排过身体表现，请勿重复调用"}
        self._accepted_generation = generation
        if plan.speak:
            # Only the explicit continuation metadata may consume this plan.
            self._plans[call_id] = (plan, generation, time.monotonic() + 20)
            result = {**plan.result("scheduled"), "performance_id": call_id}
        else:
            try:
                outcome = await self._play_motion(plan)
            except asyncio.CancelledError:
                if self.closed or self._performance_closed or self._current(generation):
                    raise
                return {**plan.result("cancelled"), "error": "本轮已被打断"}
            result = plan.result("completed" if outcome.completed else outcome.status.value)
            if not outcome.completed:
                result["error"] = "设备未完成动作"
        logger.info("[robot_perform] device_id=%s call_id=%s response_id=%s gesture=%s expression=%s timing=%s status=%s",
                    self.device_id, call_id, response_id, plan.gesture, plan.expression, plan.timing, result["status"])
        return result

    async def _on_event(self, action, data):
        if action == "input.speech.started":
            self._performance_generation = getattr(self.client, "_input_generation", self._performance_generation + 1)
            self._plans.clear()
            self._response_plans.clear()
            self._media_plans.clear()
            self._response_media.clear()
            self._spawn(self.media.stop())
            for task in list(self._motion_tasks):
                task.cancel()
        elif action == "response.started":
            pid = data.get("performance_id")
            if isinstance(pid, str) and pid in self._plans:
                self._response_plans[data["run_id"]] = pid
            mid = data.get("media_id")
            if isinstance(mid, str) and mid in self._media_plans:
                self._response_media[data["run_id"]] = mid
            token = data.get("announcement")
            if isinstance(token, str) and token in self._announcements:
                self._response_announcements[data["run_id"]] = token
        elif action == "announcement.rejected":
            future = self._announcements.get(data.get("token"))
            if future is not None and not future.done():
                future.set_result({"ok": False, "error": "提醒被用户交互打断或延后"})
        elif action == "response.ended":
            self._response_announcements.pop(data.get("run_id"), None)
            self._response_plans.pop(data.get("run_id"), None)
            self._response_media.pop(data.get("run_id"), None)
            if not data.get("has_audio") and not data.get("has_tools"):
                future = self._announcements.get(data.get("announcement"))
                if future is not None and not future.done():
                    future.set_result({"ok": False, "error": "外部 Agent 没有生成提醒语音"})
                pid = data.get("performance_id")
                value = self._plans.pop(pid, None) if isinstance(pid, str) else None
                if value and data.get("status") == "completed" and self._current(value[1]) and time.monotonic() <= value[2]:
                    self._spawn(self._play_motion(value[0]))
                mid = data.get("media_id")
                media = self._media_plans.pop(mid, None) if isinstance(mid, str) else None
                if media and data.get("status") == "completed":
                    self._spawn(self._start_planned_media(media))

    def _configure_run(self, run):
        run.announcement = self._response_announcements.pop(run.identity[0], None)
        pid = self._response_plans.pop(run.identity[0], None)
        value = self._plans.pop(pid, None)
        if value and self._current(value[1]) and time.monotonic() <= value[2]:
            run.performance = value[0]
        mid = self._response_media.pop(run.identity[0], None)
        media = self._media_plans.pop(mid, None)
        if media and self._current(media["generation"]) and time.monotonic() <= media["expires"]:
            run.media_plan = media

    async def _before_audio(self, run):
        if run.performance and run.performance.timing == "before_speech":
            outcome = await self._play_motion(run.performance)
            if not outcome.completed:
                raise RuntimeError("robot before-speech playback failed")

    def _audio_tracks(self, run):
        if run.performance and run.performance.timing == "with_speech" and not run.performance_attached:
            run.performance_attached = True
            return run.performance.tracks()
        return {}

    def _minimum_audio_ms(self, run):
        return run.performance.duration_ms if run.performance and run.performance.timing == "with_speech" else 0

    async def _after_audio(self, run):
        if run.performance and run.performance.timing == "after_speech":
            outcome = await self._play_motion(run.performance)
            if not outcome.completed:
                raise RuntimeError("robot after-speech playback failed")
        if run.media_plan:
            self._spawn(self._start_planned_media(run.media_plan))

    async def _start_planned_media(self, plan):
        if not self._current(plan["generation"]) or time.monotonic() > plan["expires"]:
            return
        try:
            if plan["kind"] == "image":
                await self.media.show_image(plan["asset_id"], plan["duration_ms"], generation=plan["generation"])
            else:
                await self.media.start_music(plan["asset_id"], generation=plan["generation"])
        except ValueError:
            self.media.status = {"state": "failed", "error": "媒体资源已不可用"}

    async def _play_motion(self, plan):
        # Standalone, non-stream PB uses wall time. Stream anim/servo tracks
        # would otherwise wait for an audio clock that pure actions don't have.
        req = uuid.uuid4().hex
        wire = {"type": "pb_single", "req": req, "idx": 0, "chunk_ms": plan.duration_ms, **plan.tracks()}
        seq = PbSeq(req=req, entries=(PbBlock.from_wire(wire),), level=1, action=PbAction.APPEND)

        async def play():
            outcome = None
            try:
                if self.closed or self._performance_closed:
                    return PlaybackOutcome(PlaybackStatus.DISCONNECTED, req=req)
                outcome = await self.hub.send_and_wait(self.device_id, seq, expected_generation=self.generation)
                return outcome
            finally:
                if outcome is None or not outcome.completed:
                    await self.hub.interrupt_current_playback(self.device_id, reason="robot_performance_cancelled", expected_req=req)

        task = asyncio.create_task(play())
        self._motion_tasks.add(task)
        task.add_done_callback(self._motion_tasks.discard)
        try:
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        self._performance_closed = True
        for future in list(self._announcements.values()):
            if not future.done():
                future.set_result({"ok": False, "error": "设备连接已关闭"})
        self._response_announcements.clear()
        self._media_plans.clear()
        self._response_media.clear()
        await self.media.stop_music()
        self._plans.clear()
        self._response_plans.clear()
        tasks = list(self._motion_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()


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
            elif kind == "rom_result":
                hub.accept_rom_result(device_id, data)
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
