"""Built-in expression IDs survive PB model conversion and device capability gates."""
import asyncio
import json
from dataclasses import replace

import pytest

from deskbot_server.model.pb_seq import PbAnim, PbBlock, PbSeq, PbType
from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry


def test_face_id_round_trip_keeps_wire_small():
    block = PbBlock(type=PbType.SINGLE, req='face-test', chunk_ms=1000,
                    anim_id='curious', anim_version='test-version')
    wire = block.to_wire()
    assert 'anim' not in wire
    assert len(json.dumps(wire)) < 256
    assert PbBlock.from_wire(wire) == block
    assert PbSeq(req=block.req, entries=(block,)).total_chunk_ms == 1000
    assert 'chunk_ms' not in replace(block,chunk_ms=0).to_wire()


def test_phoneme_ids_preserve_audio_timing_and_round_trip():
    block = PbBlock(type=PbType.CHUNK, req='speech', idx=1, chunk_ms=300,
                    anim=(PbAnim(phoneme_id='a',ms=120),PbAnim(phoneme_id='ai',ms=180)),
                    anim_version='v2', epoch=2, stream=True, pts_ms=500)
    wire=block.to_wire()
    assert wire['anim']==[{'phoneme_id':'a','ms':120},{'phoneme_id':'ai','ms':180}]
    assert wire['anim_version']=='v2'
    assert PbBlock.from_wire(wire)==block
    with pytest.raises(ValueError,match='inline'):
        PbAnim(phoneme_id='a',elements={'mouth':[]}).to_wire()


@pytest.mark.parametrize('caps,version,ok',[(set(),'v2',False),({'phoneme_id_v1'},'old',False),({'phoneme_id_v1'},'v2',True)])
def test_phoneme_send_gate(monkeypatch,caps,version,ok):
    async def run():
        DeviceWsService.reset_instance()
        svc=DeviceWsService()
        entry=_DeviceEntry(device_id='mouth',ws=object(),capabilities=caps,anim_version=version)
        svc._devices['mouth']=entry
        sent=[]
        async def enqueue(entry,wire,binaries,**kwargs):
            sent.append(json.loads(wire));return True
        monkeypatch.setattr(svc,'_enqueue_dl',enqueue)
        block=PbBlock(type=PbType.SINGLE,req='mouth',chunk_ms=120,anim=(PbAnim(phoneme_id='a',ms=120),),anim_version='v2')
        assert await svc._do_send_to_device('mouth',block) is ok
        assert len(sent)==int(ok)
    asyncio.run(run())


@pytest.mark.parametrize('change', [
    dict(type=PbType.CHUNK), dict(idx=1), dict(stream=True), dict(binaries=(b'x',)),
    dict(anim=(PbAnim(elements={},ms=100),)),
])
def test_face_id_rejects_mixed_payloads(change):
    block = PbBlock(type=PbType.SINGLE, req='test', anim_id='curious')
    with pytest.raises(ValueError,match='standalone'):
        replace(block,**change).to_wire()


@pytest.mark.parametrize('capabilities,version,accepted', [
    (set(), 'v1', False), ({'anim_id_v1'}, 'old', False), ({'anim_id_v1'}, 'v1', True),
])
def test_send_checks_device_support_and_version(monkeypatch,capabilities,version,accepted):
    async def run():
        DeviceWsService.reset_instance()
        svc = DeviceWsService()
        entry = _DeviceEntry(device_id='face-device',ws=object(),capabilities=capabilities,anim_version=version)
        svc._devices[entry.device_id] = entry
        wires = []
        async def enqueue(entry,wire,binaries,**kwargs):
            wires.append(json.loads(wire))
            return True
        monkeypatch.setattr(svc,'_enqueue_dl',enqueue)
        block = PbBlock(type=PbType.SINGLE,req='face-test',anim_id='curious',anim_version='v1',chunk_ms=1000)
        assert await svc._do_send_to_device(entry.device_id,block) is accepted
        assert len(wires) == int(accepted)
        if accepted:
            assert wires[0]['anim_id'] == 'curious'
            assert 'anim' not in wires[0]
        assert svc._snapshot_entry(entry)['anim_version'] == version
    asyncio.run(run())
