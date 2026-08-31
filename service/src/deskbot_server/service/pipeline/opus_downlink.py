"""下行 pb TTS：PCM s16le → Opus batch（与上行相同的 uint16_be + frame 格式）。

编码器为**模块级单例、按采样率缓存、跨 batch 复用**：一整段音频切成多个小块
下发时，必须共用同一编码器。若每个 batch 新建 Encoder，每块首帧会缺失
编码器内部的 lookahead 启动状态，解码端在块边界产生相位跳变，听感为断续
（s16le 无此问题）。opus 帧本身相互独立，解码端（固件 s_dec 本就是跨帧
复用的单例）无需任何改动。

并发约束：编码器有内部状态，调用方须串行（asyncio 单线程内天然成立）；
``_encoders`` 仅用锁保护字典本身。采样率变化时自动重建（同固件
``ensure_decoder`` 的模式）。
"""

from __future__ import annotations

import struct
import threading

import opuslib_next

from deskbot_server.service.pipeline.opus_uplink import opus_frame_samples

_OPUS_LP_HDR = struct.Struct("!H")

_encoders: dict[int, opuslib_next.Encoder] = {}
_encoders_lock = threading.Lock()


def _get_encoder(sample_rate: int) -> opuslib_next.Encoder:
    with _encoders_lock:
        enc = _encoders.get(sample_rate)
        if enc is None:
            enc = opuslib_next.Encoder(sample_rate, 1, opuslib_next.APPLICATION_AUDIO)
            _encoders[sample_rate] = enc
        return enc


def encode_pcm_s16le_to_opus_batch(pcm: bytes, sample_rate: int) -> tuple[bytes, int]:
    """mono s16le PCM → ``(opus_batch, frame_count)``。"""
    if not pcm:
        return b"", 0
    frame_samples = opus_frame_samples(sample_rate)
    frame_bytes = frame_samples * 2
    enc = _get_encoder(sample_rate)
    parts: list[bytes] = []
    nframes = 0
    offset = 0
    while offset < len(pcm):
        chunk = pcm[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        opus = enc.encode(chunk, frame_samples)
        parts.append(_OPUS_LP_HDR.pack(len(opus)) + opus)
        nframes += 1
        offset += frame_bytes
    return b"".join(parts), nframes


def decode_opus_batch_to_pcm_s16le(
    decoder: opuslib_next.Decoder, payload: bytes, *, sample_rate: int, opus_frames: int | None = None
) -> bytes:
    """Opus batch → mono s16le PCM（供单测与调试）。"""
    if not payload:
        return b""
    frame_samples = opus_frame_samples(sample_rate)
    if opus_frames is None or opus_frames <= 1:
        return decoder.decode(payload, frame_samples)
    pcm_parts: list[bytes] = []
    offset = 0
    for i in range(opus_frames):
        if offset + _OPUS_LP_HDR.size > len(payload):
            raise ValueError(f"opus downlink frame {i}: missing length header")
        (frame_len,) = _OPUS_LP_HDR.unpack_from(payload, offset)
        offset += _OPUS_LP_HDR.size
        if frame_len <= 0 or offset + frame_len > len(payload):
            raise ValueError(f"opus downlink frame {i}: invalid length {frame_len}")
        pcm_parts.append(decoder.decode(payload[offset : offset + frame_len], frame_samples))
        offset += frame_len
    if offset != len(payload):
        raise ValueError(f"opus downlink trailing bytes: {len(payload) - offset}")
    return b"".join(pcm_parts)
