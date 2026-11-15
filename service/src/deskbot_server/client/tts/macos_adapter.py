"""realtime_agent-style macOS phrase synthesis, adapted to robot PCM/phonemes."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import wave
from collections import OrderedDict
from pathlib import Path
from weakref import WeakKeyDictionary

from deskbot_server.client.tts.doubao_phoneme_align import pcm_duration_ms, split_pcm_by_timed_phonemes
from deskbot_server.model.errors import ClientError
from deskbot_server.utils.audio_resample import resample_pcm16_mono
from deskbot_server.utils.phoneme_duration import text_to_phoneme_durations

_LIMITERS: WeakKeyDictionary = WeakKeyDictionary()
# Bound memory independently of conversation length; only short phrases enter.
_PCM_CACHE: OrderedDict = OrderedDict()
_CACHE_SIZE = 32


class MacOsTtsAdapter:
    def __init__(self, settings, overrides=None):
        self.sample_rate = int(settings.tts.sample_rate or 16000)
        raw = overrides or {}
        self.voice = str(raw.get("voice") or "Tingting")
        self.rate = int(raw.get("rate") or 230)
        if not 80 <= self.rate <= 400 or not 1 <= len(self.voice) <= 128:
            raise ValueError("invalid macOS voice/rate")

    async def synthesize_phoneme_segments(self, text):
        if sys.platform != "darwin":
            raise ClientError("macos TTS requires a macOS host")
        clean = (text or "").strip()
        if not clean:
            return self.sample_rate, []
        key = (self.sample_rate, self.voice, self.rate, clean)
        cached = _PCM_CACHE.get(key)
        if cached is not None:
            _PCM_CACHE.move_to_end(key)
            return self._segments(clean, cached)
        loop = asyncio.get_running_loop()
        limiter = _LIMITERS.setdefault(loop, asyncio.Semaphore(2))
        async with asyncio.timeout(15), limiter:
            with tempfile.TemporaryDirectory(prefix="deskbot-voice-") as directory:
                target = Path(directory) / "speech.wav"
                process = await asyncio.create_subprocess_exec(
                    "/usr/bin/say", "--file-format=WAVE", "--data-format=LEI16@24000",
                    "-r", str(self.rate), "-v", self.voice, "-o", str(target), "-f", "-",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, error = await process.communicate(clean.encode("utf-8"))
                    if process.returncode:
                        raise ClientError(f"macOS speech failed: {error.decode(errors='replace')[:160]}")
                    with wave.open(str(target), "rb") as source:
                        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 24000):
                            raise ClientError("unexpected macOS speech format")
                        pcm = source.readframes(source.getnframes())
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
        if not pcm:
            raise ClientError("macOS speech returned empty audio")
        if self.sample_rate != 24000:
            pcm = await asyncio.to_thread(resample_pcm16_mono, pcm, 24000, self.sample_rate)
        if len(pcm) <= self.sample_rate * 2 * 5:
            _PCM_CACHE[key] = pcm
            _PCM_CACHE.move_to_end(key)
            while len(_PCM_CACHE) > _CACHE_SIZE:
                _PCM_CACHE.popitem(last=False)
        return self._segments(clean, pcm)

    def _segments(self, text, pcm):
        timed = text_to_phoneme_durations(text, pcm_duration_ms(pcm, self.sample_rate))
        return self.sample_rate, split_pcm_by_timed_phonemes(pcm, self.sample_rate, timed)
