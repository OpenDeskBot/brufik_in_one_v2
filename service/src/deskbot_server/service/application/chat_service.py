"""应用服务：组合 ASR / LLM / TTS，委托给单例 Service 层。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from deskbot_server.model.settings import AppSettings
from deskbot_server.ports.asr import AsrPort
from deskbot_server.ports.llm import LlmPort
from deskbot_server.ports.tts import TtsPort
from deskbot_server.service.asr_service import AsrService
from deskbot_server.service.tts_service import TtsService


class ChatService:
    """应用服务：组合 ASR / LLM / TTS 端口，不含 WebSocket 细节。"""

    supports_streaming = True

    def __init__(
        self, settings: AppSettings, *, asr: AsrPort, llm: LlmPort, tts: TtsPort, route_devices: bool = False
    ) -> None:
        self.settings = settings
        self._asr = asr
        self._llm = llm
        self._tts = tts
        self._route_devices = route_devices
        self._realtime_warmed: set[str] = set()
        self._warm_locks: dict[str, asyncio.Lock] = {}

    @property
    def tts_cfg(self) -> dict:
        return self.settings.tts_cfg

    @property
    def asr_chat_device_pb_only(self) -> bool:
        return self.settings.server.asr_chat_device_pb_only

    async def asr(self, pcm_bytes: bytes, sample_rate: int, *, device_id: str | None = None) -> str:
        if self._route_devices:
            return await AsrService().transcribe(pcm_bytes, sample_rate, device_id=device_id)
        return await self._asr.transcribe(pcm_bytes, sample_rate)

    def is_valid_asr_text(self, text: str, *, device_id: str | None = None) -> bool:
        if self._route_devices:
            return AsrService().is_valid_text(text, device_id=device_id)
        return self._asr.is_valid_text(text)

    async def llm(
        self,
        text: str,
        *,
        device_context: str | None = None,
        device_id: str | None = None,
        history_messages: list[dict[str, str]] | None = None,
        extra_messages: list[dict[str, str]] | None = None,
        on_tts_ready: Callable[[str], Awaitable[None]] | None = None,
        on_system_prompt: Callable[[str], None] | None = None,
        user_message_override: str | None = None,
        on_raw_response: Callable[[dict], None] | None = None,
    ) -> str:
        return await self._llm.complete(
            text,
            device_context=device_context,
            device_id=device_id,
            history_messages=history_messages,
            extra_messages=extra_messages,
            on_tts_ready=on_tts_ready,
            on_system_prompt=on_system_prompt,
            user_message_override=user_message_override,
            on_raw_response=on_raw_response,
        )

    async def llm_tool_round(
        self,
        text: str,
        *,
        device_context: str | None = None,
        device_id: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        extra_messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "auto",
        on_system_prompt: Callable[[str], None] | None = None,
        user_message_override: str | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> Any:
        """原生 function calling 单回合（透传 LlmPort.llm_tool_round）。"""
        stream_kwargs = {"on_delta": on_delta} if on_delta is not None else {}
        return await self._llm.llm_tool_round(
            text,
            device_context=device_context,
            device_id=device_id,
            history_messages=history_messages,
            extra_messages=extra_messages,
            tools=tools,
            tool_choice=tool_choice,
            on_system_prompt=on_system_prompt,
            user_message_override=user_message_override,
            **stream_kwargs,
        )

    async def warmup_realtime(self, device_id: str) -> None:
        async with self._warm_locks.setdefault(device_id, asyncio.Lock()):
            if device_id in self._realtime_warmed:
                return
            warmup = getattr(self._llm, "warmup_realtime", None)
            if warmup is not None:
                await warmup(device_id)
            from deskbot_server.service.tts_routing import resolve_tts_provider
            if resolve_tts_provider(device_id) == "macos":
                await asyncio.gather(*(self.tts_phoneme_segments(text, device_id=device_id)
                                       for text in ("你好。", "在呢，怎么了？", "在呢。", "我在呢。")))
            self._realtime_warmed.add(device_id)

    async def aclose(self) -> None:
        close = getattr(self._llm, "aclose", None)
        if close is not None:
            await close()
        from deskbot_server.client.registry import aclose_clients

        await aclose_clients()

    async def tts_phoneme_segments(self, text: str, *, device_id: str | None = None) -> tuple[int, list[dict]]:
        if self._route_devices:
            return await TtsService().synthesize_phoneme_segments(text, device_id=device_id)
        sr, segs = await self._tts.synthesize_phoneme_segments(text)
        return sr, [TtsService._seg_to_dict(s) for s in segs]
