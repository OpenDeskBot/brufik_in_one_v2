"""Composition Root：装配 ChatService，并绑定 MVC Service 单例。"""

from __future__ import annotations

import logging

from deskbot_server.infrastructure.asr.doubao_adapter import DoubaoAsrAdapter
from deskbot_server.infrastructure.asr.http_adapter import HttpAsrAdapter
from deskbot_server.infrastructure.llm.openai_compat import OpenAiLlmAdapter
from deskbot_server.infrastructure.tts.factory import build_tts_adapter
from deskbot_server.model.settings import AppSettings
from deskbot_server.service.application.chat_service import ChatService
from deskbot_server.service.asr_service import AsrService
from deskbot_server.service.llm_service import LlmService
from deskbot_server.service.tts_service import TtsService

logger = logging.getLogger("deskbot-server")


def build_asr_adapter(settings: AppSettings):
    """按 config.yaml 的 asr.provider 装配：
    external=独立 funasr 进程（默认）；doubao=火山云端 ASR。
    进程内 FunASR 已于 v1.2.0 移除（funasr 完全独立化，见 externals/funasr）。
    """
    if settings.asr.provider == "doubao":
        return DoubaoAsrAdapter(settings)
    if settings.asr.provider != "external":
        logger.warning("[ASR] 未知 provider=%r，回落 external", settings.asr.provider)
    return HttpAsrAdapter(settings.asr.external_url, settings.asr.text_filter)


def build_chat_service(config: dict) -> ChatService:
    settings = AppSettings.from_config(config)
    asr = build_asr_adapter(settings)
    llm = OpenAiLlmAdapter(settings)
    tts = build_tts_adapter(settings)

    AsrService().bind(asr)
    LlmService().bind(llm)
    TtsService().bind(tts)

    return ChatService(settings, asr=asr, llm=llm, tts=tts)
