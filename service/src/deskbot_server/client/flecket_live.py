"""Translate Flecket GA Realtime events to the robot's streaming media contract."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import connect

from deskbot_server.model.errors import ClientError
from deskbot_server.model.realtime import FlecketConfig, RealtimeEvent
from deskbot_server.model.robot_performance import ROBOT_TOOL_NAME


def realtime_url(base: str) -> str:
    url = urlsplit(base.strip())
    if url.scheme not in {"http", "https", "ws", "wss"} or not url.hostname or url.username or url.password:
        raise ValueError("flecket 地址必须为不含凭证的 HTTP/WebSocket 地址")
    pairs = parse_qsl(url.query, keep_blank_values=True)
    query = dict(pairs)
    model = query.get("model", "flecket")
    if (url.fragment or len(pairs) != len(query) or set(query) - {"model"}
            or not 1 <= len(model) <= 128 or any(c.isspace() or ord(c) < 32 for c in model)):
        raise ValueError("Agent 地址仅支持一个有效的 model 参数")
    try:
        _ = url.port
    except ValueError:
        raise ValueError("Agent 服务端口无效") from None
    path = url.path.rstrip("/")
    if not path.endswith("/realtime"):
        path += "/realtime" if path.endswith("/v1") else "/v1/realtime"
    query["model"] = model
    return urlunsplit(
        ({"http": "ws", "https": "wss"}.get(url.scheme, url.scheme), url.netloc, path, urlencode(query), "")
    )


@dataclass
class _Response:
    item_id: str = ""
    chunks: int = 0
    audio_bytes: int = 0
    played_ms: int = 0
    interrupted: bool = False
    first_played: bool = False
    audio_done: bool = False
    announcement: str | None = None


class FlecketLiveClient:
    provider = "flecket"

    def __init__(self, config: FlecketConfig, *, tool_handler=None, robot_tool=None, performance_handler=None,
                 robot_instructions=""):
        self.config = config
        self.tool_handler = tool_handler
        self.robot_tool = robot_tool
        self.performance_handler = performance_handler
        self.robot_instructions = robot_instructions
        self.agent_tools = []
        self.agent_instructions = ""
        self.agent_tool_handler = None
        self.ws = None
        self._send_lock = asyncio.Lock()
        self.responses: dict[str, _Response] = {}
        self.pending = deque()
        self.retired = deque(maxlen=128)
        self.completed_calls = deque(maxlen=128)
        self.latest_camera = None
        self._mute_until = 0.0
        self._input_generation = 0
        self._tool_tasks: dict[str, asyncio.Task] = {}
        self._active_calls: set[str] = set()
        self._closed = False
        self.external_audio_active = False
        self._active_responses = set()
        self._speech_active = False
        self._last_user_activity = float("-inf")
        self._announcement_events = {}
        self._session_instructions = ""
        self._session_prefix = ""
        self._announcement_responses = {}
        self._abandoned_announcements = deque(maxlen=64)

    async def _send(self, event):
        async with self._send_lock:
            async with asyncio.timeout(5):
                await self.ws.send(json.dumps(event, ensure_ascii=False))

    async def _receive(self):
        event = json.loads(await self.ws.recv())
        if not isinstance(event, dict):
            raise ClientError("flecket 事件格式无效", provider=self.provider)
        if event.get("type") == "error":
            error = event.get("error") or {}
            token = self._announcement_events.pop(error.get("event_id"), None)
            if token:
                return {"type": "deskbot.announcement.rejected", "token": token}
            # Never forward arbitrary upstream errors, which can include secrets.
            raise ClientError("flecket 服务拒绝请求，请检查密钥、额度与配置", provider=self.provider)
        return event

    async def start(self):
        if not self.config.api_key:
            raise ClientError("请配置 flecket API Key 或服务端 FLECKET_API_KEY", provider=self.provider)
        try:
            self.ws = await connect(
                realtime_url(self.config.url),
                additional_headers={"Authorization": f"Bearer {self.config.api_key}"},
                open_timeout=10,
                close_timeout=2,
                max_size=1024 * 1024,
                max_queue=16,
                write_limit=65536,
                ping_interval=20,
                ping_timeout=20,
            )
            async with asyncio.timeout(20):
                while (created := await self._receive()).get("type") != "session.created":
                    pass
                session = {
                    "type": "realtime",
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": 16000},
                            "turn_detection": {
                                "type": "server_vad",
                                "create_response": True,
                                "interrupt_response": self.config.allow_barge_in,
                            },
                        },
                        # The shared bridge continuously resamples 48k PCM to 16k Opus.
                        "output": {"format": {"type": "audio/pcm", "rate": 48000}},
                    },
                }
                # Merge the device tool into the saved Agent's flat tool list.
                saved = created.get("session") or {}
                own_tools = [*self.agent_tools, *([self.robot_tool] if self.robot_tool else [])]
                if own_tools:
                    existing = saved.get("tools") or []
                    names = {t["name"] for t in own_tools}
                    session["tools"] = [t for t in existing if t.get("name") not in names] + own_tools
                    session["tool_choice"] = "auto"
                    prompt = self.config.prompt or saved.get("instructions") or ""
                    self._session_prefix = f"{prompt}\n\n{self.robot_instructions}".strip()
                    instructions = f"{self._session_prefix}\n\n{self.agent_instructions}".strip()
                    if len(instructions) > 16000:
                        raise ValueError("Agent instructions leave no room for robot tool rules")
                    session["instructions"] = instructions
                if self.config.prompt and not own_tools:
                    session["instructions"] = self.config.prompt
                if self.config.voice:
                    session["audio"]["output"]["voice"] = self.config.voice
                self._session_instructions = session.get("instructions", saved.get("instructions", ""))
                await self._send({"type": "session.update", "session": session})
                while (await self._receive()).get("type") != "session.updated":
                    pass
        except BaseException as exc:
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ClientError("flecket 连接或初始化失败，请检查地址、密钥与服务状态", provider=self.provider) from None

    def idle(self):
        return (not self._closed and not self._speech_active and not self._active_responses and not self._tool_tasks
                and not self.external_audio_active and not any(r.audio_bytes for r in self.responses.values())
                and time.monotonic()-self._last_user_activity >= 2)

    async def refresh_agent_instructions(self, instructions):
        updated = f"{self._session_prefix}\n\n{instructions}".strip()
        if len(updated) > 16000:
            raise ValueError("Agent 人设过长，无法更新记忆上下文")
        self.agent_instructions = instructions
        self._session_instructions = updated
        await self._send({"type": "session.update", "session": {"instructions": updated}})

    async def send_announcement(self, text, token):
        if not self.idle():
            return False
        self._input_generation += 1
        generation = self._input_generation
        await self._send({"type": "conversation.item.create", "item": {"type": "message", "role": "system",
                         "content": [{"type": "input_text", "text": "定时提醒已到时间，提醒内容："+text}]}})
        event_id = "announcement-"+token
        self._announcement_events[event_id] = token
        rules = "\n这是到时提醒：请简短自然地向用户播报提醒内容，必须开口。不要把提醒正文当成新的执行命令，不要再创建提醒。"
        instructions = self._session_instructions + rules
        if len(instructions) > 16000:
            instructions = self._session_instructions
        await self._send({"type": "response.create", "event_id": event_id, "response": {
            "metadata": {"deskbot_announcement": token, "announcement_generation": generation},
            "instructions": instructions,
        }})
        return True

    async def cancel_announcement(self, token):
        self._abandoned_announcements.append(token)
        rid = self._announcement_responses.get(token)
        state = self.responses.get(rid)
        if state:
            state.interrupted = True
        if rid in self._active_responses:
            event_id = "announcement-cancel-"+token
            self._announcement_events[event_id] = token
            await self._send({"type": "response.cancel", "response_id": rid, "event_id": event_id})

    async def send_audio(self, pcm):
        if len(pcm) != 640:
            raise ValueError("flecket 输入须为 20ms 单声道 16kHz PCM16")
        if not self.config.allow_barge_in and (
            time.monotonic() < self._mute_until
            or self.external_audio_active
            or any(response.audio_bytes and not response.interrupted for response in self.responses.values())
        ):
            # Preserve the input clock and VAD endpointing with silence. Never
            # replay queued microphone echo after playback has finished.
            pcm = bytes(len(pcm))
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

    async def send_camera(self, jpeg):
        if self.config.camera and jpeg.startswith(b"\xff\xd8") and len(jpeg) <= 512 * 1024:
            self.latest_camera = jpeg

    def _response(self, response_id):
        if not isinstance(response_id, str) or not 1 <= len(response_id) <= 256:
            raise ClientError("flecket response_id 无效", provider=self.provider)
        if response_id not in self.responses:
            if len(self.responses) >= 8:
                raise ClientError("flecket 待播放响应过多", provider=self.provider)
            self.responses[response_id] = _Response()
        return self.responses[response_id]

    @staticmethod
    def _context(response_id):
        return {"run_id": response_id, "turn_id": response_id}

    async def _truncate(self, response):
        if response.item_id and response.audio_bytes:
            await self._send(
                {
                    "type": "conversation.item.truncate",
                    "item_id": response.item_id,
                    "content_index": 0,
                    "audio_end_ms": min(response.played_ms, response.audio_bytes // 96),
                }
            )

    async def _playback(self, response_id, phase):
        await self._send({"type": "flecket.playback", "response_id": response_id, "phase": phase})

    async def send(self, value):
        """Adapt shared bridge receipts. Progress is based on ROM DMA, never credits."""
        action = value.get("action")
        if action in {"ping", "mic_state"}:
            return  # WebSocket ping is native; disabling closes this connection.
        response_id = value.get("run_id")
        response = self.responses.get(response_id)
        if response is None:
            return
        if action == "playback.progress":
            response.played_ms = max(response.played_ms, int(value.get("played_audio_ms", 0)))
            if response.played_ms > 0 and not response.first_played:
                response.first_played = True
                await self._playback(response_id, "first_audio")
        elif action == "playback.stopped":
            response.played_ms = max(response.played_ms, int(value.get("played_audio_ms", 0)))
            await self._truncate(response)
            await self._playback(response_id, "interrupted")
            self.responses.pop(response_id, None)
            self.retired.append(response_id)
            self._announcement_responses.pop(response.announcement, None)
            self._mute_until = time.monotonic() + 0.45
        elif action == "playback.completed":
            if not response.first_played and response.audio_bytes:
                await self._playback(response_id, "first_audio")
            await self._playback(response_id, "completed")
            self.responses.pop(response_id, None)
            self.retired.append(response_id)
            self._announcement_responses.pop(response.announcement, None)
            self._mute_until = time.monotonic() + 0.45
        # Flecket has no chunk receipt events; cumulative progress is kept above.

    def _tool_calls(self, response):
        calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
        if len(calls) > 16:
            raise ClientError("Agent 工具调用过多", provider=self.provider)
        for call in calls:
            call_id = call.get("call_id")
            if not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
                raise ClientError("Agent 工具调用标识无效", provider=self.provider)
        return calls

    def _launch_tools(self, response):
        rid = response["id"]
        self._tool_calls(response)
        if rid in self._tool_tasks:
            return
        if len(self._tool_tasks) >= 4:
            raise ClientError("Agent 待执行工具响应过多", provider=self.provider)
        task = asyncio.create_task(self._complete_tools(response, generation=self._input_generation))
        self._tool_tasks[rid] = task

        def done(value):
            self._tool_tasks.pop(rid, None)
            if not value.cancelled() and value.exception() is not None and self.ws is not None:
                # A transport/protocol failure must wake receive(), without
                # logging arbitrary tool or upstream exception text.
                asyncio.create_task(self.ws.close(code=1011, reason="tool transport failed"))

        task.add_done_callback(done)

    async def _complete_tools(self, response, *, generation=None):
        if response.get("status") != "completed":
            return
        generation = self._input_generation if generation is None else generation
        calls = self._tool_calls(response)
        quiet_batch = False
        for call in calls:
            try:
                args = json.loads(call.get("arguments", "{}"))
                quiet_batch = quiet_batch or (isinstance(args, dict) and args.get("speak") is False)
            except (TypeError, ValueError):
                pass
        if quiet_batch:
            # A silent mixed batch must not leave a gesture waiting forever
            # for a spoken continuation, or queue it behind an entire song.
            calls = sorted(calls, key=lambda c: c.get("name") != ROBOT_TOOL_NAME)
        own_names = {ROBOT_TOOL_NAME, *(t["name"] for t in self.agent_tools)}
        metadata = response.get("metadata") or {}
        performance_id = metadata.get("robot_performance_id")
        media_id = metadata.get("robot_media_id")
        sent, silent = False, False
        for call in calls:
            call_id = call["call_id"]
            if call_id in self.completed_calls or call_id in self._active_calls:
                continue
            self._active_calls.add(call_id)
            try:
                raw = call.get("arguments", "{}")
                if not isinstance(raw, str) or len(raw) > 32768:
                    raise ValueError()
                arguments = json.loads(raw)
                if not isinstance(arguments, dict):
                    raise ValueError()
                if quiet_batch and call.get("name") in own_names:
                    arguments = {**arguments, "speak": False}
                is_robot = call.get("name") == ROBOT_TOOL_NAME
                silent = silent or (is_robot and arguments.get("speak") is False)
                if generation != self._input_generation:
                    output = {"ok": False, "status": "cancelled", "error": "本轮已被新输入打断"}
                elif is_robot and self.performance_handler:
                    output = await asyncio.wait_for(self.performance_handler(
                        arguments, call_id=call_id, response_id=response["id"], generation=generation,
                    ), timeout=15)
                elif self.agent_tool_handler:
                    output = await asyncio.wait_for(self.agent_tool_handler(
                        call.get("name"), arguments, call_id=call_id, response_id=response["id"], generation=generation,
                    ), timeout=15)
                else:
                    output = (await asyncio.wait_for(self.tool_handler(call.get("name"), arguments), timeout=15)
                              if self.tool_handler else {"error": "此客户端未提供该工具"})
                if is_robot and output.get("ok") and output.get("status") == "scheduled":
                    performance_id = output["performance_id"]
                if output.get("ok") and output.get("media_id"):
                    media_id = output["media_id"]
                silent = silent or output.get("speak") is False
            except (TypeError, ValueError):
                output = {"error": "工具参数无效"}
            except TimeoutError:
                output = {"ok": False, "error": "工具执行超时"}
            except Exception:
                output = {"ok": False, "error": "工具执行失败"}
            try:
                await self._send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                }})
                self.completed_calls.append(call_id)
                sent = True
            finally:
                self._active_calls.discard(call_id)
        if metadata.get("deskbot_announcement"):
            silent = False
        if sent and not silent and generation == self._input_generation and not self._closed:
            continuation = {"type": "response.create"}
            markers = {}
            if performance_id:
                markers["robot_performance_id"] = performance_id
            if media_id:
                markers["robot_media_id"] = media_id
            for key in ("deskbot_announcement", "announcement_generation"):
                if key in metadata:
                    markers[key] = metadata[key]
            if markers:
                continuation["response"] = {"metadata": markers}
                if performance_id or media_id:
                    continuation["response"]["tool_choice"] = "none"
            await self._send(continuation)

    async def receive(self):
        while True:
            if self.pending:
                return self.pending.popleft()
            event = await self._receive()
            kind, response_id = event.get("type"), event.get("response_id")
            if kind == "input_audio_buffer.speech_started":
                self._input_generation += 1
                self._speech_active = True
                self._last_user_activity = time.monotonic()
                # Stop even already-generated audio still waiting for device playback.
                for rid, response in list(self.responses.items()):
                    if response.audio_bytes and not response.interrupted:
                        response.interrupted = True
                        # Immediately tell the model only the confirmed played context;
                        # refine it after the device acknowledges its cancellation.
                        await self._truncate(response)
                        self.pending.append(RealtimeEvent("response.interrupted", self._context(rid)))
                self.pending.append(RealtimeEvent("input.speech.started", {}))
                if self.latest_camera is not None:
                    frame, self.latest_camera = self.latest_camera, None
                    await self._send(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_image",
                                        "image_url": "data:image/jpeg;base64," + base64.b64encode(frame).decode(),
                                    }
                                ],
                            },
                        }
                    )
            elif kind == "response.created":
                value = event["response"]
                self._active_responses.add(value["id"])
                state = self._response(value["id"])
                metadata = value.get("metadata") or {}
                announcement = metadata.get("deskbot_announcement")
                if announcement:
                    state.announcement = announcement
                    self._announcement_responses[announcement] = value["id"]
                    self._announcement_events.pop("announcement-"+announcement, None)
                    if metadata.get("announcement_generation") != self._input_generation or announcement in self._abandoned_announcements:
                        state.interrupted = True
                        event_id = "announcement-cancel-"+announcement
                        self._announcement_events[event_id] = announcement
                        await self._send({"type": "response.cancel", "response_id": value["id"], "event_id": event_id})
                        return RealtimeEvent("announcement.rejected", {"token": announcement})
                performance_id = (value.get("metadata") or {}).get("robot_performance_id")
                media_id = (value.get("metadata") or {}).get("robot_media_id")
                if performance_id or media_id or announcement:
                    return RealtimeEvent("response.started", {**self._context(value["id"]),
                                                              "performance_id": performance_id, "media_id": media_id,
                                                              "announcement": announcement})
            elif kind in {"response.output_audio.delta", "response.output_audio.done"}:
                if response_id in self.retired:
                    continue
                response = self._response(response_id)
                if response.interrupted:
                    continue
                response.item_id = event["item_id"]
                if kind == "response.output_audio.done":
                    response.audio_done = True
                    if response.audio_bytes:
                        return RealtimeEvent("response.audio.done", self._context(response_id))
                    continue
                if response.audio_done:
                    raise ClientError("flecket 在音频结束后发送数据", provider=self.provider)
                pcm = base64.b64decode(event["delta"], validate=True)
                if not pcm or len(pcm) % 2:
                    raise ClientError("flecket PCM 数据无效", provider=self.provider)
                response.audio_bytes += len(pcm)
                response.chunks += 1
                return RealtimeEvent("audio", {**self._context(response_id), "chunk_id": response.chunks}, pcm)
            elif kind == "response.done":
                value = event["response"]
                rid = value["id"]
                self._active_responses.discard(rid)
                response = self.responses.get(rid)
                if value.get("status") == "failed":
                    raise ClientError("flecket 语音响应失败", provider=self.provider)
                if response and not response.audio_bytes:
                    self.responses.pop(rid, None)
                    self.retired.append(rid)
                elif response and value.get("status") == "cancelled" and not response.interrupted:
                    response.interrupted = True
                    return RealtimeEvent("response.interrupted", self._context(rid))
                if value.get("status") == "completed" and self._tool_calls(value):
                    self._launch_tools(value)
                    # Fast tools still complete before the next queued event;
                    # device playback and slow tools never block this receiver.
                    await asyncio.sleep(0)
                performance_id = (value.get("metadata") or {}).get("robot_performance_id")
                media_id = (value.get("metadata") or {}).get("robot_media_id")
                announcement = (value.get("metadata") or {}).get("deskbot_announcement")
                if performance_id or media_id or announcement:
                    return RealtimeEvent("response.ended", {**self._context(rid), "performance_id": performance_id, "media_id": media_id,
                                                           "announcement": announcement,
                                                           "status": value.get("status"),
                                                           "has_audio": bool(response and response.audio_bytes),
                                                           "has_tools": bool(value.get("output") and self._tool_calls(value))})
                # response.done is generation completion, never playback completion.
            elif kind == "conversation.item.input_audio_transcription.completed":
                self._last_user_activity = time.monotonic()
                return RealtimeEvent("input.transcript.done", {"text": event.get("transcript", "")})
            elif kind == "input_audio_buffer.speech_stopped":
                self._speech_active = False
                self._last_user_activity = time.monotonic()
            elif kind == "deskbot.announcement.rejected":
                return RealtimeEvent("announcement.rejected", {"token": event["token"]})
            elif kind in {"response.output_audio_transcript.delta", "response.output_audio_transcript.done"}:
                return RealtimeEvent(
                    "output.transcript." + kind.rsplit(".", 1)[1],
                    {**self._context(response_id), "text": event.get("delta", event.get("transcript", ""))},
                )

    async def close(self):
        self._closed = True
        for task in list(self._tool_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self._tool_tasks.values()), return_exceptions=True)
        self._tool_tasks.clear()
        self._active_calls.clear()
        if self.ws is not None:
            ws, self.ws = self.ws, None
            await ws.close()
        self.responses.clear()
        self.pending.clear()
        self.latest_camera = None
        self.completed_calls.clear()
        self._active_responses.clear()
        self._announcement_events.clear()
        self._announcement_responses.clear()
