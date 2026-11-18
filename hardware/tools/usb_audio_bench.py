#!/usr/bin/env python3
"""USB-only playback, microphone/AEC capture, and mouth timing measurements.
Run with service/.venv/bin/python (numpy + pyserial). No server required.
"""

from __future__ import annotations
import argparse
from datetime import datetime
import json
import math
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import serial
import serial.tools.list_ports

MAGIC = b"DBAUD001"
MAGIC2 = b"DBAUD002"
TRACE_BYTES = 1304
SR = 16000


class UsbBench:
    def __init__(self, port):
        self.link = serial.Serial(port, 115200, timeout=0.001, write_timeout=2)
        self.buffer = bytearray()
        self.events, self.frames, self.logs = [], [], []
        self.bad_checksums = 0
        self.epoch = int(time.time() * 1000) & 0x7FFFFFFF

    def close(self):
        self.link.close()

    def command(self, command):
        wire = command.encode() + b"\n"
        for off in range(0, len(wire), 1024):
            self.link.write(wire[off : off + 1024])
            self.pump(0.0001)

    def pump(self, seconds=0.02):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.buffer.extend(self.link.read(max(1, self.link.in_waiting)))
            while self.buffer:
                if self.buffer.startswith((MAGIC, MAGIC2)):
                    compact = self.buffer.startswith(MAGIC2)
                    length = 664 if compact else TRACE_BYTES
                    half = 320 if compact else 640
                    if len(self.buffer) < length:
                        break
                    frame = bytes(self.buffer[:length])
                    (checksum,) = struct.unpack_from("<I", frame, length - 4)
                    if sum(frame[:-4]) & 0xFFFFFFFF != checksum:
                        self.bad_checksums += 1
                        del self.buffer[0]
                        continue
                    seq, ms, flags = struct.unpack_from("<III", frame, 8)
                    self.frames.append(
                        dict(
                            seq=seq,
                            ms=ms,
                            flags=flags,
                            rate=8000 if compact else SR,
                            raw=np.frombuffer(
                                frame[20 : 20 + half], dtype="<i2"
                            ).copy(),
                            processed=np.frombuffer(
                                frame[20 + half : 20 + 2 * half], dtype="<i2"
                            ).copy(),
                        )
                    )
                    del self.buffer[:length]
                    continue
                positions = [self.buffer.find(m) for m in (MAGIC, MAGIC2)]
                magic = min((x for x in positions if x >= 0), default=-1)
                newline = self.buffer.find(b"\n")
                if magic >= 0 and (newline < 0 or magic < newline):
                    del self.buffer[:magic]
                    continue
                if newline < 0:
                    break
                line = bytes(self.buffer[:newline]).decode(errors="replace").strip()
                del self.buffer[: newline + 1]
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        self.events.append(item)
                except (ValueError, TypeError):
                    if line:
                        self.logs.append(line)

    def wait(self, predicate, timeout=5, start=0):
        cursor = start
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            end = len(self.events)
            for event in self.events[cursor:end]:
                if predicate(event):
                    return event
            cursor = end
            self.pump()
        raise TimeoutError("USB response deadline exceeded")

    def status(self):
        start = len(self.events)
        for _ in range(3):
            self.command("usb_status")
            try:
                return self.wait(lambda e: e.get("usb") == "ready", 2, start=start)
            except TimeoutError:
                pass
        raise TimeoutError("No USB status reply")

    def pb(self, message, pcm=b""):
        # Same packed PB bytes as WebSocket, with a USB envelope. No base64
        # expansion or large JSON-string parse in the real-time receiver.
        header = json.dumps(message, separators=(",", ":")).encode()
        packed = struct.pack("!I", len(header)) + header + pcm
        start = len(self.events)
        self.link.write(b"DBPB0001" + struct.pack("!I", len(packed)) + packed)
        queued = self.wait(
            lambda e: (
                e.get("usb") == "queued" and e.get("idx") == message.get("idx", 0)
            ),
            timeout=3,
            start=start,
        )
        if not queued.get("accepted"):
            raise RuntimeError(f"USB PB rejected: {queued}")

    def play(self, pcm, *, volume=35, anim="idle", cancel_at=None, sample_rate=SR):
        self.epoch += 1
        epoch = self.epoch
        req = f"usb-{epoch:x}"
        frames = len(pcm) // 2
        block_frames = sample_rate // 10
        count = math.ceil(frames / block_frames)
        started = time.monotonic()
        cancelled = False
        sent = 0
        for idx in range(count):
            due = max(0, idx * 0.1 - 0.25)
            while time.monotonic() - started < due:
                self.pump()
            if cancel_at is not None and time.monotonic() - started >= cancel_at:
                self.pb(dict(type="pb_cancel", req=req, epoch=epoch))
                cancelled = True
                break
            block = pcm[idx * block_frames * 2 : (idx + 1) * block_frames * 2]
            ms = len(block) // 2 * 1000 // sample_rate
            typ = (
                "pb_single"
                if count == 1
                else "pb_start"
                if idx == 0
                else "pb_end"
                if idx == count - 1
                else "pb_chunk"
            )
            message = dict(
                type=typ,
                req=req,
                idx=idx,
                level=3,
                epoch=epoch,
                stream=True,
                pts_ms=idx * 100,
                chunk_ms=ms,
                sr=sample_rate,
                ch=1,
                fmt="s16le",
                volume=volume,
                audio=dict(next_bin_len=len(block)),
            )
            if idx == 0:
                message["anim"] = [dict(anim_id=anim, frame=0, ms=ms)]
            self.pb(message, block)
            sent += len(block) // 2
            self.pump(0.01)
            errors = [
                e
                for e in self.events
                if (e.get("ack_type") == "pb_error" and e.get("req") == req)
                or (e.get("usb") == "queued" and not e.get("accepted"))
            ]
            if errors:
                raise RuntimeError(f"Device rejected playback: {errors[-1]}")
        terminal = "pb_cancelled" if cancelled else "pb_end"
        self.wait(
            lambda e: e.get("ack_type") == terminal and e.get("req") == req,
            timeout=frames / sample_rate + 5,
        )
        self.pump(0.7)
        return dict(
            epoch=epoch,
            req=req,
            cancelled=cancelled,
            samples_sent=sent,
            host_elapsed_s=time.monotonic() - started,
        )


