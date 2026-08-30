# === externals/funasr 本地副本（funasr 独立化，勿直接删改此段）===
# 来源: src/deskbot_server/infrastructure/asr/protocol.py
# 同步: 主服务改动后按 docs/external_services.md「funasr」节同步本副本并更新日期
# synced: 2026-08-30
"""ASR 外部服务协议 v1（见 docs/asr_protocol.md）。

一模块服务两端：
- 服务端（externals/*-engine server.py）：解析 /transcribe 请求（PCM/WAV → 归一化
  PCM + sr）、构造标准成功/失败响应。
- 客户端（FunAsrAdapter 等）：校验成功响应结构、提取错误码。

纯 dict 级，无 pydantic 依赖。协议演进（加可选字段）只改 ok_response 与
parse_transcribe_response。
"""

from __future__ import annotations

import io
import wave
from typing import Any

DEFAULT_SAMPLE_RATE = 16000
MAX_AUDIO_SECONDS = 60.0  # 超限 413 audio_too_large（可选实现，解析层默认校验）

CONTENT_TYPE_PCM = "application/octet-stream"  # raw PCM int16 LE + X-Sample-Rate
CONTENT_TYPE_WAV = "audio/wav"  # WAV 容器，采样率/声道自描述

# 错误码（与 docs/asr_protocol.md 错误码清单一致）
ERR_EMPTY_PCM = "empty_pcm"
ERR_INVALID_SAMPLE_RATE = "invalid_sample_rate"
ERR_INVALID_WAV = "invalid_wav"
ERR_AUDIO_TOO_LARGE = "audio_too_large"
ERR_UNSUPPORTED_MEDIA = "unsupported_media_type"
ERR_MODEL_NOT_READY = "model_not_ready"
ERR_TRANSCRIBE_FAILED = "transcribe_failed"
ERR_INVALID_RESPONSE = "invalid_response"
ERR_HTTP_ERROR = "http_error"  # 客户端兜底：引擎返回非协议错误体（如网关 502 HTML）

# HTTP 状态码（错误码清单一一对应）
_STATUS_FOR_CODE = {
    ERR_EMPTY_PCM: 400,
    ERR_INVALID_SAMPLE_RATE: 400,
    ERR_INVALID_WAV: 400,
    ERR_AUDIO_TOO_LARGE: 413,
    ERR_UNSUPPORTED_MEDIA: 415,
    ERR_MODEL_NOT_READY: 503,
    ERR_TRANSCRIBE_FAILED: 500,
    ERR_INVALID_RESPONSE: 502,
    ERR_HTTP_ERROR: 502,
}


