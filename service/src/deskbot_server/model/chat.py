from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LlmTurnResult:
    """一轮 LLM 主调用（含工具轮往返）的结果摘要。"""

    parsed: dict[str, Any]
    tools: list[Any] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    answer: str = ""
    system_prompt: str | None = None


@dataclass
class ChatTurnResult:
    """一轮 ASR→LLM→TTS/pb 的时序与结果摘要。"""

    llm_text: str | None = None
    llm_raw: str | None = None
    moves: list[Any] = field(default_factory=list)
    anims: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    servo: list[Any] = field(default_factory=list)
    need_reply: bool = True
    json_ok: bool = False
    t_llm_end: float | None = None
    t_tts_synth_end: float | None = None
    t_tts_end: float | None = None
    status: str = "ok"
    error: str | None = None
    voice_auto_reply_off: bool = False
    scenes: list[str] = field(default_factory=list)
    # 调试记录字段（turn_recorder 消费）
    system_prompt: str | None = None
    user_audio: str | None = None
    user_audio_ms: int | None = None
    bot_audio: str | None = None
    bot_audio_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
