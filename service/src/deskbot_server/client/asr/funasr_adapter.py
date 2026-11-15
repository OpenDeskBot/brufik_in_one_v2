"""AsrPort 的 HTTP 实现：转写转发到外部 funasr 进程（默认 ASR provider）。

is_valid_text 是纯文本过滤逻辑，保持本地执行（无需远程往返）。
请求/响应格式遵循 ASR 外部服务协议 v1（docs/asr_protocol.md），
解析与错误提取走 infrastructure.asr.protocol。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

from deskbot_server.client.asr.protocol import (
    ERR_HTTP_ERROR,
    AsrProtocolError,
    extract_error,
    parse_transcribe_response,
)
from deskbot_server.client.asr.text_filter import is_asr_text_acceptable
from deskbot_server.model.errors import ClientError
from deskbot_server.model.settings import AsrTextFilterSettings

logger = logging.getLogger("deskbot-server")

TRANSCRIBE_TIMEOUT_S = 30.0  # 转写含音频上行与推理，宽限


class FunAsrClient:
    """外部 funasr 进程的 AsrPort 客户端（无状态，可每次调用构造）。"""

    def __init__(self, base_url: str, text_filter: AsrTextFilterSettings) -> None:
        self.base_url = base_url.rstrip("/")
        self._text_filter = text_filter
        logger.info("[ASR] provider=funasr base_url=%s", self.base_url)

    def is_valid_text(self, text: str) -> bool:
        return is_asr_text_acceptable(
            text,
            min_len=self._text_filter.min_text_len,
            min_chinese_ratio=self._text_filter.min_chinese_ratio,
        )

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
        url = f"{self.base_url}/transcribe"
        return await asyncio.to_thread(self._post_pcm, url, pcm_bytes, sample_rate)

    async def transcribe_preview(self, pcm_bytes: bytes, sample_rate: int) -> str:
        if sample_rate == 8000:
            # The preview is band-limited speech. Avoid importing librosa/JIT
            # in the external ASR worker on the first 8kHz request.
            from deskbot_server.utils.audio_resample import upsample_pcm16_mono
            pcm_bytes = upsample_pcm16_mono(pcm_bytes, 8000, 16000)
            sample_rate = 16000
        return await asyncio.to_thread(self._post_pcm, f"{self.base_url}/transcribe",
                                       pcm_bytes, sample_rate, timeout_s=.6)

    def _post_pcm(self, url: str, pcm_bytes: bytes, sample_rate: int, *, timeout_s: float = TRANSCRIBE_TIMEOUT_S) -> str:
        req = urllib.request.Request(
            url,
            data=pcm_bytes,
            method="POST",
            headers={"Content-Type": "application/octet-stream", "X-Sample-Rate": str(sample_rate)},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code, message = self._parse_error_body(exc)
            # AsrProtocolError 是 RuntimeError 子类：携带 code/http_status 供上层降级
            raise AsrProtocolError(message, code=code, http_status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise ClientError("funasr 引擎不可达", kind="connection", provider="funasr") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ClientError("funasr 响应不是有效 JSON", kind="mapping", provider="funasr") from exc
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


FunAsrAdapter = FunAsrClient
