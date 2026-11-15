"""Start the normal server and run a one-shot physical playback regression.

No test HTTP endpoint is installed. The ordinary server remains available after
this local diagnostic task completes. Only explicitly selected device is driven.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import time
from dataclasses import replace
from pathlib import Path

from deskbot_server.model.pb_seq import PbSeq, PbType


def pairs(req: str, phrase_index: int):
    sr, ms = 16000, 500
    # Quiet test tone, short head motion, visible expression. Animation is
    # deliberately 300ms longer than audio to expose cross-lane barriers.
    pcm = b"".join(struct.pack("<h", int(1600*math.sin(2*math.pi*330*i/sr))) for i in range(sr*ms//1000))
    msg = {"type": "pb_single", "req": req, "idx": 0, "chunk_ms": ms,
           "sr": sr, "ch": 1, "fmt": "s16le", "audio": {"next_bin_len": len(pcm)},
           "servo": [{"xm": 0, "ym": 2, "x": 88 if phrase_index % 2 else 92, "y": 0, "ms": 300}],
           "anim": [{"ms": 800, "elements": {"mouth": [
               {"shape": "rect", "x": 120, "y": 155, "w": 40, "h": 8+phrase_index*4, "c": 65535}]}}]}
    return [(msg, [pcm])]


async def verify(runtime, device_id, output):
    hub = runtime.device_ws
    events, tx = [], []
    original_ack, original_send = hub.record_pb_ack, hub._do_send_to_device

    async def record(dev, ack):
        if dev == device_id:
            events.append({**ack, "host_mono": time.monotonic()})
        await original_ack(dev, ack)

    async def send(dev, block, **kwargs):
        if dev == device_id:
            tx.append({"req": block.req, "idx": block.idx, "type": block.type.wire,
                       "epoch": block.epoch, "pts_ms": block.pts_ms, "host_mono": time.monotonic()})
        return await original_send(dev, block, **kwargs)

    hub.record_pb_ack, hub._do_send_to_device = record, send
    live = hub._live_service
    hub._live_service = None
    report = {"device_id": device_id, "measurement": "real hardware; I2S submission/ACK timing, not acoustic microphone recording",
              "rounds": [], "events": events, "tx": tx}
    try:
        async with asyncio.timeout(120):
            while True:
                entry = hub._devices.get(device_id)
                if entry is not None and "pb_stream_v1" in entry.capabilities:
                    break
                await asyncio.sleep(.2)
        await hub.interrupt_current_playback(device_id, reason="physical_verification")
        async with asyncio.timeout(10):
            while entry.sending_seq is not None:
                await asyncio.sleep(.02)
        warmup = PbSeq.from_wire_pairs(pairs("verify_warmup", 0), level=3)
        await hub.send_and_wait(device_id, warmup)
        await asyncio.sleep(.3)
        for mode in ("legacy", "continuous", "legacy", "continuous", "legacy", "continuous"):
            req = f"verify_{mode}_{len(report['rounds'])}"
            start = time.monotonic()
            if mode == "legacy":
                outcomes = []
                for index in range(3):
                    seq = PbSeq.from_wire_pairs(pairs(f"{req}_{index}", index), level=3)
                    outcomes.append((await hub.send_and_wait(device_id, seq)).status.value)
            else:
                stream = await hub.open_speech_stream(device_id, req, sr=16000, fmt="s16le", level=3)
                for index in range(3):
                    await stream.append(pairs(req, index))
                outcomes = [(await stream.finish()).status.value]
            report["rounds"].append({"mode": mode, "req": req, "wall_ms": (time.monotonic()-start)*1000,
                                      "audio_ms": 1500, "outcomes": outcomes})
            await asyncio.sleep(.5)
        # Preempt while the producer itself is idle, then play a new response.
        stream = await hub.open_speech_stream(device_id, "verify_cancel_old", sr=16000, fmt="s16le", level=3)
        await stream.append(pairs("cancel", 0))
        async with asyncio.timeout(5):
            while not any(e.get("req") == stream.seq.req and e.get("ack_type") == "pb_started" for e in events):
                await asyncio.sleep(.005)
        cancel_at = time.monotonic()
        await hub.interrupt_current_playback(device_id, reason="physical_verification_cancel")
        await stream.seq._done.wait()
        async with asyncio.timeout(5):
            while not any(e.get("req") == stream.seq.req and e.get("ack_type") == "pb_cancelled" for e in events):
                await asyncio.sleep(.005)
        cancelled = next(e for e in events if e.get("req") == stream.seq.req and e.get("ack_type") == "pb_cancelled")
        report["cancel_ack_ms"] = (cancelled["host_mono"]-cancel_at)*1000
        newer = await hub.open_speech_stream(device_id, "verify_cancel_new", sr=16000, fmt="s16le", level=3)
        await newer.append(pairs("new", 1))
        async with asyncio.timeout(5):
            while not any(e.get("req") == newer.seq.req and e.get("ack_type") == "pb_started" for e in events):
                await asyncio.sleep(.005)
        # Inject a delayed packet and cancellation from the old epoch. Neither
        # may cancel or contaminate the new response.
        stale = replace(PbSeq.from_wire_pairs(pairs("verify_cancel_old", 0)).entries[0],
                        epoch=stream.seq.epoch, stream=True, type=PbType.CHUNK, idx=99)
        await original_send(device_id, stale, sr=16000, fmt="s16le", ch=1, level=3)
        await original_send(device_id, replace(stale, type=PbType.CANCEL, binaries=(), audio=None))
        report["new_after_cancel"] = (await newer.finish()).status.value
        report["cancel_trials_ms"] = [report["cancel_ack_ms"]]
        for trial in range(4):
            test = await hub.open_speech_stream(device_id, f"verify_interrupt_{trial}", sr=16000, fmt="s16le", level=3)
            await test.append(pairs(test.seq.req, trial % 2))
            async with asyncio.timeout(5):
                while not any(e.get("req") == test.seq.req and e.get("ack_type") == "pb_started" for e in events):
                    await asyncio.sleep(.005)
            await asyncio.sleep(.01 * trial)
            requested = time.monotonic()
            await hub.interrupt_current_playback(device_id, reason="physical_cancel_repeat")
            await test.seq._done.wait()
            async with asyncio.timeout(5):
                while not any(e.get("req") == test.seq.req and e.get("ack_type") == "pb_cancelled" for e in events):
                    await asyncio.sleep(.005)
            stopped = next(e for e in events if e.get("req") == test.seq.req and e.get("ack_type") == "pb_cancelled")
            report["cancel_trials_ms"].append((stopped["host_mono"]-requested)*1000)

        from deskbot_server.service.application.ws_chat_turn import run_ws_chat_turn
        report["dialogues"] = []
        for attempt in range(3):
            req = f"verify_dialogue_{attempt}"
            dialogue_start = time.monotonic()
            dialogue = await run_ws_chat_turn(entry.ws, runtime.chat, "请用一句话回答：在吗？", request_id=req,
                                               device_ws=hub, device_id=device_id, bus_service=runtime.bus_service)
            first = next((e for e in events if e.get("req") == req and e.get("ack_type") == "pb_started"), None)
            report["dialogues"].append({"input": "text fixture, ASR excluded", "req": req, "reply": dialogue.get("llm_text"),
                                      "status": dialogue.get("playback_status"), "error": dialogue.get("error"),
                                      "text_to_first_i2s_ack_ms": (first["host_mono"]-dialogue_start)*1000 if first else None})
        report["dialogue"] = report["dialogues"][0]
        report["completed"] = all(x == "completed" for r in report["rounds"] for x in r["outcomes"]) and report["new_after_cancel"] == "completed" and all(d["status"] == "completed" for d in report["dialogues"])
    except asyncio.CancelledError:
        report["completed"] = False
        report["interrupted_by_live_input"] = True
        # The user now owns the device. Old diagnostic cleanup must not clear
        # their replacement response.
    except Exception as exc:
        report["completed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        await hub.interrupt_current_playback(device_id, reason="verification_failed")
    finally:
        hub.record_pb_ack, hub._do_send_to_device = original_ack, original_send
        hub._live_service = live
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k not in ("events", "tx")}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    import deskbot_server.main as server_main
    from deskbot_server.__main__ import cli
    factory = server_main.create_fastapi_app

    def create(runtime):
        app = factory(runtime)
        asyncio.create_task(verify(runtime, args.device_id, args.output), name="physical-playback-verification")
        return app

    server_main.create_fastapi_app = create
    cli()


if __name__ == "__main__":
    main()
