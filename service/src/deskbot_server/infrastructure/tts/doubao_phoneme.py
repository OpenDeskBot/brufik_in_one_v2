from __future__ import annotations

import asyncio
import logging
from typing import Any

from deskbot_server.infrastructure.tts.doubao import load_doubao_tts_config, synthesize_doubao_tts
from deskbot_server.infrastructure.tts.doubao_phoneme_align import build_phoneme_segments
from deskbot_server.model.settings import AppSettings
from deskbot_server.ports.tts import PhonemeSegment
from deskbot_server.utils.audio_resample import resample_pcm16_mono

logger = logging.getLogger("deskbot-server")


class DoubaoPhonemeTtsAdapter:
    """豆包 TTS 适配器：时间戳 / 拼音均分 → 音素分片（口型）。

    ``overrides`` 为设备级参数（tts_param["doubao"]），优先级高于全局 env。
    云端返回的 PCM 若高于统一下发采样率（settings.tts.sample_rate，默认 16k），
    先降采样再按时间戳切分（与 moss 链路同一口径）。
    """

    def __init__(self, settings: AppSettings, overrides: dict[str, Any] | None = None) -> None:
        self._settings = settings
        self._overrides = dict(overrides or {})

    async def synthesize_phoneme_segments(self, text: str) -> tuple[int, list[PhonemeSegment]]:
        clean = (text or "").strip()
        if not clean:
            sr = int(self._settings.tts.sample_rate or 16000)
            return sr, []

        cfg = load_doubao_tts_config(self._settings.tts.extra, self._overrides)
        result = await synthesize_doubao_tts(clean, cfg)
        pcm = bytes(result.pcm or b"")
        sr = int(result.sample_rate or cfg.sample_rate or 16000)
        if not pcm:
            raise RuntimeError(f"豆包 TTS 无 PCM: {clean!r}")

        target = int(self._settings.tts.sample_rate or 0)
        if 0 < target < sr:
            pcm = await asyncio.to_thread(resample_pcm16_mono, pcm, sr, target)
            logger.info("[TTS/doubao] 云端 %dHz → 统一下发 %dHz", sr, target)
            sr = target

        segs = build_phoneme_segments(
            text=clean, pcm=pcm, sample_rate=sr, sentence_end=result.sentence_end, subtitles=result.subtitles
        )
        logger.info(
            "[TTS/doubao] 音素分片 n=%d pcm_bytes=%d elapsed_ms=%d text=%r",
            len(segs),
            len(pcm),
            result.elapsed_ms,
            clean[:80] + ("…" if len(clean) > 80 else ""),
        )
        return sr, segs
