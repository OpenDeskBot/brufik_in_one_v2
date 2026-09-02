"""应用服务：组合 ASR / LLM / TTS，委托给单例 Service 层。"""

from __future__ import annotations

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

    def __init__(self, settings: AppSettings, *, asr: AsrPort, llm: LlmPort, tts: TtsPort) -> None:
        self.settings = settings
        self._asr = asr
        self._llm = llm
        self._tts = tts

    @property
    def tts_cfg(self) -> dict:
        return self.settings.tts_cfg

    @property
    def asr_chat_device_pb_only(self) -> bool:
        return self.settings.server.asr_chat_device_pb_only

    async def asr(self, pcm_bytes: bytes, sample_rate: int, *, device_id: str | None = None) -> str:
        try:
            return await AsrService().transcribe(pcm_bytes, sample_rate, device_id=device_id)
        except RuntimeError:
            return await self._asr.transcribe(pcm_bytes, sample_rate)

    def is_valid_asr_text(self, text: str, *, device_id: str | None = None) -> bool:
        try:
            return AsrService().is_valid_text(text, device_id=device_id)
        except RuntimeError:
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
    ) -> Any:
        """原生 function calling 单回合（透传 LlmPort.llm_tool_round）。"""
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
        )

    async def tts_phoneme_segments(self, text: str, *, device_id: str | None = None) -> tuple[int, list[dict]]:
        try:
            return await TtsService().synthesize_phoneme_segments(text, device_id=device_id)
        except RuntimeError:
            sr, segs = await self._tts.synthesize_phoneme_segments(text)
            return sr, [TtsService._seg_to_dict(s) for s in segs]
