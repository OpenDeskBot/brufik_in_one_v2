"""insightface-engine（外部 fr 服务）的 /detect HTTP 客户端。

请求/响应格式遵循 fr 外部服务契约（docs/external_services.md）：
POST /detect，body=JPEG bytes → {"faces": [{landmarks, embedding, ...}, ...]}。
实现风格镜像 funasr_adapter：urllib + asyncio.to_thread，无新依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("deskbot-server")

DEFAULT_DETECT_TIMEOUT_S = 10.0


class FrHttpClient:
    """insightface-engine 的 /detect 客户端（无状态，可每次调用构造）。

    异常语义：引擎不可达 / HTTP 错误 / 响应异常 → RuntimeError（上层回落进程内池）。
    """

    def __init__(self, base_url: str, timeout_s: float = DEFAULT_DETECT_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = max(1.0, float(timeout_s))
        logger.info("[face] provider=http base_url=%s timeout_s=%.1f", self.base_url, self.timeout_s)

    async def detect(self, jpeg_bytes: bytes) -> list[dict]:
        """JPEG → [{landmarks, embedding, face_descriptor, descriptor_kind, image_w, image_h}, ...]。"""
        if not jpeg_bytes:
            return []
        return await asyncio.to_thread(self._post_detect, jpeg_bytes)

    def _post_detect(self, jpeg_bytes: bytes) -> list[dict]:
        req = urllib.request.Request(
            f"{self.base_url}/detect",
            data=jpeg_bytes,
            method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"insightface-engine HTTP {exc.code}: {self._error_detail(exc)}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"insightface-engine 不可达: {exc.reason}") from exc
        faces = payload.get("faces") if isinstance(payload, dict) else None
        if not isinstance(faces, list):
            raise RuntimeError(f"insightface-engine /detect 响应异常: {str(payload)[:200]}")
        return faces

    @staticmethod
    def _error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            return ""
        try:
            parsed = json.loads(body)
            return str(parsed.get("error") or body)[:200]
        except Exception:
            return body[:200]
