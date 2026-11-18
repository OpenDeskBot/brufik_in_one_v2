"""Host USB trace framing survives arbitrary reads and corrupted records."""

import importlib.util
import json
import struct
import unittest
from pathlib import Path

AVAILABLE = (
    importlib.util.find_spec("numpy") is not None
    and importlib.util.find_spec("serial") is not None
)


@unittest.skipUnless(
    AVAILABLE, "run with service/.venv/bin/python for numpy and pyserial"
)
class UsbParserTest(unittest.TestCase):
    def test_speech_band_rms_excludes_dc_and_slow_bias(self):
        spec = importlib.util.spec_from_file_location(
            "usb_audio_bench",
            Path(__file__).resolve().parents[1] / "tools/usb_audio_bench.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        t = module.np.arange(8000) / 8000
        samples = (
            5000
            + 3000 * module.np.sin(2 * module.np.pi * 30 * t)
            + 1000 * module.np.sin(2 * module.np.pi * 1000 * t)
        )
        self.assertAlmostEqual(module.speech_band_rms(samples, 8000), 1000 / 2**0.5)

    def test_fragmented_trace_checksum_recovery_and_json(self):
        spec = importlib.util.spec_from_file_location(
            "usb_audio_bench",
            Path(__file__).resolve().parents[1] / "tools/usb_audio_bench.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pcm = struct.pack("<320h", *range(320))
        payload = b"DBAUD001" + struct.pack("<III", 4, 1000, 3) + pcm + pcm
        packet = payload + struct.pack("<I", sum(payload) & 0xFFFFFFFF)
        corrupt = bytearray(packet)
        corrupt[-1] ^= 1
        stream = (
            b'{"usb":"ready"}\n'
            + packet
            + bytes(corrupt)
            + packet
            + b'{"usb":"done"}\n'
        )
        for chunk in (1, 7, 19, 511, 8192):

            class Link:
                in_waiting = 0

                def __init__(self):
                    self.at = 0

                def read(self, _):
                    data = stream[self.at : self.at + chunk]
                    self.at += len(data)
                    return data

            bench = module.UsbBench.__new__(module.UsbBench)
            bench.link = Link()
            bench.buffer = bytearray()
            bench.events, bench.frames, bench.logs = [], [], []
            bench.bad_checksums = 0
            bench.pump(0.04)
            self.assertEqual([e["usb"] for e in bench.events], ["ready", "done"])
            self.assertEqual(len(bench.frames), 2)
            self.assertEqual(bench.bad_checksums, 1)
            self.assertEqual(bench.frames[0]["ms"], 1000)
            self.assertEqual(bench.frames[0]["raw"].tolist(), list(range(320)))

    def test_compact_trace_layout(self):
        spec = importlib.util.spec_from_file_location(
            "usb_audio_bench",
            Path(__file__).resolve().parents[1] / "tools/usb_audio_bench.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pcm = struct.pack("<160h", *range(160))
        payload = b"DBAUD002" + struct.pack("<III", 7, 2000, 1) + pcm + pcm
        stream = payload + struct.pack("<I", sum(payload) & 0xFFFFFFFF)

        class Link:
            in_waiting = 0

            def read(self, _):
                nonlocal stream
                chunk, stream = stream[:13], stream[13:]
                return chunk

        bench = module.UsbBench.__new__(module.UsbBench)
        bench.link = Link()
        bench.buffer = bytearray()
        bench.events, bench.frames, bench.logs = [], [], []
        bench.bad_checksums = 0
        bench.pump(0.02)
        self.assertEqual(len(bench.frames), 1)
        self.assertEqual(bench.frames[0]["rate"], 8000)
        self.assertEqual(bench.frames[0]["raw"].tolist(), list(range(160)))

    def test_binary_sender_keeps_pb_and_audio_bytes(self):
        spec = importlib.util.spec_from_file_location(
            "usb_audio_bench",
            Path(__file__).resolve().parents[1] / "tools/usb_audio_bench.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sent = []

        class Link:
            def write(self, data):
                sent.append(data)

        bench = module.UsbBench.__new__(module.UsbBench)
        bench.link = Link()
        bench.events = []
        bench.wait = lambda *args, **kwargs: {"accepted": True}
        pcm = bytes(range(256)) * 12
        message = {
            "type": "pb_single",
            "req": "usb-audio",
            "idx": 0,
            "audio": {"next_bin_len": len(pcm)},
        }
        bench.pb(message, pcm)
        wire = b"".join(sent)
        self.assertEqual(wire[:8], b"DBPB0001")
        total, json_length = struct.unpack_from("!II", wire, 8)
        self.assertEqual(total, len(wire) - 12)
        self.assertEqual(json.loads(wire[16 : 16 + json_length]), message)
        self.assertEqual(wire[16 + json_length :], pcm)

    def test_lifetime_tx_drops_do_not_mark_a_new_capture_as_corrupt(self):
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "usb_audio_bench",
            Path(__file__).resolve().parents[1] / "tools/usb_audio_bench.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for end_drops, expected in ((40, True), (42, False)):

            class Bench:
                epoch = 3
                bad_checksums = 0

                def __init__(self):
                    self.events, self.frames, self.logs = [], [], []
                    self.status_calls = 0

                def status(self):
                    self.status_calls += 1
                    return {"tx_drops": 40 if self.status_calls == 1 else end_drops}

                def command(self, _):
                    pass

                def wait(self, *_args, **_kwargs):
                    return {"accepted": True}

                def pump(self, _):
                    pass

                def play(self, *_args, **_kwargs):
                    self.frames.append(
                        dict(
                            seq=0,
                            ms=20,
                            flags=1,
                            rate=8000,
                            raw=module.np.zeros(160, dtype="<i2"),
                            processed=module.np.zeros(160, dtype="<i2"),
                        )
                    )
                    return {"epoch": 3}

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                module.write_wav(path / "source-input.wav", bytes(640))
                result = module.run_trial(
                    Bench(),
                    path / "result",
                    "probe",
                    32,
                    35,
                    wav_path=path / "source-input.wav",
                    quiet=True,
                )
                self.assertEqual(result["trace_integrity_ok"], expected)
                self.assertEqual(result["tx_drops_delta"], end_drops - 40)

    def test_capture_failure_stops_audio_and_keeps_diagnostics(self):
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "usb_audio_bench",
            Path(__file__).resolve().parents[1] / "tools/usb_audio_bench.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Bench:
            epoch = 5

            def __init__(self):
                self.events, self.frames, self.logs = [], [], []
                self.commands, self.packets = [], []

            def status(self):
                return {"tx_drops": 0}

            def command(self, value):
                self.commands.append(value)

            def wait(self, *_args, **_kwargs):
                return {"accepted": True}

            def pump(self, _):
                pass

            def pb(self, value):
                self.packets.append(value)

            def play(self, *_args, **_kwargs):
                raise RuntimeError("device disconnected")

        bench = Bench()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            module.write_wav(path / "source-input.wav", bytes(640))
            with self.assertRaisesRegex(RuntimeError, "device disconnected"):
                module.run_trial(
                    bench,
                    path / "result",
                    "probe",
                    32,
                    35,
                    wav_path=path / "source-input.wav",
                    quiet=True,
                )
            self.assertEqual(
                bench.packets, [{"type": "pb_cancel", "req": "usb-5", "epoch": 5}]
            )
            self.assertEqual(bench.commands[-1], "mic_trace_stop")
            self.assertTrue((path / "result/failed-events.json").exists())
