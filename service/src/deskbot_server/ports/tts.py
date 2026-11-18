from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class PhonemeSegment:
    """TTS playback segment; historical phoneme fields stay empty for RMS mouths."""

    phoneme: str
    ms: int
    pcm: bytes
    phoneme_id: Any = None


class TtsPort(Protocol):
    # Keep the port name for existing callers and explicitly injected adapters.
    async def synthesize_phoneme_segments(self, text: str) -> tuple[int, list[PhonemeSegment]]: ...
