import json

import pytest

from deskbot_server.pb.media_wire import (
    ACK,
    ACK_HEADER,
    ACK_TYPES,
    AUDIO,
    AUDIO_HEADER,
    CAPABILITIES,
    COMPACT,
    MediaWire,
)
from deskbot_server.service.application.asr_chat_uplink import pack_ws_downlink_frame


def block(kind="pb_start", idx=0, req="request", epoch=7, **extra):
    return dict(
        type=kind,
        req=req,
        epoch=epoch,
        idx=idx,
        pts_ms=idx * 80,
        chunk_ms=80,
        stream=True,
        pb_ver=2,
        level=3,
        sr=16000,
        ch=1,
        fmt="opus",
        audio={"next_bin_len": 3, "frames": 4},
        **extra,
    )


def test_binary_continuation_and_independent_optional_controls():
    w = MediaWire()
    w.capabilities = set(CAPABILITIES)
    first, raw = w.downlink(block(voice_key="voice-a"), [b"abc"])
    assert raw is None and first["voice_key"] == "voice-a"
    p, raw = w.downlink(block("pb_chunk", 1), [b"abc"])
    assert AUDIO_HEADER.unpack_from(raw) == (b"DB", 1, 1, 7, 1, 80, 80, 4, 3)
    assert raw[24:] == b"abc"
    # An optional expression or servo stays a single ordered PB JSON+media frame.
    for extra in ({"servo": [{"ms": 80}]}, {"anim": [{"ms": 80}]}, {"volume": 80}):
        p, raw = w.downlink(block("pb_chunk", 2, **extra), [b"abc"])
        assert raw is None and not any(k in p for k in ("level", "sr", "fmt", "ch", "stream", "pb_ver"))
    # No hidden metadata inheritance on another request or changed format.
    assert w.downlink(block("pb_chunk", 3, req="other", epoch=8), [b"abc"])[1] is None
    changed = block("pb_chunk", 3)
    changed["sr"] = 24000
    assert w.downlink(changed, [b"abc"])[0]["sr"] == 24000


@pytest.mark.parametrize("caps", [set(), {COMPACT}, {AUDIO}, {ACK}, set(CAPABILITIES)])
def test_fallback_and_capabilities_are_independent(caps):
    w = MediaWire()
    w.capabilities = caps
    w.downlink(block(), [b"abc"])
    p, raw = w.downlink(block("pb_chunk", 1), [b"abc"])
    assert (raw is not None) == (AUDIO in caps)
    if raw is None:
        assert ("sr" not in p) == (COMPACT in caps)
    legacy = pack_ws_downlink_frame(json.dumps(dict(type="device_hello")))
    assert w.uplink(legacy).doc["type"] == "device_hello"


def test_uplink_bounds_replay_preview_and_version():
    w = MediaWire()
    raw = AUDIO_HEADER.pack(b"DB", 1, 2, 0, 0, 0, 40, 2, 3) + b"abc"
    assert w.uplink(raw) is None
    w.capabilities = {AUDIO}
    p = w.uplink(raw)
    assert p.bin == b"abc" and p.doc["frames"] == 2 and p.doc["sr"] == 16000
    assert w.uplink(raw) is None
    for n in range(24):
        assert w.uplink(raw[:n]) is None
    for header in [
        (b"DB", 2, 2, 0, 1, 40, 40, 2, 3),
        (b"DB", 1, 2, 1, 1, 40, 40, 2, 3),
        (b"DB", 1, 2, 0, 1, 40, 41, 2, 3),
        (b"DB", 1, 2, 0, 1, 40, 40, 2, 4),
    ]:
        assert w.uplink(AUDIO_HEADER.pack(*header) + b"abc") is None
    preview = AUDIO_HEADER.pack(b"DB", 1, 2, 0, 1, 40, 40, 2, 3) + b"abc" + bytes(640)
    assert w.uplink(preview).doc["raw_bytes"] == 640
    assert w.uplink(AUDIO_HEADER.pack(b"DB", 1, 2, 0, 2, 80, 40, 2, 3) + b"abc" + bytes(2)) is None


def test_ack_identity_lifecycle_and_progress_are_not_credit():
    w = MediaWire()
    w.capabilities = {ACK}
    w.downlink(block(), [b"abc"])
    w.downlink(block("pb_chunk", 1), [b"abc"])
    for code, kind in enumerate(ACK_TYPES):
        raw = ACK_HEADER.pack(
            b"DB", 1, 3, 7, 1, 160, 80, 1234, 10, code, int(kind in ("pb_progress", "pb_started")), 20
        )
        p = w.uplink(raw).doc
        assert p["req"] == "request" and p["ack_type"] == kind
        assert ("played_ms" in p) == (kind in ("pb_progress", "pb_started"))
    assert MediaWire().uplink(raw) is None  # reconnect drops all epoch bindings
    for epoch, idx, code, flags in [(8, 1, 0, 0), (7, 2, 0, 0), (7, 1, 8, 0), (7, 1, 0, 2)]:
        assert w.uplink(ACK_HEADER.pack(b"DB", 1, 3, epoch, idx, 0, 0, 0, 0, code, flags, 0)) is None
    assert w.uplink(raw + b"x") is None
    with pytest.raises(ValueError):
        w.downlink(block(req="other"), [b"abc"])


def test_epoch_map_is_bounded():
    w = MediaWire()
    for i in range(1, 101):
        w.downlink(block(epoch=i, req=str(i)), [b"abc"])
    assert len(w.epochs) == 64 and 1 not in w.epochs


def test_nonstream_window_keeps_full_metadata_and_end_can_inherit():
    w = MediaWire()
    w.capabilities = set(CAPABILITIES)
    head = block()
    head["stream"] = False
    w.downlink(head, [b"abc"])
    chunk = block("pb_chunk", 1)
    chunk["stream"] = False
    p, raw = w.downlink(chunk, [b"abc"])
    assert raw is None and p["sr"] == 16000 and p["level"] == 3
    # END has no audio/sr/fmt/ch in PbBlock.to_wire; inherit the stream head.
    w.downlink(block(), [b"abc"])
    end = {k: v for k, v in block("pb_end", 2).items() if k not in ("audio", "sr", "fmt", "ch")}
    p, raw = w.downlink(end, [])
    assert raw is None and p == dict(type="pb_end", req="request", epoch=7, idx=2, pts_ms=160, chunk_ms=80)
