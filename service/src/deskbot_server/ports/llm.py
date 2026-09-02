from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class LlmPort(Protocol):
    async def complete(
        self,
        user_text: str,
        *,
        device_context: str | None = None,
        device_id: str | None = None,
        history_messages: list[dict[str, str]] | None = None,
        extra_messages: list[dict[str, str]] | None = None,
        on_tts_ready: Callable[[str], Awaitable[None]] | None = None,
        on_system_prompt: Callable[[str], None] | None = None,
        user_message_override: str | None = None,
    ) -> str: ...

    async def llm_tool_round(
        self,
        user_text: str,
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
        # 具体为 LlmToolRoundResult（实现侧定义，端口层保持抽象）
        ...
