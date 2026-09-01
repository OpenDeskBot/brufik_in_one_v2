from __future__ import annotations

import logging

from deskbot_server.infrastructure.tts.doubao_phoneme import DoubaoPhonemeTtsAdapter
from deskbot_server.infrastructure.tts.moss_adapter import MossTtsAdapter
from deskbot_server.model.settings import AppSettings
from deskbot_server.ports.tts import TtsPort

logger = logging.getLogger("deskbot-server")

# 默认 TTS provider：独立管理服务 moss-tts-nano（HTTP 9101）
DEFAULT_TTS_PROVIDER = "moss-tts-nano"
SUPPORTED_TTS_PROVIDERS = ("moss-tts-nano", "doubao")


def build_tts_adapter(settings: AppSettings) -> TtsPort:
    """按 ``settings.tts.provider`` 选择 TTS 后端（bootstrap 默认装配 / 测试用）。

    设备级运行时解析请用 ``infrastructure/tts/resolve.py::resolve_tts_adapter``。
    - moss-tts-nano：本地独立进程（默认），只返回音频，口型由 phoneme_duration 补充
    - doubao：火山云端 TTS，时间戳/拼音均分音素口型
    """
    provider = (settings.tts.provider or DEFAULT_TTS_PROVIDER).strip().lower()
    if provider == "doubao":
        logger.info("[TTS] provider=doubao（时间戳/拼音均分音素口型）")
        return DoubaoPhonemeTtsAdapter(settings)
    if provider != DEFAULT_TTS_PROVIDER:
        logger.warning("[TTS] 未知 provider=%r，使用 %s", provider, DEFAULT_TTS_PROVIDER)
    return MossTtsAdapter(settings)
