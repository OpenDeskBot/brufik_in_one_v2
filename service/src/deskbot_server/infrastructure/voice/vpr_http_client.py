"""wespeaker-resnet34（外部 vpr 服务）的 /voiceprint HTTP 客户端。

请求/响应格式遵循 vpr 外部服务契约（docs/external_services.md）：
POST /voiceprint，JSON {"audio_base64": <PCM int16 LE 或 WAV 的 base64>, "sample_rate": N}
→ {"embedding": [256 维], "dim": 256}；错误统一 {"error": {code, message}}。
实现风格镜像 fr_http_client / funasr_adapter：urllib + asyncio.to_thread，无新依赖。
主服务不做 /compare —— 说话人比对走本机声纹档案余弦（与人脸同哲学）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("deskbot-server")

DEFAULT_VPR_TIMEOUT_S = 5.0
VPR_EMBEDDING_DIM = 256


class VprHttpError(RuntimeError):
    """vpr-engine 调用错误。``code`` 透传引擎错误码（AUDIO_TOO_SHORT / MODEL_NOT_READY 等）。"""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class VprHttpClient:
    """wespeaker-resnet34 的 /voiceprint 客户端（无状态，可每次调用构造）。

    异常语义：一律抛 VprHttpError（上层静默降级，不进对话链路）。
    """

    def __init__(self, base_url: str, timeout_s: float = DEFAULT_VPR_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = max(1.0, float(timeout_s))
        logger.info("[vpr] provider=http base_url=%s timeout_s=%.1f", self.base_url, self.timeout_s)

    async def embedding(self, pcm_bytes: bytes, sample_rate: int = 16000) -> list[float]:
        """PCM int16 LE mono → 256 维 speaker embedding。"""
        if not pcm_bytes:
            raise VprHttpError("音频为空", code="INVALID_AUDIO")
        return await asyncio.to_thread(self._post_voiceprint, pcm_bytes, int(sample_rate))

    def _post_voiceprint(self, pcm_bytes: bytes, sample_rate: int) -> list[float]:
        body = json.dumps(
            {
                "audio_base64": base64.b64encode(pcm_bytes).decode("ascii"),
                "sample_rate": sample_rate,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/voiceprint",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code, detail = self._error_parts(exc)  # 只读一次 body（read 会消费流）
            raise VprHttpError(f"vpr-engine HTTP {exc.code}: {detail}", code=code) from exc
        except urllib.error.URLError as exc:
            raise VprHttpError(f"vpr-engine 不可达: {exc.reason}") from exc

        embedding = payload.get("embedding") if isinstance(payload, dict) else None
        if not isinstance(embedding, list) or len(embedding) != VPR_EMBEDDING_DIM:
            raise VprHttpError(f"vpr-engine /voiceprint 响应异常: {str(payload)[:200]}")
        try:
            return [float(x) for x in embedding]
        except (TypeError, ValueError) as exc:
            raise VprHttpError(f"vpr-engine /voiceprint embedding 非法: {exc}") from exc

    @staticmethod
    def _error_parts(exc: urllib.error.HTTPError) -> tuple[str | None, str]:
        """读一次错误 body → (code, detail)；body 读取会消费流，只允许调用一次。"""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            return None, str(exc)[:200]
        try:
            parsed = json.loads(body)
        except Exception:
            return None, body[:200]
        if not isinstance(parsed, dict):
            return None, body[:200]
        err = parsed.get("error")
        if isinstance(err, dict):
            code = str(err["code"]) if err.get("code") else None
            return code, str(err.get("message") or err)[:200]
        return None, body[:200]
