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
    # 逐次 LLM 调用明细：{n, model, ms, text, truncated}（工具轮会多次）
    llm_calls: list[dict[str, Any]] = field(default_factory=list)


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
    # 逐次 LLM 调用明细透传（run_chat_turn 自 LlmTurnResult 拷贝）
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    # 本轮使用的 system prompt 全文（首轮捕获）——调试展示用
    system_prompt: str | None = None
    # 本轮语音输入时的图像识别文本（人脸行；仅语音轮有值）
    face_sight: str | None = None
    # 本轮语音输入时的声纹识别文本（说话人行；仅语音轮有值）
    voice_sight: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
