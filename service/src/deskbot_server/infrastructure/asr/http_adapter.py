"""AsrPort 的 HTTP 实现：转写转发到外部 funasr 进程。

进程内实现（FunAsrAdapter）保留，本适配器按 config.yaml 的 asr.provider=external
启用；is_valid_text 是纯文本过滤逻辑，保持本地执行（无需远程往返）。
请求/响应格式遵循 ASR 外部服务协议 v1（docs/asr_protocol.md），
解析与错误提取走 infrastructure.asr.protocol。
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request

from deskbot_server.infrastructure.asr.protocol import (
    ERR_HTTP_ERROR,
    AsrProtocolError,
    extract_error,
    parse_transcribe_response,
)
from deskbot_server.infrastructure.asr.text_filter import is_asr_text_acceptable
from deskbot_server.model.settings import AsrTextFilterSettings

logger = logging.getLogger("deskbot-server")

TRANSCRIBE_TIMEOUT_S = 30.0  # 转写含音频上行与推理，宽限


class HttpAsrAdapter:
    """转外部 funasr 进程的 AsrPort 实现（无状态，可复用单例）。"""

    def __init__(self, base_url: str, text_filter: AsrTextFilterSettings) -> None:
        self.base_url = base_url.rstrip("/")
        self._text_filter = text_filter
        logger.info("[ASR] provider=external base_url=%s", self.base_url)

    def is_valid_text(self, text: str) -> bool:
        return is_asr_text_acceptable(
            text,
            min_len=self._text_filter.min_text_len,
            min_chinese_ratio=self._text_filter.min_chinese_ratio,
        )

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
        url = f"{self.base_url}/transcribe"
        return await asyncio.to_thread(self._post_pcm, url, pcm_bytes, sample_rate)

    def _post_pcm(self, url: str, pcm_bytes: bytes, sample_rate: int) -> str:
        import json

        req = urllib.request.Request(
            url,
            data=pcm_bytes,
            method="POST",
            headers={"Content-Type": "application/octet-stream", "X-Sample-Rate": str(sample_rate)},
        )
        try:
            with urllib.request.urlopen(req, timeout=TRANSCRIBE_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code, message = self._parse_error_body(exc)
            # AsrProtocolError 是 RuntimeError 子类：携带 code/http_status 供上层降级
            raise AsrProtocolError(message, code=code, http_status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"funasr 引擎不可达: {exc.reason}") from exc
        return str(parse_transcribe_response(payload)["text"])

    @staticmethod
    def _parse_error_body(exc: urllib.error.HTTPError) -> tuple[str, str]:
        """从错误响应体提取 (code, message)；非协议错误体回退通用 http_error。"""
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = None
        err = extract_error(payload)
        if err:
            return err
        detail = str(payload)[:200] if payload is not None else ""
        return ERR_HTTP_ERROR, f"funasr 引擎 HTTP {exc.code}: {detail}"
