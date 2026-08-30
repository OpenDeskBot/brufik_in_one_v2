"""AsrPort 的豆包实现：转写转发到火山一句话识别 v1（云端 API）。

按 config.yaml 的 asr.provider=doubao 启用；配置走 env（见 doubao.py）。
is_valid_text 是纯文本过滤逻辑，保持本地执行（无需远程往返）。
"""

from __future__ import annotations

import logging

from deskbot_server.infrastructure.asr.doubao import load_doubao_asr_config, transcribe_doubao
from deskbot_server.infrastructure.asr.text_filter import is_asr_text_acceptable
from deskbot_server.model.settings import AppSettings

logger = logging.getLogger("deskbot-server")


class DoubaoAsrAdapter:
    """转豆包 ASR 的 AsrPort 实现（无状态，可复用单例；配置 env 热读）。"""

    def __init__(self, settings: AppSettings) -> None:
        self._text_filter = settings.asr.text_filter
        cfg = load_doubao_asr_config()
        cfg.validate()
        self._cfg = cfg
        logger.info("[ASR] provider=doubao url=%s", cfg.url)

    def is_valid_text(self, text: str) -> bool:
        return is_asr_text_acceptable(
            text,
            min_len=self._text_filter.min_text_len,
            min_chinese_ratio=self._text_filter.min_chinese_ratio,
        )

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
        return await transcribe_doubao(pcm_bytes, sample_rate, self._cfg)
