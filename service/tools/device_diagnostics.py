"""Serial scalar diagnostics shared by the real-device verification tools."""
from __future__ import annotations

import asyncio
from collections import deque
import json
from pathlib import Path
import re
import threading
import time

import serial

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

    def close(self):
        self.stop_event.set(); self.thread.join(2); self.serial.close(); self.file.close()
