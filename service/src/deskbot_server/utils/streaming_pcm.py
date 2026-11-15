"""Chunk-invariant causal FIR decimation, 48kHz mono PCM16 to 16kHz."""

from __future__ import annotations

import numpy as np


class Pcm48To16:
    def __init__(self):
        n = np.arange(63) - 31
        taps = np.sinc(2 * 0.15 * n) * np.kaiser(63, 8.6)
        self.taps = taps / taps.sum()
        self.history = np.zeros(62)
        self.samples = 0

    def feed(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("unaligned PCM16")
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        if not len(x):
            return b""
        merged = np.concatenate((self.history, x))
        filtered = np.convolve(merged, self.taps, mode="valid")
        y = filtered[(-self.samples) % 3 :: 3]
        self.history = merged[-62:]
        self.samples += len(x)
        return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()
