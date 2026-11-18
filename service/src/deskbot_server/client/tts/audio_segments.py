"""Bounded PCM chunks for playback; mouth animation is computed by the ROM."""

from __future__ import annotations

from deskbot_server.ports.tts import PhonemeSegment


def split_pcm_segments(pcm: bytes, sample_rate: int, *, chunk_ms: int = 200) -> list[PhonemeSegment]:
    """Split mono s16le at sample boundaries, preserving every byte in order.

    The TTS port keeps its existing segment type, with empty phoneme metadata.
    No text alignment or pronunciation model is needed for volume-only mouths.
    """
    if sample_rate <= 0 or chunk_ms <= 0:
        raise ValueError("sample_rate and chunk_ms must be positive")
    if len(pcm) % 2:
        raise ValueError("mono s16le PCM must contain complete samples")
    frames_per_chunk = max(1, sample_rate * chunk_ms // 1000)
    total_frames = len(pcm) // 2
    segments = []
    for start in range(0, total_frames, frames_per_chunk):
        end = min(start + frames_per_chunk, total_frames)
        duration = max(1, end * 1000 // sample_rate - start * 1000 // sample_rate)
        segments.append(PhonemeSegment(phoneme="", ms=duration, pcm=pcm[start * 2:end * 2]))
    return segments
