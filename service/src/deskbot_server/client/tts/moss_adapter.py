"""TtsPort 的 HTTP 实现：转发到外部 MOSS-TTS-Nano 进程（moss-tts-nano，默认 TTS provider）。

MOSS 只返回 WAV 音频、不返回音素时间戳；引擎输出为原生 48kHz，此处先统一
降采样到 ``settings.tts.sample_rate``（默认 16k，与豆包下发一致，设备端
解码预算余量最大），口型用 ``utils.phoneme_duration`` 按文本 + 音频总时长
补充音素时间轴，再复用 ``doubao_phoneme_align`` 的 PCM 切分。
请求格式：multipart/form-data POST /api/generate（text + demo_id），响应 JSON
含 audio_base64（WAV，16-bit PCM）。无第三方 HTTP 依赖（stdlib urllib）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import OrderedDict
from typing import Any

import numpy as np

from deskbot_server.client.tts.doubao_phoneme_align import pcm_duration_ms, split_pcm_by_timed_phonemes
from deskbot_server.model.errors import ClientError
from deskbot_server.model.settings import AppSettings
from deskbot_server.utils.audio_resample import resample_pcm16_mono
from deskbot_server.utils.phoneme_duration import text_to_phoneme_durations

logger = logging.getLogger("deskbot-server")

TTS_ENGINE_BASE_URL = "http://127.0.0.1:9101"  # moss-tts-nano 服务端口（service.yaml）
GENERATE_TIMEOUT_S = 60.0  # 合成含模型推理，宽限
DEFAULT_DEMO_ID = "demo-1"  # demo.jsonl 首行音色（🇨🇳 欢迎关注模思智能）
MOSS_SAMPLE_RATE = 48000
# ONNX runtime 的 CPU 线程数：实测 M5 Pro 上 8 线程比默认 os.cpu_count() 快约 30%
# （短句稳态 0.64s vs 0.95s）。必须与 externals/moss-tts-nano/service.yaml 的
# --cpu-threads 8 保持一致——不一致会让服务端为不同线程数重建第二套 ONNX runtime
# （首个请求额外多约 2–3s）。
MOSS_CPU_THREADS = 8
# 短句 TTS 结果 LRU 缓存：重复的问候/应答语可跳过整个模型推理（~1s → ~0ms）。
# key=(text, demo_id, 下发采样率)；只缓存 ≤80 字，避免长音频占内存。
_TTS_CACHE_MAX = 64
_TTS_CACHE: OrderedDict[tuple[str, str, str, int], tuple[int, list[Any]]] = OrderedDict()

# 设备级参数白名单（resolve 共用）
MOSS_TTS_FIELDS = ("demo_id", "base_url")


class MossTtsClient:
    """外部 moss-tts-nano 进程的 TtsPort 客户端（无状态，可每次调用构造）。

    ``overrides`` 为设备级参数（tts_param["moss"]），优先级高于 config.yaml tts.extra。
    """

    def __init__(self, settings: AppSettings, overrides: dict[str, Any] | None = None) -> None:
        self._settings = settings
        extra = dict(settings.tts.extra or {})
        ov = dict(overrides or {})
        base_url = str(ov.get("base_url") or "").strip() or str(extra.get("base_url") or "").strip() or TTS_ENGINE_BASE_URL
        self.base_url = base_url.rstrip("/")
        self.demo_id = str(ov.get("demo_id") or "").strip() or str(extra.get("demo_id") or "").strip() or DEFAULT_DEMO_ID
        logger.info("[TTS] provider=moss-tts-nano base_url=%s demo_id=%s", self.base_url, self.demo_id)

    async def synthesize_phoneme_segments(self, text: str) -> tuple[int, list[Any]]:
        clean = (text or "").strip()
        if not clean:
            sr = int(self._settings.tts.sample_rate or MOSS_SAMPLE_RATE)
            return sr, []

        target = int(self._settings.tts.sample_rate or 0)
        cache_key = (self.base_url, self.demo_id, clean, target)
        cached = _TTS_CACHE.get(cache_key)
        if cached is not None:
            _TTS_CACHE.move_to_end(cache_key)
            sr, cached_segs = cached
            logger.info(
                "[TTS/moss] cache hit sr=%d n=%d text=%r",
                sr,
                len(cached_segs),
                clean[:80],
            )
            return sr, list(cached_segs)

        t0 = time.monotonic()
        payload = await asyncio.to_thread(self._generate, clean)
        t_http = time.monotonic()
        wav_b64 = str(payload.get("audio_base64") or "")
        if not wav_b64:
            raise ClientError(f"moss-tts-nano 无音频: {clean!r}")
        try:
            wav = base64.b64decode(wav_b64)
        except Exception as exc:
            raise ClientError(f"moss-tts-nano audio_base64 解码失败: {exc}") from exc
        pcm, sr = _wav_to_pcm16_mono(wav)
        if not pcm:
            raise ClientError(f"moss-tts-nano WAV 无 PCM 数据: {clean!r}")
        t_decode = time.monotonic()

        # 引擎原生 48k → 统一下发采样率（默认 16k，与豆包一致）：降采样后再切分，
        # 避免设备端解码突发超出 I2S DMA 环覆盖导致的块边界断音
        # （见 utils/audio_resample 模块说明）。
        if 0 < target < sr:
            pcm = await asyncio.to_thread(resample_pcm16_mono, pcm, sr, target)
            logger.info("[TTS/moss] 引擎 %dHz → 统一下发 %dHz", sr, target)
            sr = target
        t_resample = time.monotonic()

        total_ms = pcm_duration_ms(pcm, sr)
        timed = text_to_phoneme_durations(clean, total_ms)
        segs = split_pcm_by_timed_phonemes(pcm, sr, timed)
        t_split = time.monotonic()

        if len(clean) <= 80:
            # 缓存段对象本身（只读；TtsService 会转 dict 后下发，调用方不原地修改）
            _TTS_CACHE[cache_key] = (sr, list(segs))
            _TTS_CACHE.move_to_end(cache_key)
            while len(_TTS_CACHE) > _TTS_CACHE_MAX:
                _TTS_CACHE.popitem(last=False)

        logger.info(
            "[TTS/moss] 音素分片 n=%d pcm_bytes=%d sr=%d "
            "timing http=%dms decode=%dms resample=%dms split=%dms total=%dms text=%r",
            len(segs),
            len(pcm),
            sr,
            int((t_http - t0) * 1000),
            int((t_decode - t_http) * 1000),
            int((t_resample - t_decode) * 1000),
            int((t_split - t_resample) * 1000),
            int((t_split - t0) * 1000),
            clean[:80] + ("…" if len(clean) > 80 else ""),
        )
        return sr, segs

    # ---------- 同步 HTTP（asyncio.to_thread 中执行） ----------

    def _generate(self, text: str) -> dict[str, Any]:
        boundary = uuid.uuid4().hex
        body = _multipart_form(
            {
                "text": text,
                "demo_id": self.demo_id,
                # 显式线程数：服务端默认用 os.cpu_count()，实测反而比 8 线程慢
                "cpu_threads": str(MOSS_CPU_THREADS),
            },
            boundary,
        )
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
            raise ClientError(_http_error_message(exc)) from exc
        except urllib.error.URLError as exc:
            raise ClientError(f"moss-tts-nano 引擎不可达: {exc.reason}") from exc
        if not isinstance(payload, dict):
            raise ClientError(f"moss-tts-nano 响应异常: {payload!r}")
        return payload


def _multipart_form(fields: dict[str, str], boundary: str) -> bytes:
    """构造 multipart/form-data body（纯 stdlib，text 为 UTF-8）。"""
    buf = bytearray()
    for name, value in fields.items():
        buf += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        buf += str(value).encode("utf-8")
        buf += b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf)


def _wav_to_pcm16_mono(wav: bytes) -> tuple[bytes, int]:
    """解 MOSS 返回的 WAV → (s16le mono PCM, sample_rate)。

    多声道取各声道均值降为单声道（设备端链路按 s16le mono 切分/播放）。
    """
    with wave.open(io.BytesIO(wav), "rb") as f:
        if f.getcomptype() != "NONE":
            raise ClientError(f"moss-tts-nano WAV 非 PCM 编码: {f.getcomptype()!r}")
        sampwidth = f.getsampwidth()
        channels = f.getnchannels()
        sr = int(f.getframerate())
        frames = f.readframes(f.getnframes())
    if sampwidth != 2:
        raise ClientError(f"moss-tts-nano WAV 非 16-bit: {sampwidth * 8}bit")
    if sr <= 0 or not frames:
        raise ClientError("moss-tts-nano WAV 无有效音频数据")
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


MossTtsAdapter = MossTtsClient
