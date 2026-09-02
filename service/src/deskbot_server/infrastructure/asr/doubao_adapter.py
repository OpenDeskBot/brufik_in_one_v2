"""AsrPort 的豆包实现：转写转发到火山 Seed-ASR 2.0 极速版（云端 API）。

配置全部来自设备级 overrides（resolve.py 从 asr_param 注入）；base 为纯默认构造
（DoubaoAsrConfig()，不含全局密钥）。is_valid_text 是纯文本过滤逻辑，本地执行。
"""

from __future__ import annotations

import logging

from deskbot_server.infrastructure.asr.doubao import DoubaoAsrConfig, merge_doubao_config, transcribe_doubao
from deskbot_server.infrastructure.asr.text_filter import is_asr_text_acceptable
from deskbot_server.model.settings import AppSettings

logger = logging.getLogger("deskbot-server")


class DoubaoAsrAdapter:
    """转豆包 ASR 2.0 的 AsrPort 实现（无状态，可每次调用构造）。

    overrides 为设备级参数（asr_param["doubao"]，已过滤掩码/空值），
    非空字段覆盖纯默认；无 key 时由 DoubaoAsrConfig.validate() 抛 RuntimeError。
    """

    def __init__(self, settings: AppSettings, overrides: dict[str, str] | None = None) -> None:
        self._text_filter = settings.asr.text_filter
        base = DoubaoAsrConfig()
        self._cfg = merge_doubao_config(base, overrides or {})
        self._cfg.validate()
        logger.info("[ASR] provider=doubao url=%s", self._cfg.url)

    def is_valid_text(self, text: str) -> bool:
        return is_asr_text_acceptable(
            text,
            min_len=self._text_filter.min_text_len,
            min_chinese_ratio=self._text_filter.min_chinese_ratio,
        )

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
        return await transcribe_doubao(pcm_bytes, sample_rate, self._cfg)