def write_wav(path, pcm, rate=SR):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)


def noise(seconds, seed=330):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=int(seconds * SR))
    spectrum = np.fft.rfft(x)
    hz = np.fft.rfftfreq(len(x), 1 / SR)
    spectrum[(hz < 150) | (hz > 3400)] = 0
    x = np.fft.irfft(spectrum, len(x))
    return x / np.std(x) * 0.07


def double_talk_stimulus(out):
    """Local spoken instructions followed by quiet/echo windows, never cloud TTS."""
    for command in ("say", "afconvert"):
        if not shutil.which(command):
            raise RuntimeError(
                "中文引导需要 macOS 的 say 和 afconvert；其他系统可用 --wav"
            )
    prompts = [
        ("start_prompt", "第一轮，请准备。听到开始后，重复说屏幕上的句子。开始。"),
        ("double_talk_prompt", "第二轮，边听测试音，边重复同一句话。开始。"),
        ("stop_prompt", "测试结束。"),
    ]
    spoken = {}
    with tempfile.TemporaryDirectory(prefix="deskbot-cues-") as tmp:
        for name, text in prompts:
            aiff, wav = Path(tmp) / f"{name}.aiff", Path(tmp) / f"{name}.wav"
            subprocess.run(["say", "-v", "Tingting", "-o", str(aiff), text], check=True)
            subprocess.run(
                [
                    "afconvert",
                    "-f",
                    "WAVE",
                    "-d",
                    "LEI16@16000",
                    "-c",
                    "1",
                    str(aiff),
                    str(wav),
                ],
                check=True,
            )
            samples = np.frombuffer(stimulus("probe", wav), dtype="<i2").astype(float)
            if not len(samples) or not np.any(samples):
                raise RuntimeError("系统未生成中文提示音，请确认 Tingting 中文语音可用")
            spoken[name] = samples / max(np.max(np.abs(samples)), 1) * 0.65
    parts, phases = [], []
    position = 0
    for name, samples in [
        ("initial_quiet", np.zeros(int(0.4 * SR))),
        ("start_prompt", spoken["start_prompt"]),
        ("near_end_only", np.zeros(8 * SR)),
        ("double_talk_prompt", spoken["double_talk_prompt"]),
        ("double_talk", noise(8) * 0.7),
        ("stop_prompt", spoken["stop_prompt"]),
        ("final_quiet", np.zeros(SR)),
    ]:
        parts.append(samples)
        phases.append(
            dict(name=name, start=position / SR, end=(position + len(samples)) / SR)
        )
        position += len(samples)
    (out / "phases.json").write_text(json.dumps(phases, ensure_ascii=False, indent=2))
    return np.clip(np.concatenate(parts) * 32768, -32768, 32767).astype("<i2").tobytes()


