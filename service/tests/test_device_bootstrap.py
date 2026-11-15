"""Exercise HTTP bootstrap, content-addressed updates and actual PB asset references."""

import asyncio
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deskbot_server.controller import device_bootstrap_controller
from deskbot_server.controller.decorators.http import RequestMiddleware
from deskbot_server.controller.registry import register_routers
from deskbot_server.dao.face_asset_catalog import find_face_asset
from deskbot_server.model.face_package import compile_document, encode_package
from deskbot_server.service.device_data_paths import global_config_dir


@pytest.fixture
def client():
    app = FastAPI()
    register_routers(app, [device_bootstrap_controller])
    app.add_middleware(RequestMiddleware)
    with TestClient(app) as result:
        yield result


def test_bootstrap_public_assets_no_credentials_and_conditional_get(client):
    response = client.get("/api/device/bootstrap", params={"device_id": "rom-test"})
    assert response.status_code == 200
    assert response.headers["x-request-id"]
    data = response.json()["data"]
    assert data["realtime"] == {"protocol": "deskbot.pb.v2", "url": "/asr_chat"}
    assert data["camera"]["url"] != data["realtime"]["url"]
    assert not any(k in response.text.lower() for k in ("api_key", "password", "secret"))
    pack = client.get(data["face_pack"]["url"])
    assert pack.status_code == 200
    assert len(pack.content) == data["face_pack"]["size"] < 256 * 1024
    assert hashlib.sha256(pack.content).hexdigest() == data["face_pack"]["sha256"]
    lib = pack.json()["library"]
    assert lib["version"] == data["face_pack"]["version"]
    assert lib["idle"]["clips"][1]["servo"]
    assert lib["lipsync"]["classifier"] == "rms-v1"
    assert set(lib["lipsync"]) == {"classifier", "closed_phoneme", "open_phoneme"}
    assert lib["lipsync"]["open_phoneme"] in {p["id"] for p in lib["phonemes"]}
    conditional = client.get(data["face_pack"]["url"], headers={"If-None-Match": pack.headers["etag"]})
    assert conditional.status_code == 304 and not conditional.content
    assert client.get("/api/device/face-pack/not-a-version").status_code == 404
    assert client.get("/api/device/bootstrap", params={"device_id": "../other"}).status_code == 400


def test_live_edit_changes_version_and_pb_catalog_but_old_package_is_immutable(client):
    before = client.get("/api/device/bootstrap?device_id=rom").json()["data"]["face_pack"]
    old = client.get(before["url"]).content
    source = global_config_dir() / "deskbot-face.json"
    doc = json.loads(source.read_text())
    doc["emotions"][0]["frames"][0]["ms"] += 1
    doc["lipsync"]["open_phoneme"] = "i"
    source.write_text(json.dumps(doc, ensure_ascii=False))
    after = client.get("/api/device/bootstrap?device_id=rom").json()["data"]["face_pack"]
    assert after["version"] != before["version"]
    assert client.get(before["url"]).content == old
    assert client.get(after["url"]).json()["library"]["lipsync"]["open_phoneme"] == "i"
    assert find_face_asset("idle")[1] == after["version"]


def test_package_version_includes_idle_and_mouth_mapping_and_is_deterministic():
    doc = json.loads((global_config_dir() / "deskbot-face.json").read_text())
    first = compile_document(doc)
    reordered = compile_document(json.loads(json.dumps(doc, sort_keys=True)))
    assert encode_package(first) == encode_package(reordered)
    doc["idle"]["after_ms"] += 1
    assert compile_document(doc)["version"] != first["version"]
    doc["lipsync"]["open_phoneme"] = "missing"
    with pytest.raises(ValueError, match="unknown phoneme"):
        compile_document(doc)


def test_legacy_voice_templates_do_not_change_exported_package_or_version():
    doc = json.loads((global_config_dir() / "deskbot-face.json").read_text())
    expected = compile_document(doc)
    doc["lipsync"].update(
        classifier="mfcc-vowel-v1", feature="obsolete", profiles=[{"id": "unused"}],
        vowels={"A": "missing"}, hold_ms=40, min_confidence=.08,
    )
    migrated = compile_document(doc)
    assert migrated["lipsync"]["classifier"] == "rms-v1"
    assert encode_package(migrated) == encode_package(expected)


def test_explicit_audio_only_does_not_transmit_animation_or_phonemes():
    from deskbot_server.pb.wire import build_pb_wire_pairs

    pcm = b"\x22\x11" * 16000
    pairs, _, _, _ = build_pb_wire_pairs(
        [{"pcm": pcm, "ms": 1000, "phoneme": "a"}], {"output_codec": "s16le"}, sample_rate=16000, anims=[]
    )
    assert b"".join(binary for _, binaries in pairs for binary in binaries) == pcm
    for wire, _ in pairs:
        assert wire["audio"]["next_bin_len"] > 0
        assert "anim" not in wire and "phoneme" not in json.dumps(wire)


@pytest.mark.parametrize("payload_kind", ["audio", "servo", "stream_end"])
def test_failed_package_update_does_not_prevent_independent_payload(monkeypatch, payload_kind):
    from deskbot_server.model.pb_seq import PbAnim, PbAudio, PbBlock, PbServo, PbType
    from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry

    async def run():
        DeviceWsService.reset_instance()
        service = DeviceWsService()
        entry = _DeviceEntry(
            device_id="offline-pack", ws=object(), capabilities={"local_face_pack_v1"}, anim_version="old-pack"
        )
        service._devices[entry.device_id] = entry
        sent = []

        async def enqueue(entry, wire, binaries, **kwargs):
            sent.append((json.loads(wire), binaries))
            return True

        monkeypatch.setattr(service, "_enqueue_dl", enqueue)
        block = PbBlock(
            type=PbType.END if payload_kind == "stream_end" else PbType.SINGLE,
            req="voice",
            chunk_ms=40,
            anim=(PbAnim(anim_id="happy", ms=40),),
            anim_version="new-pack",
            audio=PbAudio(next_bin_len=1280) if payload_kind == "audio" else None,
            binaries=(b"\0" * 1280,) if payload_kind == "audio" else (),
            servo=(PbServo(xm=1, x=5, ms=200),) if payload_kind == "servo" else (),
            stream=payload_kind == "stream_end",
            epoch=1 if payload_kind == "stream_end" else 0,
        )
        assert await service._do_send_to_device(entry.device_id, block)
        wire, binaries = sent[0]
        assert "anim" not in wire and "anim_version" not in wire
        if payload_kind == "audio":
            assert binaries == [b"\0" * 1280]
        elif payload_kind == "servo":
            assert wire["servo"] == [block.servo[0].to_wire()]
        else:
            assert wire["type"] == "pb_end" and wire["epoch"] == 1

    asyncio.run(run())
