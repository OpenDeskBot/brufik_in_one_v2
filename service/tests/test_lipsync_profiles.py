import asyncio
import json

import pytest

from deskbot_server.model.lipsync_profile import normalize_lipsync
from deskbot_server.model.pb_seq import PbSeq

def test_volume_only_defaults_and_legacy_templates_are_stripped():
    expected = {'classifier': 'rms-v1', 'closed_phoneme': 'sil', 'open_phoneme': 'a'}
    names = {'sil', 'a'}
    assert normalize_lipsync({}, names) == expected
    assert normalize_lipsync(expected, names) == expected
    legacy = {'classifier': 'mfcc-vowel-v1', 'profiles': [{'id': 'old-voice'}],
              'feature': 'obsolete', 'vowels': {'A': 'missing'}, 'hold_ms': True}
    assert normalize_lipsync(legacy, names) == expected
    assert legacy['classifier'] == 'mfcc-vowel-v1'
    assert normalize_lipsync(dict(legacy, open_phoneme='sil'), names)['open_phoneme'] == 'sil'


@pytest.mark.parametrize('config', [None, [], {'unexpected': 1}, {'classifier': 'other'},
                                    {'open_phoneme': 'missing'}, {'closed_phoneme': None},
                                    {'open_phoneme': ['a']}, {'closed_phoneme': 1}])
def test_invalid_mouth_endpoints_are_rejected(config):
    with pytest.raises(ValueError):
        normalize_lipsync(config, {'sil', 'a'})


def test_audio_voice_identity_survives_batch_roundtrip_without_mouth_labels():
    wire = {'type': 'pb_single', 'req': 'voice', 'idx': 0, 'chunk_ms': 100,
            'sr': 16000, 'ch': 1, 'fmt': 's16le', 'voice_key': 'macos:Tingting',
            'audio': {'next_bin_len': 3200}}
    seq = PbSeq.from_wire_pairs([(wire, [bytes(3200)])])
    assert seq.voice_key == 'macos:Tingting'
    result = seq.to_wire_pairs()[0][0]
    assert result['voice_key'] == seq.voice_key
    assert not any(key in result for key in ('anim', 'phoneme', 'phoneme_id'))


@pytest.mark.parametrize('key', ['', 'macos:Tingting'])
def test_stream_only_sends_declared_voice_metadata(monkeypatch, key):
    from contextlib import suppress
    from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry

    async def run():
        DeviceWsService.reset_instance()
        service = DeviceWsService()
        entry = _DeviceEntry(device_id='test', ws=object(), capabilities={'pb_stream_v1'})
        service._devices['test'] = entry
        sent = []

        async def enqueue(entry, wire, binaries, **kwargs):
            message = json.loads(wire)
            sent.append(message)
            entry.ack_queue.put_nowait({'type': 'pb_ack', 'req': message['req'], 'epoch': message['epoch'],
                                       'idx': message['idx'], 'ack_type': 'pb_end' if message['type'] == 'pb_end' else 'pb_credit'})
            return True

        monkeypatch.setattr(service, '_enqueue_dl', enqueue)
        stream = await service.open_speech_stream('test', 'voice', sr=16000, fmt='s16le', voice_key=key)
        worker = asyncio.create_task(service._device_loop('test'))
        try:
            await stream.append([({'type': 'pb_single', 'req': 'x', 'idx': 0, 'chunk_ms': 100,
                                  'audio': {'next_bin_len': 3200}}, [bytes(3200)])])
            async with asyncio.timeout(3):
                assert (await stream.finish()).completed
            assert sent[0].get('voice_key', '') == key
            assert 'voice_key' not in sent[-1]
            assert not any('anim' in message or 'phoneme' in message for message in sent)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError): await worker
            DeviceWsService.reset_instance()

    asyncio.run(run())
