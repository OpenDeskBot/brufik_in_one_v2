"""Negotiated, connection-scoped PB media encoding (network byte order).

Control and first audio remain PB JSON; only pure continuation audio uses the
24-byte envelope. ACKs use 32 bytes. Neither receipt nor playback is inferred
from the other. See docs/esp32_pb_protocol.md for the byte layout.
"""

from __future__ import annotations

import struct
from collections import OrderedDict

from deskbot_server.service.application.asr_chat_uplink import PackedFrame, parse_packed_frame

COMPACT = "pb_compact_v1"
AUDIO = "audio_bin_v1"
ACK = "ack_bin_v1"
CAPABILITIES = [COMPACT, AUDIO, ACK]
MAGIC = b"DB\x01"
AUDIO_HEADER = struct.Struct("!2sBBIIIHHI")
ACK_HEADER = struct.Struct("!2sBBIIIIIHBBI")
ACK_TYPES = ("pb_chunk", "pb_end", "pb_credit", "pb_audio_end", "pb_started", "pb_progress", "pb_cancelled", "pb_error")
MAX_MEDIA = 256 * 1024 - AUDIO_HEADER.size
META_KEYS = ("pb_ver", "level", "stream", "sr", "fmt", "ch")


class MediaWire:
    """One instance per main WS generation; maps bounded epochs to request IDs."""

    def __init__(self):
        self.capabilities: set[str] = set()
        self.epochs: OrderedDict[int, tuple[str, int]] = OrderedDict()
        self.meta: tuple | None = None
        self.uplink_idx = -1

    def downlink(self, payload: dict, binaries: list[bytes]) -> tuple[dict, bytes | None]:
        p = dict(payload)
        epoch, idx = p.get("epoch", 0), p.get("idx", 0)
        if epoch:
            old = self.epochs.get(epoch)
            if old and old[0] != p["req"]:
                raise ValueError("epoch reused for a different request")
            self.epochs[epoch] = (p["req"], max(idx, old[1] if old else 0))
            while len(self.epochs) > 64:
                self.epochs.popitem(last=False)
        metadata = tuple(p.get(k) for k in META_KEYS)
        identity = (p.get("req"), epoch)
        continuation = (
            p.get("stream")
            and p.get("type") in ("pb_chunk", "pb_end")
            and self.meta is not None
            and self.meta[0] == identity
            and all(k not in p or p[k] == v for k, v in zip(META_KEYS, self.meta[1], strict=True))
        )
        audio = p.get("audio") or {}
        if (
            AUDIO in self.capabilities
            and continuation
            and p.get("type") == "pb_chunk"
            and p.get("stream")
            and len(binaries) == 1
            and not p.get("anim")
            and not p.get("servo")
            and "volume" not in p
            and not p.get("voice_key")
            and not p.get("anim_version")
            and 0 < len(binaries[0]) <= MAX_MEDIA
            and len(binaries[0]) == audio.get("next_bin_len")
            and 0 < p.get("chunk_ms", 0) <= 65535
            and 0 <= audio.get("frames", 0) <= 65535
        ):
            return p, AUDIO_HEADER.pack(
                b"DB", 1, 1, epoch, idx, p["pts_ms"], p["chunk_ms"], audio.get("frames", 0), len(binaries[0])
            ) + binaries[0]
        if COMPACT in self.capabilities and continuation:
            for key in META_KEYS:
                p.pop(key, None)
        # Only a full head establishes metadata; no inheritance across requests.
        if payload.get("type") in ("pb_start", "pb_single"):
            self.meta = (identity, metadata)
        return p, None

    def uplink(self, raw: bytes) -> PackedFrame | None:
        if raw[:2] != b"DB":
            return parse_packed_frame(raw)
        if raw[:3] != MAGIC or len(raw) < 4:
            return None
        if raw[3] == 2 and AUDIO in self.capabilities:
            if len(raw) < AUDIO_HEADER.size:
                return None
            _, _, _, epoch, idx, pts, duration, frames, opus_bytes = AUDIO_HEADER.unpack_from(raw)
            media = raw[AUDIO_HEADER.size :]
            if (
                epoch
                or idx <= self.uplink_idx
                or not 1 <= frames <= 50
                or duration != frames * 20
                or not 0 < opus_bytes <= len(media) <= MAX_MEDIA
            ):
                return None
            raw_bytes = len(media) - opus_bytes
            # Optional raw preview is exactly 8kHz mono PCM16 over this duration.
            if raw_bytes not in (0, duration * 16):
                return None
            self.uplink_idx = idx
            return PackedFrame(
                dict(
                    type="audio",
                    codec="opus",
                    sr=16000,
                    ch=1,
                    frames=frames,
                    next_bin_len=len(media),
                    opus_bytes=opus_bytes,
                    raw_bytes=raw_bytes,
                    raw_sr=8000,
                    pts_ms=pts,
                ),
                len(media),
                media,
            )
        if raw[3] == 3 and ACK in self.capabilities and len(raw) == ACK_HEADER.size:
            _, _, _, epoch, idx, played, buffered, device, space, code, flags, wait = ACK_HEADER.unpack(raw)
            stream = self.epochs.get(epoch)
            if not stream or code >= len(ACK_TYPES) or flags & ~1 or idx > stream[1] or idx > 0x7FFFFFFF:
                return None
            p = dict(
                type="pb_ack",
                req=stream[0],
                epoch=epoch,
                idx=idx,
                ack_type=ACK_TYPES[code],
                buffered_ms=buffered,
                device_ms=device,
                space=space,
            )
            if flags & 1:
                p.update(played_ms=played, queue_wait_ms=wait)
            return PackedFrame(p)
        return None
