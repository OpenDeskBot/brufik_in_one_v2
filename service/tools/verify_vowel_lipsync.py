"""Run the normal service with one bounded, selected-device vowel experiment.

No diagnostic HTTP route is installed. USB carries scalar diagnostics only;
audio, servo and expression traffic goes through the real server WebSocket.
After testing, hooks/diagnostics are removed and the normal service continues.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
import json
import math
import re
from pathlib import Path
import statistics
import threading
import time
import wave

import serial

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "service/tests/fixtures/lipsync"
SENTENCE = "你好，今天我们一起测试机器人的口型，看看声音和嘴巴是不是同步。"


def parse_diagnostic_line(line):
    """Accept a status JSON after an interleaved firmware logger prefix.

    The reply can interleave with either a header or a log body. Only the three
    known scalar status types qualify; unrelated JSON stays in the log.
    """
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {"line": line}
    except ValueError:
        pass
    marker = re.search(r'\{"type":"(?:resource_status|audio_status|lipsync_status)"', line)
    if marker and re.match(r"^\[[0-9:.]+\]\s+\[[A-Z ]+\]", line):
        try:
            value, _ = json.JSONDecoder().raw_decode(line[marker.start():])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    return {"line": line}


class Recorder:
    def __init__(self, port, path):
        self.serial = serial.Serial(port, 115200, timeout=.05, write_timeout=2)
        self.file = Path(path).open("ab", buffering=0)
        self.events = deque(maxlen=12000)
        self.lock = threading.Lock()
        self.writing = threading.Lock()
        self.stop_event = threading.Event()
        self.allow_continue = False
        self.ready = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def send(self, text):
        with self.writing:
            self.serial.write((text + "\n").encode())

    def run(self):
        buffer = b""; last = 0
        try:
            while not self.stop_event.is_set():
                data = self.serial.read(max(1, self.serial.in_waiting))
                if data:
                    self.file.write(data); buffer += data
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        line = raw.decode(errors="replace").strip()
                        if "ESP-ROM:" in line: self.ready = False
                        value = parse_diagnostic_line(line)
                        if value.get("type") == "resource_status" and value.get("ready"): self.ready = True
                        with self.lock: self.events.append({"host_mono": time.monotonic(), **value})
                if self.allow_continue and not self.ready and time.monotonic()-last > 3:
                    self.send("continue_boot"); self.send("resource_status"); last = time.monotonic()
        except Exception as exc:
            with self.lock: self.events.append({"host_mono": time.monotonic(), "serial_error": str(exc)})

    def since(self, start):
        with self.lock: return [event.copy() for event in self.events if event["host_mono"] >= start]

    async def query(self, command, kind=None, timeout=5):
        start = time.monotonic(); self.send(command)
        async with asyncio.timeout(timeout):
            while True:
                for event in self.since(start):
                    if event.get("type") == (kind or command): return event
                await asyncio.sleep(.02)

    async def mode(self, mode):
        self.send("lipsync_mode " + mode)
        status = await self.query("lipsync_status")
        assert status["mode"] == ("rms" if mode == "rms" else "mfcc"), status

    def close(self):
        self.stop_event.set(); self.thread.join(2); self.serial.close(); self.file.close()


def pcm_file(path):
    with wave.open(str(path), "rb") as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
        data = source.readframes(source.getnframes())
    return data[:len(data)//32*32]


def quantiles(values):
    values = sorted(values)
    return {"count": len(values), "median": statistics.median(values) if values else None,
            "p95": values[int(.95*(len(values)-1))] if values else None,
            "max": max(values) if values else None}


async def verify(runtime, args, recorder):
    from deskbot_server.model.pb_seq import PbBlock, PbSeq, PbType
    from deskbot_server.service.pipeline.opus_downlink import encode_pcm_s16le_to_opus_batch
    import deskbot_server.service.device_ws_service as ws_module

    hub = runtime.device_ws
    report = {"device_id": args.device_id, "completed": False, "phase": "starting", "cases": [], "snapshots": [],
              "measurement": "actual ROM DMA, display-push and task diagnostics; not acoustic/optical measurement",
              "serial_log": args.serial_log, "uplink": {"audio_packets": 0, "camera_frames": 0, "jpeg_bytes": 0},
              "soak_requested_seconds": args.soak_seconds}
    events = deque(maxlen=6000)
    original_ack, original_camera = hub.record_pb_ack, hub._accept_camera_frame
    original_audio = ws_module.note_uplink_audio
    active_req = ""
    saved_volume = None
    feedback_guard = None
    report["feedback_guard"] = {"scope": "only diagnostic playback requests; microphone, AFE, uplink and ASR remain active",
                                "preview_texts": 0, "recognized_segments": 0}

    def save():
        destination = Path(args.output)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
        temporary.replace(destination)

    async def record(dev, ack):
        if dev == args.device_id: events.append({**ack, "host_mono": time.monotonic()})
        await original_ack(dev, ack)

    def camera(dev, media, *, source):
        if dev == args.device_id:
            report["uplink"]["camera_frames"] += 1
            report["uplink"]["jpeg_bytes"] += len(media)
        return original_camera(dev, media, source=source)

    def audio(dev):
        if dev == args.device_id: report["uplink"]["audio_packets"] += 1
        return original_audio(dev)

    def pairs(req, pcm, rate, index=0, optional=False, volume=None):
        blob, count = encode_pcm_s16le_to_opus_batch(pcm, rate)
        message = {"type": "pb_single", "req": req, "idx": 0, "sr": rate, "ch": 1, "fmt": "opus",
                   "chunk_ms": len(pcm)*1000//(rate*2), "audio": {"next_bin_len": len(blob), "frames": count}}
        if optional:
            message.update(anim_version=entry.anim_version,
                           anim=[{"anim_id": "curious", "frame": index % 4, "ms": 300}],
                           servo=[{"xm": 1, "ym": 2, "x": 2, "y": 0, "ms": 150},
                                  {"xm": 1, "ym": 2, "x": -2, "y": 0, "ms": 150}])
        if volume is not None: message["volume"] = volume
        return [(message, [blob])]

    async def play(name, pcm, *, key="macos:Tingting", mode="mfcc", rate=16000, optional=True, volume=40):
        nonlocal active_req
        await recorder.mode(mode)
        before = await recorder.query("lipsync_status")
        recorder.send("mouth_trace 60000")
        start = time.monotonic(); active_req = "vowel_" + name
        hub.note_spoken_text(args.device_id, active_req, SENTENCE + "啊衣呜诶哦")
        stream = await hub.open_speech_stream(args.device_id, active_req, sr=rate, fmt="opus", level=3, voice_key=key)
        if stream is None: raise RuntimeError("device stream unavailable")
        chunk = rate*2*400//1000
        for i, offset in enumerate(range(0, len(pcm), chunk)):
            await stream.append(pairs(active_req, pcm[offset:offset+chunk], rate, i, optional and i % 10 == 0,
                                      volume if i == 0 else None))
        outcome = await stream.finish()
        await asyncio.sleep(.15)
        samples = [e for e in recorder.since(start) if e.get("usb") == "mouth"]
        ack = next((e for e in events if e.get("req") == active_req and e.get("ack_type") == "pb_started"), None)
        lag = [(e["us"]-e["audio_us"])/1000 for e in samples if e.get("active") and not e.get("underrun") and e.get("audio_us")]
        after = await recorder.query("lipsync_status")
        item = {"name": name, "voice_key": key, "mode": mode, "rate": rate, "outcome": outcome.status.value,
                "audio_ms": len(pcm)*1000/(rate*2), "wall_ms": (time.monotonic()-start)*1000,
                "first_dma_ack_ms": (ack["host_mono"]-start)*1000 if ack else None,
                "display_dma_lag_ms": quantiles(lag), "mouth_classes": dict(Counter(e.get("vowel") for e in samples)),
                "profiles_shown": sorted({e.get("profile", "") for e in samples}),
                "closed_after_end": any(not e.get("active") and e.get("shown") == 0 for e in samples),
                "analysis_frames": after["frames"]-before["frames"], "analysis_before": before, "analysis_after": after}
        report["cases"].append(item); save(); active_req = ""
        if not outcome.completed: raise RuntimeError(f"{name}: {outcome}")
        return item

    hub.record_pb_ack, hub._accept_camera_frame, ws_module.note_uplink_audio = record, camera, audio
    try:
        await asyncio.sleep(2); recorder.allow_continue = True
        async with asyncio.timeout(150):
            while True:
                entry = hub._devices.get(args.device_id)
                if entry is not None and not entry.stopped and "mfcc_vowel_v1" in entry.capabilities: break
                await asyncio.sleep(.2)
        for attempt in range(2):
            status = await recorder.query("lipsync_status")
            if status.get("ready") and len(status.get("profiles", [])) >= 2: break
            if attempt == 1: raise RuntimeError(f"calibration package not active: {status}")
            recorder.send("reboot"); await asyncio.sleep(15)
            async with asyncio.timeout(120):
                while entry.stopped or not recorder.ready: await asyncio.sleep(.2)
        if not status.get("fft_ok"): raise RuntimeError(f"DSP self-test failed: {status}")
        report["initial"] = status
        report["resource"] = await recorder.query("resource_status")
        saved_volume = (await recorder.query("audio_status"))["volume"]
        report["initial_audio"] = await recorder.query("audio_status")
        # Benchmark echo must not turn into unrelated application turns. The
        # first unguarded run is recorded separately. Recognition still runs;
        # explicit PB cancellation is tested below. This is not a barge-in test.
        guard = entry.echo_guard
        original_fragment, original_reject = guard.spoken_fragment, guard.reject
        def fragment(text, req):
            if active_req and req == active_req:
                report["feedback_guard"]["preview_texts"] += 1
                return True
            return original_fragment(text, req)
        def reject(text, **kwargs):
            if active_req and entry.sending_seq and entry.sending_seq.req == active_req:
                report["feedback_guard"]["recognized_segments"] += 1
                return "diagnostic_speaker_feedback"
            return original_reject(text, **kwargs)
        guard.spoken_fragment, guard.reject = fragment, reject
        feedback_guard = (guard, original_fragment, original_reject)
        await hub.interrupt_current_playback(args.device_id, reason="vowel_verification_start")
        async with asyncio.timeout(10):
            while entry.sending_seq is not None: await asyncio.sleep(.02)
        recorder.send("log 2"); recorder.send("lipsync_profile auto"); recorder.send("cpu_stats")
        sentence = pcm_file(FIXTURES / "heldout-tingting/sentence.wav")
        await play("warmup", sentence[:12800], mode="rms", optional=False)
        report["phase"] = "baseline"; save()
        for i in range(3):
            await play(f"rms_{i}", sentence, mode="rms", optional=True)
            await play(f"mfcc_{i}", sentence, optional=True)
        report["phase"] = "vowels_and_voices"; save()
        for folder, key in (("tingting", "macos:Tingting"), ("meijia", "macos:Meijia"), ("heldout-tingting", "macos:Tingting")):
            sequence = b"".join(pcm_file(FIXTURES / folder / f"{label}.wav") + bytes(16000*2//5) for label in "AIUEO")
            await play(folder, sequence, key=key)
        unknown = await play("unknown_voice", sentence, key="unregistered:voice")
        assert unknown["analysis_frames"] == 0 and not any(unknown["mouth_classes"].get(k) for k in "AIUEO"), unknown
        untagged = await play("untagged", sentence, key="")
        assert untagged["analysis_frames"] == 0, untagged
        from deskbot_server.utils.audio_resample import upsample_pcm16_mono
        await play("48k", upsample_pcm16_mono(sentence, 16000, 48000), rate=48000)
        report["phase"] = "cancel"; save()
        await recorder.mode("mfcc")
        active_req = "vowel_cancel"
        stream = await hub.open_speech_stream(args.device_id, active_req, sr=16000, fmt="opus", level=3, voice_key="macos:Tingting")
        await stream.append(pairs(active_req, sentence[:12800], 16000))
        async with asyncio.timeout(5):
            while not any(e.get("req") == active_req and e.get("ack_type") == "pb_started" for e in events): await asyncio.sleep(.01)
        await asyncio.sleep(.1); cancel_at = time.monotonic()
        await hub.interrupt_current_playback(args.device_id, reason="vowel_verification", expected_req=active_req)
        async with asyncio.timeout(5):
            await stream.seq._done.wait()
            while not any(e.get("req") == active_req and e.get("ack_type") == "pb_cancelled" for e in events): await asyncio.sleep(.01)
        stopped = next(e for e in events if e.get("req") == active_req and e.get("ack_type") == "pb_cancelled")
        report["cancel_ack_ms"] = (stopped["host_mono"]-cancel_at)*1000; active_req = ""
        await play("after_cancel", sentence)
        report["phase"] = "soak"; report["soak_start"] = time.time(); save()
        recorder.send("mouth_trace 0")
        soak_before = await recorder.query("audio_status")
        active_req = "vowel_soak"
        stream = await hub.open_speech_stream(args.device_id, active_req, sr=16000, fmt="opus", level=3, voice_key="macos:Tingting")
        cycle = sentence + bytes((-len(sentence)) % 12800)
        start = time.monotonic(); finished = asyncio.Event()

        async def monitor():
            while not finished.is_set():
                await asyncio.sleep(10)
                stats = await recorder.query("lipsync_status")
                out = await recorder.query("audio_status")
                report["snapshots"].append({"elapsed_s": time.monotonic()-start, "lipsync": stats,
                                             "audio": out, "uplink": report["uplink"].copy()})
                report["soak_elapsed_s"] = time.monotonic()-start
                hub.note_spoken_text(args.device_id, active_req, SENTENCE)
                save()

        watcher = asyncio.create_task(monitor())
        index = 0
        try:
            while time.monotonic()-start < args.soak_seconds:
                # Preserve encoder continuity across repetitions, just as the
                # live producer does; cached Opus packets cannot be looped.
                offset = (index % (len(cycle)//12800))*12800
                batch = pairs(active_req, cycle[offset:offset+12800], 16000, index,
                              optional=index % 10 == 0, volume=20 if index == 0 else None)
                await stream.append(batch); index += 1
            async with asyncio.timeout(15):
                outcome = await stream.finish()
            report["soak_outcome"] = outcome.status.value
            report["soak_audio_ms"] = stream.pts_ms
            report["soak_wall_ms"] = (time.monotonic()-start)*1000
            assert outcome.completed, outcome
        finally:
            finished.set(); watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        active_req = ""
        final_audio = await recorder.query("audio_status")
        report["soak_content_gap_periods"] = final_audio["content_gap_periods"]-soak_before["content_gap_periods"]
        report["final_audio"] = final_audio
        report["final"] = await recorder.query("lipsync_status")
        assert report["uplink"]["audio_packets"] > 0 and report["uplink"]["camera_frames"] > 0, report["uplink"]
        report["completed"] = True; report["phase"] = "complete"
    except asyncio.CancelledError:
        report["error"] = "verification interrupted by live input or service shutdown"
        raise
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if active_req:
            await hub.interrupt_current_playback(args.device_id, reason="vowel_verification_failed", expected_req=active_req)
    finally:
        if feedback_guard:
            guard, original_fragment, original_reject = feedback_guard
            guard.spoken_fragment, guard.reject = original_fragment, original_reject
        if saved_volume is not None:
            restore = PbSeq(req="vowel_restore_volume", level=3, entries=(PbBlock(type=PbType.SINGLE,
                req="vowel_restore_volume", idx=0, chunk_ms=0, volume=saved_volume),))
            await hub.send(args.device_id, restore)
        recorder.send("mouth_trace 0"); recorder.send("cpu_stats_stop"); recorder.send("lipsync_profile auto")
        recorder.send("lipsync_mode auto"); recorder.send("log 2")
        hub.record_pb_ack, hub._accept_camera_frame, ws_module.note_uplink_audio = original_ack, original_camera, original_audio
        save(); recorder.close()
        print("VOWEL_CHECK " + json.dumps({k: v for k, v in report.items() if k not in ("cases", "snapshots")}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--serial-log", required=True)
    parser.add_argument("--soak-seconds", type=int, default=1800)
    args = parser.parse_args()
    recorder = Recorder(args.port, args.serial_log)
    import deskbot_server.main as server_main
    from deskbot_server.__main__ import cli
    factory = server_main.create_fastapi_app

    def create(runtime):
        app = factory(runtime)
        asyncio.create_task(verify(runtime, args, recorder), name="vowel-physical-verification")
        return app

    server_main.create_fastapi_app = create
    cli()


if __name__ == "__main__":
    main()
