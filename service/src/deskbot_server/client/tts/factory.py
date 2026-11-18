from __future__ import annotations

import logging

from deskbot_server.client.tts.doubao_phoneme import DoubaoPhonemeTtsAdapter
from deskbot_server.client.tts.macos_adapter import MacOsTtsAdapter
from deskbot_server.client.tts.moss_adapter import MossTtsAdapter
from deskbot_server.model.settings import AppSettings
from deskbot_server.ports.tts import TtsPort

logger = logging.getLogger("deskbot-server")

# 默认 TTS provider：独立管理服务 moss-tts-nano（HTTP 9101）
DEFAULT_TTS_PROVIDER = "moss-tts-nano"
SUPPORTED_TTS_PROVIDERS = ("moss-tts-nano", "doubao", "macos")


def build_tts_adapter(settings: AppSettings) -> TtsPort:
    """按 ``settings.tts.provider`` 选择 TTS 后端（bootstrap 默认装配 / 测试用）。

    设备级运行时解析请用 ``client/tts/resolve.py::resolve_tts_adapter``。
    - moss-tts-nano：本地独立进程（默认），只返回音频，口型由 ROM 按播放响度生成
    - doubao：火山云端 TTS，音频分片后下发，口型由 ROM 生成
    """
    provider = (settings.tts.provider or DEFAULT_TTS_PROVIDER).strip().lower()
    if provider == "macos":
        return MacOsTtsAdapter(settings)
    if provider == "doubao":
        logger.info("[TTS] provider=doubao（时间戳/拼音均分音素口型）")
        return DoubaoPhonemeTtsAdapter(settings)
    if provider != DEFAULT_TTS_PROVIDER:
        logger.warning("[TTS] 未知 provider=%r，使用 %s", provider, DEFAULT_TTS_PROVIDER)
    return MossTtsAdapter(settings)
