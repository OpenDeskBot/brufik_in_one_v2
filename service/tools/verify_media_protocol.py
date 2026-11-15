"""Bounded real-ROM protocol comparison. No raw microphone/video is saved.

Runs the ordinary service, temporarily pauses only the selected device's model
replies (no DB changes), reconnects through legacy/compact/binary negotiation,
then restores all hooks/capabilities and the configured conversation provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from collections import Counter
from pathlib import Path

from verify_vowel_lipsync import Recorder


async def until(predicate, timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def verify(runtime, args, recorder):
    import struct

    import opuslib_next

    from deskbot_server.dao import device_mapper
    from deskbot_server.pb.media_wire import CAPABILITIES, MediaWire

    hub = runtime.device_ws
    offered = CAPABILITIES[:]
    original_auto = device_mapper.get_auto_reply
    original_ack, original_enqueue = hub.record_pb_ack, hub._enqueue_dl
    original_decode, original_camera = MediaWire.uplink, hub._accept_camera_frame
    report = dict(
        device_id=args.device_id,
        completed=False,
        cases=[],
        measurement="real WebSocket bytes, ROM DMA progress and scalar diagnostics; not acoustic/optical latency",
    )
    events, transmitted = [], []
    uplink = Counter()

    def auto(dev):
        return False if dev == args.device_id else original_auto(dev)

    async def ack(dev, value):
        if dev == args.device_id:
            events.append({**value, "host_mono": time.monotonic()})
        await original_ack(dev, value)

    async def enqueue(entry, wire, binaries=None, *a, **kw):
        if entry.device_id == args.device_id:
            media = sum(len(b) for b in binaries or [])
            transmitted.append(
                dict(
                    binary=isinstance(wire, bytes),
                    bytes=len(wire) if isinstance(wire, bytes) else 4 + len(wire.encode()) + media,
                    header=24 if isinstance(wire, bytes) else 4 + len(wire.encode()),
                    host_mono=time.monotonic(),
                )
            )
        return await original_enqueue(entry, wire, binaries, *a, **kw)

    def decode(wire_session, raw):
        value = original_decode(wire_session, raw)
        entry = hub._devices.get(args.device_id)
        if entry and entry.media_wire is wire_session and value:
            kind = value.doc.get("type")
            uplink[kind or "unknown"] += 1
            uplink[(kind or "unknown") + "_bytes"] += len(raw)
            if raw[:2] == b"DB":
                uplink[(kind or "unknown") + "_binary"] += 1
        return value

    def camera(dev, media, **kw):
        if dev == args.device_id:
            uplink["camera_frames"] += 1
        return original_camera(dev, media, **kw)

    async def reconnect():
        entry = hub._devices[args.device_id]
        generation = entry.generation
        await entry.ws.close(code=1012, reason="protocol_verification")
        await until(
            lambda: (
                hub._devices.get(args.device_id)
                and hub._devices[args.device_id].generation > generation
                and "audio_bin_v1" in hub._devices[args.device_id].capabilities
            ),
            60,
        )
        await asyncio.sleep(1)
        return hub._devices[args.device_id]

    with wave.open(str(Path(__file__).resolve().parents[1] / "tests/fixtures/vad-speech-tingting.wav")) as f:
        assert (f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, 16000)
        source = f.readframes(f.getnframes())
    # Same encoded content and 80ms packet boundaries across every mode.
    pcm = (source * (204800 // len(source) + 1))[:204800]
    encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
    encoder.bitrate = 24000
    chunks = []
    for i in range(0, len(pcm), 2560):
        packets = [encoder.encode(pcm[j : j + 640], 320) for j in range(i, i + 2560, 640)]
        chunks.append((b"".join(struct.pack("!H", len(p)) + p for p in packets), len(packets)))

    async def append(stream, mixed=False, limit=None):
        entry = hub._devices[args.device_id]
        for i, (blob, frames) in enumerate(chunks[:limit]):
            msg = dict(
                type="pb_chunk",
                req=stream.seq.req,
                idx=i,
                chunk_ms=frames * 20,
                audio=dict(next_bin_len=len(blob), frames=frames),
            )
            if mixed and i == 2:
                msg.update(
                    anim_version=entry.anim_version,
                    anim=[dict(anim_id="curious", frame=0, ms=80)],
                    servo=[dict(xm=1, ym=2, x=2, ms=80)],
                )
            if mixed and i == 3:
                msg["servo"] = [dict(xm=1, ym=2, x=-2, ms=80)]
            if args.tail_action and i == len(chunks) - 1:
                msg["servo"] = [dict(xm=2, ym=2, x=0, y=0, ms=1200)]
            await stream.append([(msg, [blob])])

    device_mapper.get_auto_reply = auto
    hub.record_pb_ack, hub._enqueue_dl = ack, enqueue
    MediaWire.uplink, hub._accept_camera_frame = decode, camera
    try:
        recorder.allow_continue = True
        await until(
            lambda: args.device_id in hub._devices and "audio_bin_v1" in hub._devices[args.device_id].capabilities, 180
        )
        recorder.send("log 3")
        recorder.send("cpu_stats")
        for mode, caps in (("legacy", []), ("compact", ["pb_compact_v1"]), ("binary", offered)):
            if mode not in args.modes:
                continue
            CAPABILITIES[:] = caps
            await reconnect()
            uplink.clear()
            events.clear()
            transmitted.clear()
            start = time.monotonic()
            before = await recorder.query("audio_status")
            stream = await hub.open_speech_stream(
                args.device_id, "wire_" + mode, sr=16000, fmt="opus", level=3, voice_key=args.voice_key
            )
            if stream is None:
                raise RuntimeError("stream unavailable")
            producer = asyncio.create_task(append(stream, mixed=True))
            await producer
            outcome = await asyncio.wait_for(stream.finish(), 25)
            after = await recorder.query("audio_status")
            matching = [e for e in events if e.get("req") == stream.seq.req]
            started = next(e for e in matching if e["ack_type"] == "pb_started")
            last_played = max(e.get("played_ms", 0) for e in matching)
            case = dict(
                mode=mode,
                status=outcome.status.value,
                epoch=stream.seq.epoch,
                duration_ms=6400,
                played_ms=last_played,
                first_dma_ack_ms=(started["host_mono"] - transmitted[0]["host_mono"]) * 1000,
                wall_ms=(time.monotonic() - start) * 1000,
                downlink_bytes=sum(x["bytes"] for x in transmitted),
                header_bytes=sum(x["header"] for x in transmitted),
                binary_audio_packets=sum(x["binary"] for x in transmitted),
                uplink=dict(uplink),
                ack_counts=dict(Counter(e["ack_type"] for e in matching)),
                audio_before=before,
                audio_after=after,
                lipsync=await recorder.query("lipsync_status"),
                cpu=[e["line"] for e in recorder.since(start) if "[CPU]" in e.get("line", "")],
            )
            report["cases"].append(case)
            assert outcome.status.value == "completed" and last_played == 6400, case
            assert (case["binary_audio_packets"] > 0) == (mode == "binary"), case
            assert (uplink["audio_binary"] > 0) == (mode == "binary"), case
            assert (uplink["pb_ack_binary"] > 0) == (mode == "binary"), case
            if args.tail_action:
                audio_end = next(e for e in matching if e["ack_type"] == "pb_audio_end")
                all_end = next(e for e in matching if e["ack_type"] == "pb_end")
                assert audio_end["host_mono"] < all_end["host_mono"], case
                case["audio_end_to_all_end_ms"] = (all_end["host_mono"] - audio_end["host_mono"]) * 1000
            print("WIRE_CHECK " + json.dumps({k: v for k, v in case.items() if not k.startswith("audio_")}), flush=True)

        if args.modes:
            # Cancellation crosses the binary media path, followed by a fresh epoch.
            old = await hub.open_speech_stream(args.device_id, "wire_cancel_old", sr=16000, fmt="opus", level=3)
            producer = asyncio.create_task(append(old))
            await until(lambda: any(e.get("req") == old.seq.req and e["ack_type"] == "pb_started" for e in events))
            start = time.monotonic()
            await hub.interrupt_current_playback(
                args.device_id, reason="protocol_verification", expected_req=old.seq.req
            )
            await until(lambda: any(e.get("req") == old.seq.req and e["ack_type"] == "pb_cancelled" for e in events))
            report["cancel_ack_ms"] = (time.monotonic() - start) * 1000
            await asyncio.gather(producer, return_exceptions=True)
            new = await hub.open_speech_stream(args.device_id, "wire_cancel_new", sr=16000, fmt="opus", level=3)
            await append(new, limit=10)
            report["replacement"] = (await asyncio.wait_for(new.finish(), 10)).status.value
            report["cancel_final_played_ms"] = max(
                (e.get("played_ms", 0) for e in events if e.get("req") == old.seq.req), default=0
            )
            report["completed"] = report["replacement"] == "completed"
        else:
            report["completed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        CAPABILITIES[:] = offered
        device_mapper.get_auto_reply = original_auto
        hub.record_pb_ack, hub._enqueue_dl = original_ack, original_enqueue
        MediaWire.uplink, hub._accept_camera_frame = original_decode, original_camera
        recorder.send("cpu_stats_stop")
        recorder.send("log 2")
        try:
            await reconnect()
            report["restored"] = True
            if args.moss_roundtrip:
                await until(
                    lambda: (
                        hub._devices[args.device_id].conversation_session is not None
                        and hub._devices[args.device_id].conversation_session.ready
                    ),
                    25,
                )
                bridge = hub._devices[args.device_id].conversation_session
                client_send = bridge.client.send
                real_offer = bridge.offer_audio
                receipts = Counter()

                async def note_send(value):
                    if isinstance(value, dict) and value.get("action", "").startswith("playback."):
                        receipts[value["action"]] += 1
                    await client_send(value)

                bridge.client.send = note_send
                # Known generated voice substitutes only the bridge input for
                # this fixture; real ROM microphone/WS/AFE continue running.
                bridge.offer_audio = lambda _header, _media: None
                try:
                    start = time.monotonic()
                    for offset in range(0, len(source) + 32000, 640):
                        real_offer(
                            {"codec": "s16le", "sr": 16000, "ch": 1}, source[offset : offset + 640].ljust(640, b"\0")
                        )
                        await asyncio.sleep(0.02)
                    bridge.offer_audio = real_offer
                    await until(lambda: receipts["playback.completed"] > 0, 40)
                    report["moss_roundtrip"] = dict(
                        completed=True,
                        receipts=dict(receipts),
                        elapsed_s=time.monotonic() - start,
                        input="generated WAV injected at bridge; real ROM output/DMA receipts",
                    )
                finally:
                    bridge.offer_audio = real_offer
                    bridge.client.send = client_send
        except Exception as exc:
            report["restore_or_moss_error"] = type(exc).__name__
            report["restored"] = False
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        recorder.close()
        print("WIRE_CHECK_DONE " + json.dumps({k: v for k, v in report.items() if k != "cases"}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--modes", nargs="+", choices=("legacy", "compact", "binary"), default=["legacy", "compact", "binary"]
    )
    parser.add_argument("--voice-key", default="macos:Tingting")
    parser.add_argument("--moss-roundtrip", action="store_true")
    parser.add_argument("--moss-only", action="store_true")
    parser.add_argument("--tail-action", action="store_true")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--serial-log", required=True)
    args = parser.parse_args()
    if args.moss_only:
        args.modes = []
        args.moss_roundtrip = True
    recorder = Recorder(args.port, args.serial_log)
    import deskbot_server.main as server_main
    from deskbot_server.__main__ import cli

    factory = server_main.create_fastapi_app

    def create(runtime):
        app = factory(runtime)
        asyncio.create_task(verify(runtime, args, recorder), name="media-wire-verification")
        return app

    server_main.create_fastapi_app = create
    cli()


if __name__ == "__main__":
    main()
