# === externals/funasr 本地副本（funasr 独立化，勿直接删改此段）===
# 来源: src/deskbot_server/infrastructure/__init__.py
# 同步: 主服务改动后按 docs/external_services.md「funasr」节同步本副本并更新日期
# synced: 2026-08-30
from __future__ import annotations

from typing import Any

__all__ = ["DoubaoPhonemeTtsAdapter", "FunAsrAdapter", "OpenAiLlmAdapter", "WsDownlinkAdapter", "build_tts_adapter"]


def __getattr__(name: str) -> Any:
    if name == "DoubaoPhonemeTtsAdapter":
        from deskbot_server.infrastructure.tts.doubao_phoneme import DoubaoPhonemeTtsAdapter

        return DoubaoPhonemeTtsAdapter
    if name == "FunAsrAdapter":
        from deskbot_server.infrastructure.asr.funasr import FunAsrAdapter

        return FunAsrAdapter
    if name == "OpenAiLlmAdapter":
        from deskbot_server.infrastructure.llm.openai_compat import OpenAiLlmAdapter

        return OpenAiLlmAdapter
    if name == "WsDownlinkAdapter":
        from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter

        return WsDownlinkAdapter
    if name == "build_tts_adapter":
        from deskbot_server.infrastructure.tts.factory import build_tts_adapter

        return build_tts_adapter
    raise AttributeError(name)