def stimulus(kind, wav_path=None):
    if wav_path:
        with wave.open(str(wav_path), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (
                SR,
                1,
                2,
            ):
                raise ValueError("Test WAV must be PCM16 mono 16000 Hz")
            return wav.readframes(wav.getnframes())
    if kind == "noise":
        x = np.concatenate([np.zeros(SR), noise(13), np.zeros(SR)])
    else:
        x = noise(7)
        envelope = np.zeros(len(x))
        for start, end, gain in [
            (1, 1.5, 0.5),
            (2, 2.6, 1),
            (3.2, 3.5, 0.7),
            (4, 4.8, 1.4),
            (5.4, 6, 0.6),
        ]:
            envelope[int(start * SR) : int(end * SR)] = gain
        x *= envelope
    return np.clip(x * 32768, -32768, 32767).astype("<i2").tobytes()


def speech_band_rms(samples, rate):
    """Parseval RMS in 120–3400 Hz; DC/slow bias is not speech evidence."""
    if not len(samples):
        return 0.0
    spectrum = np.fft.rfft(samples - np.mean(samples))
    hz = np.fft.rfftfreq(len(samples), 1 / rate)
    weights = np.full(len(spectrum), 2.0)
    weights[0] = 1
    if len(samples) % 2 == 0:
        weights[-1] = 1
    band = (hz >= 120) & (hz <= 3400)
    return float(
        np.sqrt(np.sum(np.abs(spectrum[band]) ** 2 * weights[band])) / len(samples)
    )


def analyze(bench, result, pcm, volume, rate=SR):
    epoch = result["epoch"]
    mouth = [
        e for e in bench.events if e.get("usb") == "mouth" and e.get("epoch") == epoch
    ]
    valid = [e for e in mouth if e.get("audio_us", 0) and e.get("active")]
    capture = bench.frames
    gaps = sum(max(0, b["seq"] - a["seq"] - 1) for a, b in zip(capture, capture[1:]))
    bad = bench.bad_checksums
    stats = dict(
        result,
        trace_frames=len(capture),
        trace_sequence_gaps=gaps,
        trace_bad_checksums=bad,
        mouth_samples=len(mouth),
        diagnostic_sample_rate=capture[0]["rate"] if capture else None,
        afe_processed_fraction=float(np.mean([bool(f["flags"] & 1) for f in capture]))
        if capture
        else 0,
    )
    if capture:
        raw = np.concatenate([f["raw"] for f in capture]).astype(float)
        up = np.concatenate([f["processed"] for f in capture]).astype(float)
        stats.update(
            raw_rms=float(np.std(raw)),
            processed_rms=float(np.std(up)),
            processed_peak=int(np.max(np.abs(up))),
            raw_mean_pcm=float(np.mean(raw)),
            processed_mean_pcm=float(np.mean(up)),
            speech_band_hz=[120, 3400],
            raw_speech_band_rms=speech_band_rms(raw, capture[0]["rate"]),
            processed_speech_band_rms=speech_band_rms(up, capture[0]["rate"]),
        )
        if np.std(up) > 0:
            stats["pipeline_level_difference_db"] = float(
                20 * np.log10(np.std(raw) / np.std(up))
            )
    if valid:
        lags = [(e["us"] - e["audio_us"]) / 1000 for e in valid]
        stats["dma_completion_to_panel_push_ms"] = dict(
            p50=float(np.percentile(lags, 50)),
            p95=float(np.percentile(lags, 95)),
            maximum=float(max(lags)),
        )
        voiced = [(e["us"] - e["audio_us"]) / 1000 for e in valid if e["target"] > 0.02]
        if voiced:
            stats["voiced_dma_to_panel_push_ms"] = dict(
                p50=float(np.percentile(voiced, 50)),
                p95=float(np.percentile(voiced, 95)),
                maximum=float(max(voiced)),
            )
        src = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        gain = int(volume / 100 * 32768 + 0.5)
        src = ((src * gain) >> 15) / 32768
        errors = []
        for e in valid:
            end = (
                e["first_sample"] + 160
                if "first_sample" in e
                else e["pos_ms"] * rate // 1000
            )
            if 160 <= end <= len(src):
                rms = float(np.sqrt(np.mean(src[end - 160 : end] ** 2)))
                errors.append(abs(rms - e["rms"]))
        if errors:
            stats["reported_rms_absolute_error_p95"] = float(np.percentile(errors, 95))
        unique = {e["audio_us"]: e for e in valid if e["pos_ms"] > 0}
        points = list(unique.values())
        if len(points) > 8:
            wall = np.array([e["audio_us"] / 1000 for e in points])
            pos = np.array([e["pos_ms"] for e in points])
            slope, intercept = np.polyfit(pos, wall, 1)
            differences = np.diff(wall) - np.diff(pos)
            stats["output_clock_ms_per_content_ms"] = float(
                np.median(np.diff(wall) / np.diff(pos))
            )
            stats["content_wall_stalls_ms"] = [float(x) for x in differences if x > 2]
            stats["output_clock_least_squares_including_stalls"] = float(slope)
            stats["output_clock_residual_p95_ms"] = float(
                np.percentile(np.abs(wall - (pos * slope + intercept)), 95)
            )
        # Acoustic loopback envelope vs pushed mouth. Raw PDM timestamps are
        # read-completion timestamps, NOT an optical sensor or ADC DMA clock.
        if capture:
            t = np.array([f["ms"] - 10 for f in capture])
            env = np.array([np.std(f["raw"].astype(float)) for f in capture])
            mt = np.array([e["us"] / 1000 for e in mouth])
            opening = np.array([e["shown"] for e in mouth])
            if len(mt) > 10 and env.max() > 0:
                grid = np.arange(max(t[0], mt[0]), min(t[-1], mt[-1]), 5.0)
                scores = []
                for lag in range(-200, 201, 5):
                    a = np.interp(grid - lag, mt, opening)
                    b = np.interp(grid, t, env)
                    corr = (
                        np.corrcoef(a, b)[0, 1]
                        if np.std(a) > 0 and np.std(b) > 0
                        else 0
                    )
                    scores.append((float(corr), lag))
                corr, lag = max(scores)
                stats["microphone_envelope_vs_panel_push"] = dict(
                    correlation=corr,
                    lag_ms=lag,
                    convention="positive: microphone envelope follows panel mouth",
                    limitation="PDM read timestamp + panel push proxy; not optical lip-sync measurement",
                )
    stats["aec_stats"] = [e for e in bench.events if e.get("usb") == "aec_stats"]
    return stats


def run_trial(
    bench,
    out,
    kind,
    delay,
    volume,
    wav_path=None,
    cancel_at=None,
    rate=SR,
    aec=True,
    quiet=False,
):
    out.mkdir(parents=True, exist_ok=True)
    pcm = (
        double_talk_stimulus(out)
        if kind == "double-talk" and wav_path is None
        else stimulus(kind, wav_path)
    )
    starting_status = bench.status()
    bench.events.clear()
    bench.frames.clear()
    bench.logs.clear()
    bench.bad_checksums = 0
    bench.command(f"aec_delay {delay}")
    bench.wait(lambda e: e.get("usb") == "aec_delay" and e.get("accepted"))
    bench.command("aec_enable " + ("1" if aec else "0"))
    bench.wait(lambda e: e.get("usb") == "aec_enable" and e.get("accepted"))
    bench.pump(0.2)
    if rate != SR:
        pcm = (
            np.repeat(np.frombuffer(pcm, dtype="<i2"), rate // SR)
            .astype("<i2")
            .tobytes()
        )
    duration = len(pcm) / 2 / rate
    bench.command(f"mic_trace {min(60000, int((duration + 3) * 1000))}")
    bench.pump(0.7)
    try:
        result = bench.play(pcm, volume=volume, cancel_at=cancel_at, sample_rate=rate)
    except BaseException:
        # Never leave queued audio playing when capture fails or Ctrl-C exits.
        # The request is derived from the playback epoch, not any old ACK.
        try:
            bench.pb(
                dict(type="pb_cancel", req=f"usb-{bench.epoch:x}", epoch=bench.epoch)
            )
        except Exception:
            pass
        try:
            bench.command("mic_trace_stop")
        except Exception:
            pass
        (out / "failed-events.json").write_text(
            json.dumps(bench.events, ensure_ascii=False, indent=2)
        )
        (out / "failed-logs.txt").write_text("\n".join(bench.logs))
        for field in ("raw", "processed"):
            if bench.frames:
                write_wav(
                    out / f"failed-{field}.wav",
                    b"".join(f[field].tobytes() for f in bench.frames),
                    bench.frames[0]["rate"],
                )
        raise
    bench.pump(0.5)
    # End trace deterministically before the next trial starts its sequence.
    bench.command("mic_trace_stop")
    bench.pump(0.2)
    status = bench.status()
    metrics = analyze(bench, result, pcm, volume, rate)
    metrics["final_status"] = status
    metrics["tx_drops_start"] = starting_status["tx_drops"]
    metrics["tx_drops_delta"] = (
        status["tx_drops"] - starting_status["tx_drops"]
    ) & 0xFFFFFFFF
    metrics["trace_integrity_ok"] = bool(bench.frames) and not any(
        (
            metrics["trace_sequence_gaps"],
            metrics["trace_bad_checksums"],
            metrics["tx_drops_delta"],
        )
    )
    write_wav(out / "source.wav", pcm, rate)
    for field in ("raw", "processed"):
        if bench.frames:
            write_wav(
                out / f"{field}.wav",
                b"".join(f[field].tobytes() for f in bench.frames),
                bench.frames[0]["rate"],
            )
    (out / "events.json").write_text(
        json.dumps(bench.events, ensure_ascii=False, indent=2)
    )
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    if not quiet:
        print(json.dumps(metrics, ensure_ascii=False), flush=True)
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", help="USB serial device; auto-detect one ESP32 when omitted"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    mode = sub.add_parser("mode")
    mode.add_argument("mode", choices=["usb", "wifi"])
    run = sub.add_parser("run")
    run.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / ".pio/usb-bench/manual"
        / datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    run.add_argument(
        "--kind", choices=["probe", "noise", "double-talk"], default="probe"
    )
    run.add_argument("--wav", type=Path)
    run.add_argument("--delay", type=int, default=32)
    run.add_argument("--volume", type=int, default=35)
    run.add_argument("--cancel-at", type=float)
    run.add_argument("--aec", choices=["on", "off"], default="on")
    run.add_argument("--rate", type=int, choices=[16000, 48000], default=16000)
    args = parser.parse_args()
    if args.action == "run" and args.kind == "double-talk":
        if args.wav:
            parser.error("--kind double-talk 不能与 --wav 同时使用")
        print(
            "请离机器人麦克风约 20 厘米，每次听到‘开始’后反复说：\n"
            "一二三四五六七八九十，今天天气很好。\n"
            "共两轮，每轮 8 秒；第二轮伴随测试音。听到‘测试结束’后停止。",
            flush=True,
        )
    port = args.port
    if not port:
        candidates = [
            p.device
            for p in serial.tools.list_ports.comports()
            if p.vid == 0x303A or "usbmodem" in p.device or "ttyACM" in p.device
        ]
        if len(candidates) != 1:
            parser.error(
                "Connect one ESP32 USB device, or specify --port. Found: "
                + str(candidates)
            )
        port = candidates[0]
    bench = UsbBench(port)
    try:
        if args.action == "mode":
            bench.command("usb_mode " + ("1" if args.mode == "usb" else "0"))
            if args.mode == "usb":
                print(
                    json.dumps(
                        bench.wait(lambda e: e.get("usb") == "ready", timeout=50),
                        ensure_ascii=False,
                    )
                )
            else:
                print("Device restarting in normal Wi-Fi mode")
        elif args.action == "status":
            print(json.dumps(bench.status(), ensure_ascii=False))
        else:
            status = bench.status()
            if status.get("usb_protocol", 0) < 2:
                raise RuntimeError(
                    "Flash the current USB binary protocol firmware first"
                )
            manifest = (
                Path(__file__).resolve().parents[1]
                / "firmware/generated/face_assets_manifest.json"
            )
            if (
                manifest.exists()
                and status["version"] != json.loads(manifest.read_text())["version"]
            ):
                raise RuntimeError(
                    "Device animation release differs from the local catalog; flash matching firmware"
                )
            if not status["afe_ready"]:
                raise RuntimeError("AFE not initialized; AEC measurement is invalid")
            result = run_trial(
                bench,
                args.output,
                args.kind,
                args.delay,
                args.volume,
                args.wav,
                args.cancel_at,
                args.rate,
                args.aec == "on",
                quiet=True,
            )
            print("测试完成，报告：" + str(args.output.resolve() / "metrics.json"))
            print(
                f"采集 {result['trace_frames']} 帧，丢帧 {result['trace_sequence_gaps']}，"
                f"校验错误 {result['trace_bad_checksums']}，本次 USB 丢弃 {result['tx_drops_delta']}。"
            )
            latency = result.get("voiced_dma_to_panel_push_ms")
            if latency:
                print(
                    f"有声帧 DMA 完成到推屏：P95 {latency['p95']:.1f} ms（非光学测量）。"
                )
            print(
                "AEC：" + ("开启" if result["final_status"]["aec_enabled"] else "关闭")
            )
    finally:
        bench.close()


if __name__ == "__main__":
    main()
