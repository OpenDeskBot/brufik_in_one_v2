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


class FlecketLiveClient:
    provider = "flecket"

    def __init__(self, config: FlecketConfig, *, tool_handler=None):
        self.config = config
        self.tool_handler = tool_handler
        self.ws = None
        self._send_lock = asyncio.Lock()
        self.responses: dict[str, _Response] = {}
        self.pending = deque()
        self.retired = deque(maxlen=128)
        self.completed_calls = deque(maxlen=128)
        self.latest_camera = None
        self._mute_until = 0.0

    async def _send(self, event):
        async with self._send_lock:
            async with asyncio.timeout(5):
                await self.ws.send(json.dumps(event, ensure_ascii=False))

    async def _receive(self):
        event = json.loads(await self.ws.recv())
        if not isinstance(event, dict):
            raise ClientError("flecket 事件格式无效", provider=self.provider)
        if event.get("type") == "error":
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
                while (await self._receive()).get("type") != "session.created":
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
                # Saved agents own their instructions, tools and voice unless
                # the device explicitly supplies an override.
                if self.config.prompt:
                    session["instructions"] = self.config.prompt
                if self.config.voice:
                    session["audio"]["output"]["voice"] = self.config.voice
                await self._send({"type": "session.update", "session": session})
                while (await self._receive()).get("type") != "session.updated":
                    pass
        except BaseException as exc:
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ClientError("flecket 连接或初始化失败，请检查地址、密钥与服务状态", provider=self.provider) from None

    async def send_audio(self, pcm):
        if len(pcm) != 640:
            raise ValueError("flecket 输入须为 20ms 单声道 16kHz PCM16")
        if not self.config.allow_barge_in and (
            time.monotonic() < self._mute_until
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
            self._mute_until = time.monotonic() + 0.45
        elif action == "playback.completed":
            if not response.first_played and response.audio_bytes:
                await self._playback(response_id, "first_audio")
            await self._playback(response_id, "completed")
            self.responses.pop(response_id, None)
            self.retired.append(response_id)
            self._mute_until = time.monotonic() + 0.45
        # Flecket has no chunk receipt events; cumulative progress is kept above.

    async def _complete_tools(self, response):
        if response.get("status") != "completed":
            return
        calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
        if len(calls) > 16:
            raise ClientError("Agent 工具调用过多", provider=self.provider)
        sent = False
        for call in calls:
            call_id = call.get("call_id")
            if not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
                raise ClientError("Agent 工具调用标识无效", provider=self.provider)
            if call_id in self.completed_calls:
                continue
            try:
                raw = call.get("arguments", "{}")
                if not isinstance(raw, str) or len(raw) > 32768:
                    raise ValueError()
                arguments = json.loads(raw)
                if not isinstance(arguments, dict):
                    raise ValueError()
                output = (await self.tool_handler(call.get("name"), arguments) if self.tool_handler
                          else {"error": "此客户端未提供该工具"})
            except (TypeError, ValueError):
                output = {"error": "工具参数无效"}
            await self._send({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call_id,
                "output": json.dumps(output, ensure_ascii=False),
            }})
            self.completed_calls.append(call_id)
            sent = True
        if sent:
            await self._send({"type": "response.create"})

    async def receive(self):
        while True:
            if self.pending:
                return self.pending.popleft()
            event = await self._receive()
            kind, response_id = event.get("type"), event.get("response_id")
            if kind == "input_audio_buffer.speech_started":
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
                self._response(event["response"]["id"])
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
                response = self.responses.get(rid)
                if value.get("status") == "failed":
                    raise ClientError("flecket 语音响应失败", provider=self.provider)
                if response and not response.audio_bytes:
                    self.responses.pop(rid, None)
                    self.retired.append(rid)
                elif response and value.get("status") == "cancelled" and not response.interrupted:
                    response.interrupted = True
                    return RealtimeEvent("response.interrupted", self._context(rid))
                await self._complete_tools(value)
                # response.done is generation completion, never playback completion.
            elif kind == "conversation.item.input_audio_transcription.completed":
                return RealtimeEvent("input.transcript.done", {"text": event.get("transcript", "")})
            elif kind in {"response.output_audio_transcript.delta", "response.output_audio_transcript.done"}:
                return RealtimeEvent(
                    "output.transcript." + kind.rsplit(".", 1)[1],
                    {**self._context(response_id), "text": event.get("delta", event.get("transcript", ""))},
                )

    async def close(self):
        if self.ws is not None:
            ws, self.ws = self.ws, None
            await ws.close()
        self.responses.clear()
        self.pending.clear()
        self.latest_camera = None
        self.completed_calls.clear()
