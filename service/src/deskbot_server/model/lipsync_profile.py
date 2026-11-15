"""Volume-only mouth endpoints shared by the ROM and expression packages."""
from __future__ import annotations


def normalize_lipsync(raw: dict, phonemes: set[str]) -> dict:
    # Accept old saved documents, but never export voice calibration data.
    allowed = {"classifier", "closed_phoneme", "open_phoneme", "feature", "vowels", "profiles",
               "hold_ms", "min_confidence", "max_distance", "silence_db"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("unsupported lipsync configuration")
    if raw.get("classifier", "rms-v1") not in ("rms-v1", "mfcc-vowel-v1"):
        raise ValueError("unsupported lipsync classifier")
    out = {"classifier": "rms-v1",
           "closed_phoneme": raw.get("closed_phoneme", "sil"),
           "open_phoneme": raw.get("open_phoneme", "a")}
    for key in ("closed_phoneme", "open_phoneme"):
        if not isinstance(out[key], str) or out[key] not in phonemes:
            raise ValueError("lipsync references an unknown phoneme")
    return out
