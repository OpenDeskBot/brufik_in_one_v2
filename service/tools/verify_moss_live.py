"""Opt-in real MOSS verification with generated WAV and a simulated ROM clock.

No microphone/camera capture, no live device selection or credential persistence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import wave
from collections import Counter
from pathlib import Path

from deskbot_server.client.moss_live import MossLiveClient
from deskbot_server.model.pb_seq import PbType
from deskbot_server.model.realtime import MossLiveConfig
from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry
from deskbot_server.service.moss_live_service import MossLiveBridge


async def verify(args):
    key = os.environ.get("MOSS_API_KEY", "")
    if args.env_file:
        for line in Path(args.env_file).read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "MOSS_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        raise ValueError("Set MOSS_API_KEY or --env-file")
    events = Counter()
    receipts = Counter()
    durations = []

    class Client(MossLiveClient):
        async def receive(self):
            event = await super().receive()
            events[event.action] += 1
            if event.audio:
                durations.append(len(event.audio) / 96)
            return event

        async def send(self, value):
            if isinstance(value, dict) and value.get("action", "").startswith("playback."):
                receipts[value["action"]] += 1
            await super().send(value)

    class Rom:
        closed = False

        async def close(self, **kw):
            self.closed = True

    DeviceWsService.reset_instance()
    hub = DeviceWsService()
    rom = Rom()
    entry = _DeviceEntry(device_id="simulated-moss-verifier", ws=rom, generation=1, capabilities={"pb_stream_v1"})
    hub._devices[entry.device_id] = entry
    client = Client(MossLiveConfig(key))
    bridge = MossLiveBridge(hub, entry.device_id, rom, client=client)
    entry.conversation_session = bridge
    playback = asyncio.Queue()
    cancelled = set()
    played = {}

    async def send(device_id, block, **kw):
        ack = {"type": "pb_ack", "req": block.req, "epoch": block.epoch, "idx": block.idx}
        if block.type == PbType.CANCEL:
            cancelled.add(block.req)
            bridge.offer_ack({**ack, "ack_type": "pb_cancelled", "played_ms": played.get(block.req, 0)})
        else:
            playback.put_nowait(block)
            if block.type != PbType.END:
                entry.ack_queue.put_nowait({**ack, "ack_type": "pb_credit"})
        return True

    hub._do_send_to_device = send

    async def play():
        while True:
            block = await playback.get()
            if block.req in cancelled:
                continue
            ack = {"type": "pb_ack", "req": block.req, "epoch": block.epoch, "idx": block.idx}
            if block.type == PbType.END:
                entry.ack_queue.put_nowait({**ack, "ack_type": "pb_end"})
            else:
                await asyncio.sleep(block.chunk_ms / 1000)
                if block.req in cancelled:
                    continue
                played[block.req] = block.pts_ms + block.chunk_ms
                bridge.offer_ack({**ack, "ack_type": "pb_progress", "played_ms": played[block.req]})

    workers = [asyncio.create_task(hub._device_loop(entry.device_id, 1)), asyncio.create_task(play())]
    result = {
        "kind": "real_moss_with_simulated_rom",
        "real_rom": False,
        "microphone_capture": False,
        "camera_capture": False,
    }
    start = time.monotonic()
    try:
        await bridge.start()
        result["handshake"] = "passed"
        if args.wav:
            with wave.open(args.wav, "rb") as source:
                if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
                    raise ValueError("WAV must be mono 16kHz PCM16")
                audio = source.readframes(source.getnframes())
            # Silence around the generated sentence allows upstream VAD to close.
            for i in range(0, len(audio) + 32000, 640):
                bridge.offer_audio({"codec": "s16le", "sr": 16000, "ch": 1}, audio[i : i + 640].ljust(640, b"\x00"))
                await asyncio.sleep(0.02)
            async with asyncio.timeout(args.timeout):
                while not receipts["playback.completed"]:
                    if rom.closed:
                        raise RuntimeError("session closed before completed playback")
                    await asyncio.sleep(0.05)
            result["generated_audio_to_response"] = "passed"
        else:
            for _ in range(5):
                await client.send_audio(bytes(640))
        result["events"] = dict(events)
        result["receipts"] = dict(receipts)
        result["received_audio_ms"] = round(sum(durations), 2)
        result["elapsed_s"] = round(time.monotonic() - start, 2)
    except Exception as exc:
        result["error_kind"] = type(exc).__name__
        result["error_code"] = getattr(exc, "code", None)
    finally:
        await bridge.close()
        for t in workers:
            t.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 1 if "error_kind" in result else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file")
    p.add_argument("--wav")
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--report", default="/private/tmp/deskbot-moss-live-verification.json")
    raise SystemExit(asyncio.run(verify(p.parse_args())))
