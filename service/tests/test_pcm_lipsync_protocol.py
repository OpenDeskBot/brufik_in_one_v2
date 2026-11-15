"""Local expression wire + PCM speech contract, independent of TTS labels."""
import asyncio
import json
from dataclasses import replace

import pytest

from deskbot_server.dao.face_asset_catalog import find_face_asset
from deskbot_server.model.pb_seq import PbAnim, PbAudio, PbBlock, PbType
from deskbot_server.pb.wire import build_pb_wire_pairs
from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry
from deskbot_server.service.pb_service import PbService


def build(phoneme, *, anims=None, ms=320):
    pcm = b'\x11\x22' * (16 * ms)
    pairs, _, _, _ = build_pb_wire_pairs(
        [{'pcm': pcm, 'ms': ms, 'phoneme': phoneme, 'phoneme_id': 123}],
        {'output_codec': 's16le'}, sample_rate=16000, request_id='pcm-test', anims=anims,
    )
    return pairs, pcm


def test_speech_wire_ignores_tts_labels_and_has_no_drawing_code():
    first, pcm = build('a')
    other, _ = build('sil')
    assert first == other
    assert b''.join(b for _, bins in first for b in bins) == pcm
    for wire, _ in first:
        assert 'phoneme' not in json.dumps(wire)
        assert 'elements' not in json.dumps(wire)
        assert all(a['anim_id'] == 'idle' for a in wire.get('anim', []))
        assert len(json.dumps(wire)) < 600
        block = PbBlock.from_wire(wire)
        round_trip = block.to_wire(sr=16000, fmt='s16le', ch=1)
        assert round_trip['anim'] == wire['anim']
        assert round_trip['audio'] == wire['audio']


def test_tts_assembly_does_not_resolve_voice_for_mouth_animation(monkeypatch):
    from deskbot_server.service import tts_routing

    def unavailable(*args, **kwargs):
        raise AssertionError('assembling speech must not resolve a voice template')

    monkeypatch.setattr(tts_routing, 'resolve_tts_adapter', unavailable)
    monkeypatch.setattr(tts_routing, 'resolve_tts_provider', unavailable)
    pcm = b'\x11\x22' * 1600
    seq = PbService().build_pb_seq_from_tts(
        [{'pcm': pcm, 'ms': 100, 'phoneme': 'a'}], {'output_codec': 's16le'},
        sample_rate=16000, device_id='volume-only', anims=[],
    )
    assert seq is not None
    pairs = seq.to_wire_pairs()
    assert b''.join(blob for _, blobs in pairs for blob in blobs) == pcm
    assert all('voice_key' not in wire and 'anim' not in wire for wire, _ in pairs)


def test_standalone_scene_is_one_id_and_no_inline_frames():
    seq = PbService().get_scene_pb_seq('curious')
    assert seq.is_single
    wire, bins = seq.to_wire_pairs()[0]
    assert wire['anim_id'] == 'curious' and 'anim' not in wire and not bins
    asset, version = find_face_asset('curious')
    assert wire['chunk_ms'] == asset['duration_ms'] and wire['anim_version'] == version
    assert len(json.dumps(wire)) < 300


def test_mixed_expression_audio_round_trip():
    block = PbBlock(type=PbType.CHUNK, req='audio', idx=1, epoch=2, stream=True,
                    pts_ms=300, chunk_ms=80, anim_version='release',
                    anim=(PbAnim(anim_id='happy', frame=2, ms=80),),
                    audio=PbAudio(next_bin_len=2560, sr=16000, ch=1, fmt='s16le'))
    wire = block.to_wire(sr=16000, ch=1, fmt='s16le')
    assert PbBlock.from_wire(wire) == block
    assert wire['anim'] == [{'anim_id': 'happy', 'frame': 2, 'ms': 80}]
    for change in ({'elements': {'mouth': []}}, {'frame': 64}, {'ms': 0}, {'phoneme_id': 'a'}):
        with pytest.raises(ValueError):
            replace(block.anim[0], **change).to_wire()


