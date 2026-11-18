"""Audio stays complete and timed without text-to-phoneme models."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from deskbot_server.client.tts.audio_segments import split_pcm_segments
from deskbot_server.client.tts.doubao import DoubaoTtsConfig, _build_start_session_payload, load_doubao_tts_config
from deskbot_server.client.tts.doubao_phoneme import DoubaoPhonemeTtsAdapter
from deskbot_server.model.settings import AppSettings
from deskbot_server.pb.wire import build_pb_wire_pairs


@pytest.mark.parametrize("sample_rate", [16000, 24000, 48000])
@pytest.mark.parametrize("extra_samples", [0, 1, 17])
def test_chunks_preserve_samples_order_and_duration(sample_rate, extra_samples):
    samples = sample_rate * 2 + extra_samples
    pcm = bytes(range(256)) * (samples * 2 // 256 + 1)
    pcm = pcm[:samples * 2]
    segments = split_pcm_segments(pcm, sample_rate)
    assert b"".join(s.pcm for s in segments) == pcm
    assert all(len(s.pcm) % 2 == 0 and s.ms <= 200 for s in segments)
    assert all(not s.phoneme and s.phoneme_id is None for s in segments)
    assert abs(sum(s.ms for s in segments) - samples * 1000 / sample_rate) <= 1


def test_empty_audio_and_invalid_pcm():
    assert split_pcm_segments(b"", 16000) == []
    with pytest.raises(ValueError, match="complete samples"):
        split_pcm_segments(b"x", 16000)
    with pytest.raises(ValueError, match="positive"):
        split_pcm_segments(b"", 0)
    with pytest.raises(ValueError, match="positive"):
        split_pcm_segments(b"", 16000, chunk_ms=0)


def test_audio_only_wire_preserves_content_without_mouth_metadata():
    pcm = bytes(range(256)) * 250  # Two seconds at 16kHz.
    segments = [asdict(s) for s in split_pcm_segments(pcm, 16000)]
    pairs, _, _, _ = build_pb_wire_pairs(segments, {}, sample_rate=16000, anims=[])
    assert b"".join(blob for _, blobs in pairs for blob in blobs) == pcm
    assert sum(msg["chunk_ms"] for msg, _ in pairs) == 2000
    assert all(not any(k in msg for k in ("anim", "phoneme", "phoneme_id")) for msg, _ in pairs)


def test_doubao_adapter_needs_audio_without_alignment_metadata(monkeypatch):
    async def synthesize(text, cfg):
        return SimpleNamespace(pcm=b"\x01\x00" * 16000, sample_rate=16000, elapsed_ms=1)

    monkeypatch.setattr("deskbot_server.client.tts.doubao_phoneme.synthesize_doubao_tts", synthesize)
    settings = AppSettings.from_config({"tts": {"sample_rate": 16000}})
    sr, segments = asyncio.run(DoubaoPhonemeTtsAdapter(settings).synthesize_phoneme_segments("你好 hello"))
    assert sr == 16000
    assert b"".join(s.pcm for s in segments) == b"\x01\x00" * 16000
    assert sum(s.ms for s in segments) == 1000
    assert all(not s.phoneme for s in segments)


def test_doubao_timestamps_are_opt_in():
    cfg = DoubaoTtsConfig(api_key="test", speaker="test")
    assert b"enable_timestamp" not in _build_start_session_payload(cfg)
    assert not load_doubao_tts_config({}, {"enable_timestamp": False}).enable_timestamp
    assert load_doubao_tts_config({}, {"enable_timestamp": True}).enable_timestamp


def test_retired_face_bundle_config_is_not_forwarded_to_providers():
    settings = AppSettings.from_config({"tts": {
        "pb_face_bundle": "demo", "pb_face_bundle_json": "old.json", "pb_face_bundle_file": "overlay.json",
    }})
    assert not any(k.startswith("pb_face_bundle") for k in settings.tts_cfg)
