"""Single-flight rolling ASR for explicit stop commands during speaker playback."""
from __future__ import annotations

import asyncio
import logging
import re
import time

logger = logging.getLogger("deskbot-server")


def is_interrupt_command(text: str) -> bool:
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or ""))
    return text == "停" or bool(re.search(
        r"停一下|停下|先停|暂停|听一下|别说|不要说|别讲|打住|等一下|等下|等等|安静|不是|不对", text
    ))


class BargeInProbe:
    def __init__(self, transcribe, current_token, confirm, spoken_fragment):
        self._transcribe = transcribe
        self._current_token = current_token
        self._confirm = confirm
        self._spoken_fragment = spoken_fragment
        self._task = None
        self._last_at = 0.0
        self._confirmed = None
        self._closed = False
        self._raw = bytearray()
        self._raw_req = None
        self._raw_sr = 8000

    def feed_raw(self, pcm: bytes, sample_rate: int = 8000) -> None:
        token = self._current_token()
        if not pcm or token is None:
            self._raw.clear()
            self._raw_req = None
            return
        if token[-1] != self._raw_req:
            self._raw.clear()
            self._raw_req = token[-1]
        self._raw_sr = sample_rate
        self._raw.extend(pcm)
        cap = sample_rate * 900 // 1000 * 2
        if len(self._raw) > cap:
            del self._raw[:-cap]
        if len(self._raw) >= sample_rate:  # at least 500ms, without VAD dependence
            self.feed(bytes(self._raw), sample_rate)

    def raw_snapshot(self) -> tuple[bytes, int]:
        return bytes(self._raw), self._raw_sr

    def ready(self) -> bool:
        return (not self._closed and (self._task is None or self._task.done())
                and time.monotonic() - self._last_at >= .3)

    def feed(self, pcm: bytes, sample_rate: int) -> None:
        token = self._current_token()
        if (not pcm or token is None or (self._confirmed is not None and token[-1] == self._confirmed[-1])
                or not self.ready()):
            return
        self._last_at = time.monotonic()
        self._task = asyncio.create_task(self._run(pcm, sample_rate, token), name="barge-in-preview")

    async def _run(self, pcm: bytes, sample_rate: int, token) -> None:
        started = time.monotonic()
        try:
            async with asyncio.timeout(.8):
                text = await self._transcribe(pcm, sample_rate)
            logger.info("[barge-in preview] req=%s preview_ms=%.0f audio_ms=%d text=%r",
                        token[-1], (time.monotonic() - started) * 1000,
                        len(pcm) * 1000 // (sample_rate * 2), text)
            if token != self._current_token() or not is_interrupt_command(text):
                return
            if self._spoken_fragment(text, token):
                return
            self._confirmed = token
            logger.info("[barge-in probe] confirmed req=%s preview_ms=%.0f text=%r",
                        token[-1], (time.monotonic() - started) * 1000, text)
            await self._confirm(token, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[barge-in probe] unavailable; final ASR remains active", exc_info=True)

    async def aclose(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
