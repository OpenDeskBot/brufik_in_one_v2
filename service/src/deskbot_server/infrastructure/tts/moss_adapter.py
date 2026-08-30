"""TtsPort 的 HTTP 实现：转发到外部 MOSS-TTS-Nano 进程（moss-tts-nano，默认 TTS provider）。

MOSS 只返回 WAV 音频、不返回音素时间戳，口型用 ``utils.phoneme_duration``
按文本 + 音频总时长补充音素时间轴，再复用 ``doubao_phoneme_align`` 的 PCM 切分。
请求格式：multipart/form-data POST /api/generate（text + demo_id），响应 JSON
含 audio_base64（WAV，16-bit PCM）。无第三方 HTTP 依赖（stdlib urllib）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import urllib.error
import urllib.request
import uuid
import wave
from typing import Any

import numpy as np

from deskbot_server.infrastructure.tts.doubao_phoneme_align import pcm_duration_ms, split_pcm_by_timed_phonemes
from deskbot_server.model.settings import AppSettings
from deskbot_server.utils.phoneme_duration import text_to_phoneme_durations

logger = logging.getLogger("deskbot-server")

TTS_ENGINE_BASE_URL = "http://127.0.0.1:9101"  # moss-tts-nano 服务端口（service.yaml）
GENERATE_TIMEOUT_S = 60.0  # 合成含模型推理，宽限
DEFAULT_DEMO_ID = "demo-1"  # demo.jsonl 首行音色（🇨🇳 欢迎关注模思智能）
MOSS_SAMPLE_RATE = 48000


class MossTtsAdapter:
    """外部 moss-tts-nano 进程的 TtsPort 客户端（无状态，可每次调用构造）。"""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        extra = dict(settings.tts.extra or {})
        self.base_url = str(extra.get("base_url") or "").strip() or TTS_ENGINE_BASE_URL
        self.base_url = self.base_url.rstrip("/")
        self.demo_id = str(extra.get("demo_id") or "").strip() or DEFAULT_DEMO_ID
        logger.info("[TTS] provider=moss-tts-nano base_url=%s demo_id=%s", self.base_url, self.demo_id)

    async def synthesize_phoneme_segments(self, text: str) -> tuple[int, list[Any]]:
        clean = (text or "").strip()
        if not clean:
            sr = int(self._settings.tts.sample_rate or MOSS_SAMPLE_RATE)
            return sr, []

        payload = await asyncio.to_thread(self._generate, clean)
        wav_b64 = str(payload.get("audio_base64") or "")
        if not wav_b64:
            raise RuntimeError(f"moss-tts-nano 无音频: {clean!r}")
        try:
            wav = base64.b64decode(wav_b64)
        except Exception as exc:
            raise RuntimeError(f"moss-tts-nano audio_base64 解码失败: {exc}") from exc
        pcm, sr = _wav_to_pcm16_mono(wav)
        if not pcm:
            raise RuntimeError(f"moss-tts-nano WAV 无 PCM 数据: {clean!r}")

        total_ms = pcm_duration_ms(pcm, sr)
        timed = text_to_phoneme_durations(clean, total_ms)
        segs = split_pcm_by_timed_phonemes(pcm, sr, timed)
        logger.info(
            "[TTS/moss] 音素分片 n=%d pcm_bytes=%d sr=%d text=%r",
            len(segs),
            len(pcm),
            sr,
            clean[:80] + ("…" if len(clean) > 80 else ""),
        )
        return sr, segs

    # ---------- 同步 HTTP（asyncio.to_thread 中执行） ----------

    def _generate(self, text: str) -> dict[str, Any]:
        boundary = uuid.uuid4().hex
        body = _multipart_form({"text": text, "demo_id": self.demo_id}, boundary)
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=GENERATE_TIMEOUT_S) as resp:  # noqa: S310 (127.0.0.1)
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_http_error_message(exc)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"moss-tts-nano 引擎不可达: {exc.reason}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"moss-tts-nano 响应异常: {payload!r}")
        return payload


def _multipart_form(fields: dict[str, str], boundary: str) -> bytes:
    """构造 multipart/form-data body（纯 stdlib，text 为 UTF-8）。"""
    buf = bytearray()
    for name, value in fields.items():
        buf += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        buf += str(value).encode("utf-8")
        buf += b"\r\n"
    buf += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(buf)


def _wav_to_pcm16_mono(wav: bytes) -> tuple[bytes, int]:
    """解 MOSS 返回的 WAV → (s16le mono PCM, sample_rate)。

    多声道取各声道均值降为单声道（设备端链路按 s16le mono 切分/播放）。
    """
    with wave.open(io.BytesIO(wav), "rb") as f:
        if f.getcomptype() != "NONE":
            raise RuntimeError(f"moss-tts-nano WAV 非 PCM 编码: {f.getcomptype()!r}")
        sampwidth = f.getsampwidth()
        channels = f.getnchannels()
        sr = int(f.getframerate())
        frames = f.readframes(f.getnframes())
    if sampwidth != 2:
        raise RuntimeError(f"moss-tts-nano WAV 非 16-bit: {sampwidth * 8}bit")
    if sr <= 0 or not frames:
        raise RuntimeError("moss-tts-nano WAV 无有效音频数据")
    n = len(frames) // (2 * channels) * 2 * channels
    pcm = np.frombuffer(frames[:n], dtype="<i2").reshape(-1, channels)
    if channels > 1:
        pcm = pcm.mean(axis=1).astype("<i2")
    return pcm.tobytes(), sr


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """提取服务端 JSON 错误体（MOSS 错误响应形如 ``{"error": ...}``）。"""
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    except Exception:
        payload = None
    detail = str(payload)[:300] if payload else ""
    return f"moss-tts-nano 引擎 HTTP {exc.code}: {detail}"
