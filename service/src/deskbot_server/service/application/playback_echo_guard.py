"""Reject speaker feedback without disabling the microphone during playback.

Silero detects speech, not who spoke. While the speaker is audible, defer its
onset cancellation until ASR can distinguish a real interruption from feedback.
Outside playback the normal early-onset cancellation remains unchanged.
"""
from __future__ import annotations

import re
import time
from collections import deque
from difflib import SequenceMatcher

from deskbot_server.service.application.barge_in_probe import is_interrupt_command


def _normal(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text.lower()).replace("听一下", "停一下")


class PlaybackEchoGuard:
    def __init__(self):
        self._phrases = deque(maxlen=48)
        self._windows = deque(maxlen=16)
        self._active_req = None

    def note_text(self, req: str, text: str) -> None:
        normalized = _normal(text)
        if normalized:
            self._phrases.append((req, normalized, time.monotonic()))

    def active_request(self) -> str | None:
        return self._active_req if self.defer_onset() else None

    def spoken_fragment(self, text: str, req: str) -> bool:
        heard = _normal(text)
        return bool(heard) and heard in "".join(phrase for r, phrase, _ in self._phrases if r == req)

    def acknowledge(self, ack: dict) -> None:
        now = time.monotonic()
        req, kind = ack.get("req"), ack.get("ack_type")
        if kind == "pb_started":
            self._active_req = req
            self._windows.append([req, now, now + 45])
        elif kind == "pb_progress" and req == self._active_req and self._windows:
            self._windows[-1][2] = now + 45
        elif kind in {"pb_audio_end", "pb_end", "pb_cancelled", "pb_error"}:
            if req == self._active_req and self._windows:
                self._windows[-1][2] = now + .45
                self._active_req = None

    def defer_onset(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return any(start <= now <= end for _, start, end in self._windows)

    def reject(self, text: str, *, segment_start: float, segment_end: float) -> str | None:
        reqs = {req for req, start, end in self._windows
                if start <= segment_end and end >= segment_start}
        if not reqs:
            return None
        heard = _normal(text)
        if not heard:
            return None
        # Explicit stop/correction commands must work even when short or when
        # the robot has just uttered the same word.
        if is_interrupt_command(heard) and not any(self.spoken_fragment(heard, req) for req in reqs):
            return None
        if heard.startswith("小歪"):
            return None
        spoken = [phrase for req, phrase, at in self._phrases
                  if req in reqs and segment_end - at < 90]
        joined = "".join(spoken)
        if len(heard) >= 2 and heard in joined:
            return "speaker_text"
        if len(heard) >= 5:
            # ASR can omit or substitute a few characters in residual echo.
            matches = SequenceMatcher(None, heard, joined, autojunk=False).get_matching_blocks()
            covered = sum(match.size for match in matches)
            if max(match.size for match in matches) >= 4 and covered / len(heard) >= .62:
                return "speaker_text_partial"
        if re.fullmatch(r"(?:现在)?几点(?:钟|了|啦|呢|啊)?", heard):
            return None
        # Tiny fragments of speaker leakage are often transcribed as unrelated
        # words. Require a clear command or a longer utterance during overlap.
        if len(heard) <= 4 and spoken:
            return "ambiguous_short_overlap"
        return None