def test_unknown_animation_requires_compiled_asset():
    with pytest.raises(ValueError, match='not in the firmware catalog'):
        build('a', anims=['unknown-animation'])


@pytest.mark.parametrize('caps,version,accepted', [
    (set(), 'release', False), ({'anim_frame_id_v1'}, 'old', False),
    ({'anim_frame_id_v1', 'pcm_lipsync_v1'}, 'release', True),
])
def test_audio_animation_capability_and_version_gate(monkeypatch, caps, version, accepted):
    async def run():
        DeviceWsService.reset_instance()
        svc = DeviceWsService()
        entry = _DeviceEntry(device_id='pcm-mouth', ws=object(), capabilities=caps, anim_version=version)
        svc._devices[entry.device_id] = entry
        sent = []
        async def enqueue(entry, wire, binaries, **kwargs):
            sent.append(json.loads(wire))
            return True
        monkeypatch.setattr(svc, '_enqueue_dl', enqueue)
        block = PbBlock(type=PbType.SINGLE, req='test', chunk_ms=80, anim_version='release',
                        anim=(PbAnim(anim_id='happy', frame=0, ms=80),))
        assert await svc._do_send_to_device(entry.device_id, block) is accepted
        assert len(sent) == int(accepted)
        DeviceWsService.reset_instance()
    asyncio.run(run())


def test_servo_plan_and_leading_audio_keep_animation_timeline():
    segs = [{'pcm': b'\x00\x01' * 1600, 'ms': 100, 'phoneme': 'ignored'} for _ in range(6)]
    pairs, _, _, _ = build_pb_wire_pairs(
        segs, {}, sample_rate=16000, anims=['wink'],
        servo_plan=[{'_hold_ms': 200}, {'xm': 1, 'ym': 2, 'x': 4, 'y': 0, 'ms': 100}],
    )
    assert any(msg.get('servo') for msg, _ in pairs)
    for msg, _ in pairs:
        assert sum(a['ms'] for a in msg['anim']) == msg['chunk_ms']
        assert len(msg['anim']) <= 64
        assert all('anim_id' in a and 'elements' not in a for a in msg['anim'])


def test_optional_expression_shorter_than_audio_preserves_timing_holds():
    segs = [{'pcm': b'\x00\x01' * 1600, 'ms': 100} for _ in range(6)]
    pairs, _, _, _ = build_pb_wire_pairs(
        segs, {}, sample_rate=16000, anims=[{'anim': 'wink', 'ms': 200}],
    )
    assert sum(msg['chunk_ms'] for msg, _ in pairs) == 600
    poses = [pose for msg, _ in pairs for pose in msg.get('anim', [])]
    assert any(pose.get('anim_id') == 'wink' for pose in poses)
    assert any(not pose.get('anim_id') for pose in poses)
    for msg, _ in pairs:
        if msg.get('anim'):
            assert sum(pose['ms'] for pose in msg['anim']) == msg['chunk_ms']
    assert all(not pose.get('elements') for pose in poses)


def test_short_opus_payload_keeps_batch_framing_and_declared_content_duration():
    import math
    import struct

    import opuslib_next

    from deskbot_server.service.pipeline.opus_downlink import decode_opus_batch_to_pcm_s16le

    pcm = struct.pack('<160h', *(int(10000 * math.sin(i * .2)) for i in range(160)))
    pairs, _, _, _ = build_pb_wire_pairs(
        [{'pcm': pcm, 'ms': 10}], {'output_codec': 'opus'}, sample_rate=16000,
    )
    wire, binaries = pairs[0]
    assert wire['audio']['frames'] == 1 and wire['chunk_ms'] == 10
    decoded = decode_opus_batch_to_pcm_s16le(
        opuslib_next.Decoder(16000, 1), binaries[0], sample_rate=16000, opus_frames=1,
    )
    # Codec fills a 20 ms frame; firmware plays only the declared 10 ms content.
    assert len(decoded) == 640
