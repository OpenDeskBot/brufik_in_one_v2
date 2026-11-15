"""Run the ordinary server with a one-shot, explicitly selected ROM check.

Exercises downloaded expression IDs, small relative servo moves, audio-only
streaming and cancellation. No diagnostic HTTP endpoint or raw audio capture.
The normal server remains running after the check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path


async def verify(runtime, device_id: str, output: str, delay: float):
    from deskbot_server.model.pb_seq import PbBlock, PbSeq, PbServo, PbType
    from deskbot_server.service.pb_service import PbService

    hub = runtime.device_ws
    events, sent = [], []
    original_ack, original_send = hub.record_pb_ack, hub._do_send_to_device
    report = {"device_id": device_id, "measurement": "real ROM acknowledgements and DMA/display diagnostics; no acoustic or optical capture",
              "events": events, "sent": sent, "completed": False}

    async def record(dev, ack):
        if dev == device_id:
            events.append({**ack, "host_mono": time.monotonic()})
        await original_ack(dev, ack)

    async def send(dev, block, **kwargs):
        if dev == device_id:
            sent.append({"req": block.req, "idx": block.idx, "type": block.type.wire,
                         "epoch": block.epoch, "anim_id": block.anim_id,
                         "animation_references": [a.anim_id for a in block.anim if a.anim_id],
                         "inline_primitive_count": sum(len(items) for a in block.anim for items in a.elements.values()),
                         "servo_count": len(block.servo),
                         "audio_bytes": sum(len(b) for b in block.binaries), "host_mono": time.monotonic()})
        return await original_send(dev, block, **kwargs)

    hub.record_pb_ack, hub._do_send_to_device = record, send
    try:
        async with asyncio.timeout(180):
            while True:
                entry = hub._devices.get(device_id)
                if entry is not None and {"pb_stream_v1", "local_face_pack_v1", "local_idle_v1"} <= entry.capabilities:
                    break
                await asyncio.sleep(.2)
        report.update(version=entry.anim_version, capabilities=sorted(entry.capabilities))
        await asyncio.sleep(delay)
        print("RESOURCE_CHECK begin", flush=True)
        seq = PbService().get_scene_pb_seq("curious", device_id=device_id, level=3)
        if seq is None:
            raise RuntimeError("curious expression missing")
        moves = (PbServo(xm=1, ym=2, x=3, ms=200), PbServo(xm=1, ym=2, x=-3, ms=200))
        report["expression_id"] = (await hub.send_and_wait(device_id, seq)).status.value
        servo = PbSeq(req="resource_servo", level=3, entries=(PbBlock(
            type=PbType.SINGLE, req="resource_servo", idx=0, chunk_ms=400, servo=moves),))
        report["servo"] = (await hub.send_and_wait(device_id, servo)).status.value
        with wave.open(str(Path(__file__).resolve().parents[1] / "tests/fixtures/vad-speech-tingting.wav")) as f:
            if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != (1, 2, 16000):
                raise ValueError("fixture must be 16kHz mono PCM16")
            pcm = f.readframes(f.getnframes())

        async def append_audio(stream, optional=False):
            for offset in range(0, len(pcm), 12800):
                chunk = pcm[offset:offset + 12800]
                msg = {"type": "pb_single", "req": stream.seq.req, "idx": 0,
                       "chunk_ms": len(chunk) // 32, "sr": 16000, "ch": 1, "fmt": "s16le",
                       "audio": {"next_bin_len": len(chunk)}}
                if optional and offset == 0:
                    msg.update(anim_version=entry.anim_version,
                               anim=[{"anim_id": "curious", "frame": 0, "ms": len(chunk) // 32}],
                               servo=[move.to_wire() for move in moves])
                await stream.append([(msg, [chunk])])

        stream = await hub.open_speech_stream(device_id, "resource_audio_only", sr=16000, fmt="s16le", level=3)
        if stream is None:
            raise RuntimeError("device stream unavailable")
        await append_audio(stream)
        report["audio_only"] = (await stream.finish()).status.value
        mixed = await hub.open_speech_stream(device_id, "resource_audio_optional", sr=16000, fmt="s16le", level=3)
        await append_audio(mixed, optional=True)
        report["audio_optional"] = (await mixed.finish()).status.value
        old = await hub.open_speech_stream(device_id, "resource_cancel_old", sr=16000, fmt="s16le", level=3)
        await append_audio(old)
        async with asyncio.timeout(10):
            while not any(e.get("req") == old.seq.req and e.get("ack_type") == "pb_started" for e in events):
                await asyncio.sleep(.01)
        start = time.monotonic()
        await hub.interrupt_current_playback(device_id, reason="resource_verification")
        async with asyncio.timeout(10):
            await old.seq._done.wait()
            while not any(e.get("req") == old.seq.req and e.get("ack_type") == "pb_cancelled" for e in events):
                await asyncio.sleep(.01)
        cancelled = next(e for e in events if e.get("req") == old.seq.req and e.get("ack_type") == "pb_cancelled")
        report["cancel_ack_ms"] = (cancelled["host_mono"] - start) * 1000
        new = await hub.open_speech_stream(device_id, "resource_cancel_new", sr=16000, fmt="s16le", level=3)
        await append_audio(new)
        report["replacement"] = (await new.finish()).status.value
        report["completed"] = all(report[k] == "completed" for k in ("expression_id", "servo", "audio_only", "audio_optional", "replacement"))
    except asyncio.CancelledError:
        report["error"] = "interrupted by live input or shutdown"
        raise
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        hub.record_pb_ack, hub._do_send_to_device = original_ack, original_send
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("RESOURCE_CHECK " + json.dumps({k: v for k, v in report.items() if k not in ("events", "sent")}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--delay", type=float, default=20)
    args = parser.parse_args()
    import deskbot_server.main as server_main
    from deskbot_server.__main__ import cli
    factory = server_main.create_fastapi_app

    def create(runtime):
        app = factory(runtime)
        asyncio.create_task(verify(runtime, args.device_id, args.output, args.delay), name="resource-verification")
        return app

    server_main.create_fastapi_app = create
    cli()


if __name__ == "__main__":
    main()