class AsrProtocolError(Exception):
    """协议错误：服务端捕获后转标准错误响应，客户端直接上抛（RuntimeError 子类）。

    Attributes:
        http_status: 应返回的 HTTP 状态码。
        code: 错误码（见协议错误码清单）。
    """

    def __init__(self, message: str, *, code: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status if http_status is not None else _STATUS_FOR_CODE.get(code, 500)


# ---------- 服务端：请求解析 ----------


def parse_transcribe_request(
    body: bytes,
    content_type: str,
    sample_rate_header: str | int | None = None,
    *,
    max_seconds: float = MAX_AUDIO_SECONDS,
) -> tuple[bytes, int]:
    """解析 /transcribe 请求 → 归一化 (pcm int16 LE bytes, sample_rate)。

    - ``application/octet-stream``：body 即 PCM，采样率取 ``X-Sample-Rate``（缺省 16000）
    - ``audio/wav``：采样率/声道自描述，忽略 header；非 PCM 编码或非单声道 → ``invalid_wav``
    - 其他 Content-Type → ``unsupported_media_type``；音频超时 → ``audio_too_large``

    Raises:
        AsrProtocolError: 请求不符合协议。
    """
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if not body:
        raise AsrProtocolError("empty pcm", code=ERR_EMPTY_PCM)

    if ctype == CONTENT_TYPE_WAV:
        pcm, sample_rate = _extract_pcm_from_wav(body)
    elif ctype in (CONTENT_TYPE_PCM, ""):
        sample_rate = _parse_sample_rate(sample_rate_header)
        pcm = body
    else:
        raise AsrProtocolError(
            f"unsupported media type: {ctype or '(missing)'}",
            code=ERR_UNSUPPORTED_MEDIA,
        )

    duration_s = len(pcm) / 2 / max(1, sample_rate)
    if max_seconds > 0 and duration_s > max_seconds:
        raise AsrProtocolError(
            f"audio too large: {duration_s:.1f}s > {max_seconds:.0f}s",
            code=ERR_AUDIO_TOO_LARGE,
        )
    return pcm, sample_rate


def _parse_sample_rate(header: str | int | None) -> int:
    if header is None or (isinstance(header, str) and not header.strip()):
        return DEFAULT_SAMPLE_RATE
    try:
        sr = int(header)
    except (TypeError, ValueError):
        sr = 0
    if sr <= 0:
        raise AsrProtocolError(f"invalid sample rate: {header!r}", code=ERR_INVALID_SAMPLE_RATE)
    return sr


def _extract_pcm_from_wav(body: bytes) -> tuple[bytes, int]:
    """解 WAV 容器：要求 PCM 编码、16-bit、单声道；数据区与采样率自描述。"""
    try:
        with wave.open(io.BytesIO(body), "rb") as wav:
            if wav.getcomptype() != "NONE":
                raise AsrProtocolError("wav is not PCM encoded", code=ERR_INVALID_WAV)
            if wav.getnchannels() != 1:
                raise AsrProtocolError(
                    f"wav must be mono, got {wav.getnchannels()} channels", code=ERR_INVALID_WAV
                )
            if wav.getsampwidth() != 2:
                raise AsrProtocolError(
                    f"wav must be 16-bit PCM, got {wav.getsampwidth() * 8}-bit", code=ERR_INVALID_WAV
                )
            sample_rate = wav.getframerate()
            if sample_rate <= 0:
                raise AsrProtocolError("wav has invalid sample rate", code=ERR_INVALID_WAV)
            pcm = wav.readframes(wav.getnframes())
    except AsrProtocolError:
        raise
    except Exception as exc:
        raise AsrProtocolError(f"cannot parse wav: {exc}", code=ERR_INVALID_WAV) from exc
    if not pcm:
        raise AsrProtocolError("wav contains no audio data", code=ERR_INVALID_WAV)
    return pcm, sample_rate


# ---------- 服务端：响应构造 ----------


def ok_response(
    text: str,
    *,
    language: str | None = None,
    confidence: float | None = None,
    elapsed_ms: int | None = None,
) -> dict[str, Any]:
    """构造标准成功响应；可选元数据仅非 None 时写入（协议加字段向后兼容）。"""
    payload: dict[str, Any] = {"text": text}
    if language is not None:
        payload["language"] = language
    if confidence is not None:
        payload["confidence"] = confidence
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    return payload


def error_response(code: str, message: str) -> dict[str, Any]:
    """构造标准错误响应体。"""
    return {"error": {"code": code, "message": message}}


def error_status(code: str) -> int:
    """错误码 → HTTP 状态码（服务端转响应用）。"""
    return _STATUS_FOR_CODE.get(code, 500)


# ---------- 客户端：响应解析 ----------


def parse_transcribe_response(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化成功响应；缺 ``text`` 判响应异常。

    Raises:
        AsrProtocolError: 响应缺少必填字段（code=invalid_response）。
    """
    if not isinstance(payload, dict) or "text" not in payload:
        raise AsrProtocolError(f"ASR 引擎响应缺少 text 字段: {payload!r}", code=ERR_INVALID_RESPONSE)
    text = payload.get("text")
    if not isinstance(text, str):
        raise AsrProtocolError(f"ASR 引擎响应 text 非字符串: {text!r}", code=ERR_INVALID_RESPONSE)
    out: dict[str, Any] = {"text": text}
    for key in ("language", "confidence", "elapsed_ms"):
        if key in payload:
            out[key] = payload[key]
    return out


def extract_error(payload: Any) -> tuple[str, str] | None:
    """从响应提取 (code, message)；非错误结构返回 None。"""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        return str(err.get("code", "")), str(err.get("message", ""))
    return None
