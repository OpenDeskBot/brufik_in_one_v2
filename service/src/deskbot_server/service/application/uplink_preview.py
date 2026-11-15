"""Optional negotiated raw-PCM trailer; processed Opus stays byte-for-byte intact."""
from __future__ import annotations


def split_uplink_preview(data: dict, media: bytes) -> tuple[bytes, bytes]:
    raw_bytes = int(data.get("raw_bytes") or 0)
    if not raw_bytes:
        return media, b""
    opus_bytes = int(data.get("opus_bytes") or 0)
    if (data.get("raw_sr") != 8000 or opus_bytes <= 0 or raw_bytes <= 0
            or raw_bytes > 640 or raw_bytes % 2 or opus_bytes + raw_bytes != len(media)):
        raise ValueError("invalid raw microphone preview trailer")
    return media[:opus_bytes], media[opus_bytes:]
