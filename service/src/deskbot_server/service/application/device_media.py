"""Bounded music streaming and ROM image presentation for one connection."""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
import uuid
import wave

import opuslib_next

from deskbot_server.dao import media_asset_store as store


class DeviceMedia:
    def __init__(self, bridge):
        self.bridge = bridge
        self.task = None
        self.stream = None
        self.started = None
        self.status = {"state": "idle"}

    def offer_ack(self, ack):
        if (self.stream and ack.get("req") == self.stream.seq.req and ack.get("epoch") == self.stream.seq.epoch
                and (ack.get("ack_type") == "pb_started" or (type(ack.get("played_ms")) is int and ack["played_ms"] > 0))
                and self.started and not self.started.done()):
            self.status["state"] = "playing"
            self.started.set_result({"ok": True, "status": "started"})

    async def start_music(self, asset_id, *, generation=None):
        item = await asyncio.to_thread(store.get, self.bridge.device_id, asset_id)
        if item["kind"] != "music":
            raise ValueError("请选择音乐资源")
        await self.stop_music()
        if generation is not None and not self.bridge._current(generation):
            return {"ok": False, "status": "cancelled"}
        self.started = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._play_music(item, self.started))
        try:
            async with asyncio.timeout(8):
                return await asyncio.shield(self.started)
        except TimeoutError:
            await self.stop_music()
            return {"ok": False, "error": "未收到设备开始播放的回执"}

    async def _play_music(self, item, started):
        bridge = self.bridge
        req = uuid.uuid4().hex
        bridge.client.external_audio_active = True
        self.status = {"state": "starting", "asset_id": item["id"], "title": item["title"]}
        stream = None
        try:
            stream = await bridge.hub.open_speech_stream(bridge.device_id, req, sr=16000, fmt="opus",
                                                         expected_generation=bridge.generation)
            self.stream = stream
            if stream is None:
                raise ValueError("设备不支持流式音乐播放")
            encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
            encoder.bitrate = 32000
            with wave.open(str(store.asset_path(bridge.device_id, item["id"], "wav")), "rb") as wav:
                while block := wav.readframes(1280):
                    packets = []
                    for offset in range(0, len(block), 640):
                        packet = encoder.encode(block[offset:offset+640].ljust(640, b"\0"), 320)
                        packets.append(struct.pack("!H", len(packet)) + packet)
                    blob = b"".join(packets)
                    await stream.append([({"type": "pb_chunk", "chunk_ms": len(packets)*20,
                                          "audio": {"next_bin_len": len(blob), "frames": len(packets)}}, [blob])])
            self.status["state"] = "playing"
            outcome = await stream.finish()
            self.status["state"] = "completed" if outcome.completed else outcome.status.value
        except asyncio.CancelledError:
            self.status["state"] = "stopped"
            raise
        except Exception:
            self.status = {"state": "failed", "error": "音乐播放失败"}
        finally:
            if not started.done():
                started.set_result({"ok": False, "error": "音乐未开始播放"})
            if stream and (stream.seq.outcome is None or not stream.seq.outcome.completed):
                await bridge.hub.interrupt_current_playback(bridge.device_id, reason="music_stopped", expected_req=req)
            bridge.client.external_audio_active = False
            bridge.client._mute_until = time.monotonic() + .45
            self.stream = None

    async def show_image(self, asset_id, duration_ms=5000, *, generation=None):
        if type(duration_ms) is not int or not 1000 <= duration_ms <= 30000:
            raise ValueError("图片展示时间需要 1000～30000 毫秒")
        item = await asyncio.to_thread(store.get, self.bridge.device_id, asset_id)
        if item["kind"] != "image":
            raise ValueError("请选择图片资源")
        raw = await asyncio.to_thread(store.asset_path(self.bridge.device_id, asset_id, "rgb565").read_bytes)
        if generation is not None and not self.bridge._current(generation):
            return {"ok": False, "status": "cancelled"}
        await self.bridge.hub.rom_command(self.bridge.device_id, "stop_image", {}, expected_generation=self.bridge.generation)
        if generation is not None and not self.bridge._current(generation):
            return {"ok": False, "status": "cancelled"}
        result = await self.bridge.hub.rom_command(self.bridge.device_id, "image", {
            "width": item["width"], "height": item["height"], "duration_ms": duration_ms,
        }, media=raw, expected_generation=self.bridge.generation)
        if not result.get("ok"):
            await self.bridge.hub.rom_command(self.bridge.device_id, "stop_image", {}, expected_generation=self.bridge.generation)
        return result

    async def stop_music(self):
        task = self.task
        self.task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop(self):
        await self.stop_music()
        entry = self.bridge.hub._devices.get(self.bridge.device_id)
        if entry and "rom_tools_v1" in entry.capabilities:
            await self.bridge.hub.rom_command(self.bridge.device_id, "stop_image", {}, expected_generation=self.bridge.generation)
        return {"ok": True, "status": "stopped"}
