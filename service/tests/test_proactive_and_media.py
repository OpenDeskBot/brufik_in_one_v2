import asyncio
import shutil

import pytest

from deskbot_server.model.pb_seq import PbType
from deskbot_server.service import media_library
from deskbot_server.service.application.asr_chat_uplink import parse_packed_frame
from tests.test_agent_capabilities import audio_bytes, image_bytes
from tests.test_flecket import audio
from tests.test_moss_live import cleanup, until
from tests.test_robot_performance import acknowledge, live_bridge
from tests.test_robot_performance import catalog as catalog


def test_speech_on_rom_with_device_tools_stops_media_without_reconnecting(catalog, monkeypatch):
    async def run():
        value, hub, entry, bridge, _, worker = await live_bridge(monkeypatch)
        entry.capabilities.add("rom_tools_v1")
        commands = []

        async def send(raw):
            frame = parse_packed_frame(raw)
            assert frame is not None and frame.doc["type"] == "rom_command"
            command = frame.doc
            commands.append(command)
            hub.accept_rom_result("robot", {"id": command["id"], "ok": True, "status": "completed"})

        monkeypatch.setattr(entry.ws, "send", send, raising=False)
        try:
            for count in (1, 2):
                value.ws.emit("input_audio_buffer.speech_started")
                await until(lambda expected=count: len(commands) == expected or bridge.failed)
                assert not bridge.failed and not bridge.closed and not entry.ws.closed
                assert len(commands) == count and commands[-1]["op"] == "stop_image"
            assert not entry.rom_waiters
        finally:
            await cleanup(bridge, worker)
            await hub._retire_workers(entry)
    asyncio.run(run())


def test_external_reminder_waits_for_actual_device_completion(catalog, monkeypatch):
    async def run():
        value, _, entry, bridge, sent, worker = await live_bridge(monkeypatch)
        task = asyncio.create_task(bridge.announce("该喝水了"))
        try:
            await until(lambda: any(e["type"] == "response.create" for e in value.ws.sent))
            request = next(e for e in value.ws.sent if e["type"] == "response.create")
            marker = request["response"]["metadata"]
            assert "deskbot_announcement" in marker
            value.ws.emit("response.created", response={"id": "reminder", "metadata": marker})
            audio(value.ws, rid="reminder", ms=80)
            value.ws.emit("response.output_audio.done", response_id="reminder", item_id="item-reminder")
            value.ws.emit("response.done", response={"id": "reminder", "status": "completed", "metadata": marker})
            await until(lambda: any(b.type == PbType.END for b, _ in sent))
            assert not task.done()
            acknowledge(entry, next(b for b, _ in sent if b.type == PbType.END))
            assert (await task)["ok"] is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await cleanup(bridge, worker)
    asyncio.run(run())


def test_user_activity_defers_reminder_and_late_response_is_cancelled(catalog, monkeypatch):
    async def run():
        value, _, _, bridge, _, worker = await live_bridge(monkeypatch)
        task = None
        try:
            value._speech_active = True
            assert not (await bridge.announce("提醒"))["ok"]
            assert not value.ws.sent
            value._speech_active = False
            task = asyncio.create_task(bridge.announce("提醒"))
            await until(lambda: any(e["type"] == "response.create" for e in value.ws.sent))
            marker = next(e for e in value.ws.sent if e["type"] == "response.create")["response"]["metadata"]
            value.ws.emit("input_audio_buffer.speech_started")
            value.ws.emit("response.created", response={"id": "too-late", "metadata": marker})
            assert not (await task)["ok"]
            assert any(e["type"] == "response.cancel" and e["response_id"] == "too-late" for e in value.ws.sent)
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await cleanup(bridge, worker)
    asyncio.run(run())


def test_reminder_timeout_cancels_its_upstream_response(catalog, monkeypatch):
    async def run():
        value, _, _, bridge, _, worker = await live_bridge(monkeypatch)
        task = asyncio.create_task(bridge.announce("提醒", timeout=.05))
        try:
            await until(lambda: any(e["type"] == "response.create" for e in value.ws.sent))
            marker = next(e for e in value.ws.sent if e["type"] == "response.create")["response"]["metadata"]
            value.ws.emit("response.created", response={"id": "slow", "metadata": marker})
            await until(lambda: "slow" in value._active_responses)
            assert not (await task)["ok"]
            assert any(e["type"] == "response.cancel" and e["response_id"] == "slow" for e in value.ws.sent)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await cleanup(bridge, worker)
    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg unavailable")
def test_music_start_requires_playback_ack_and_stop_cancels_stream(catalog, monkeypatch):
    item = media_library.upload("robot", "music", "小曲", audio_bytes())

    async def run():
        value, _, _, bridge, sent, worker = await live_bridge(monkeypatch)
        task = asyncio.create_task(bridge.media_tool("play_music", {"asset_id": item["id"], "speak": False}))
        try:
            await until(lambda: any(b.audio for b, _ in sent))
            assert not task.done()  # Credits alone do not mean music started.
            first = next(b for b, _ in sent if b.audio)
            bridge.offer_ack({"req": first.req, "epoch": first.epoch, "ack_type": "pb_started"})
            assert (await task)["status"] == "started"
            assert value.external_audio_active
            await bridge.media.stop_music()
            await until(lambda: any(b.type == PbType.CANCEL for b, _ in sent))
            assert not value.external_audio_active
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await cleanup(bridge, worker)
    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg unavailable")
def test_music_is_deferred_until_spoken_confirmation_finishes(catalog, monkeypatch):
    from unittest.mock import AsyncMock
    item = media_library.upload("robot", "music", "小曲", audio_bytes())

    async def run():
        value, _, entry, bridge, sent, worker = await live_bridge(monkeypatch)
        started = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(bridge.media, "start_music", started)
        try:
            result = await bridge.media_tool("play_music", {"asset_id": item["id"]})
            assert result["status"] == "scheduled"
            value.ws.emit("response.created", response={"id": "confirm", "metadata": {"robot_media_id": result["media_id"]}})
            audio(value.ws, rid="confirm", ms=80)
            value.ws.emit("response.output_audio.done", response_id="confirm", item_id="item-confirm")
            await until(lambda: any(b.type == PbType.END for b, _ in sent))
            started.assert_not_awaited()
            acknowledge(entry, next(b for b, _ in sent if b.type == PbType.END))
            await until(lambda: started.await_count == 1)
            started.assert_awaited_once_with(item["id"], generation=0)
        finally:
            await cleanup(bridge, worker)
    asyncio.run(run())


def test_image_is_displayed_after_voice_and_quiet_image_displays_immediately(catalog, monkeypatch):
    from unittest.mock import AsyncMock
    item = media_library.upload("robot", "image", "照片", image_bytes())

    async def run():
        value, _, entry, bridge, sent, worker = await live_bridge(monkeypatch)
        shown = AsyncMock(return_value={"ok": True, "status": "displayed"})
        monkeypatch.setattr(bridge.media, "show_image", shown)
        try:
            result = await bridge.media_tool("show_image", {"asset_id": item["id"], "duration_ms": 7000})
            assert result["status"] == "scheduled"
            value.ws.emit("response.created", response={"id": "picture-confirm", "metadata": {"robot_media_id": result["media_id"]}})
            audio(value.ws, rid="picture-confirm", ms=80)
            value.ws.emit("response.output_audio.done", response_id="picture-confirm", item_id="item-picture-confirm")
            await until(lambda: any(b.type == PbType.END for b, _ in sent))
            shown.assert_not_awaited()
            acknowledge(entry, next(b for b, _ in sent if b.type == PbType.END))
            await until(lambda: shown.await_count == 1)
            shown.assert_awaited_once_with(item["id"], 7000, generation=0)
            quiet = await bridge.media_tool("show_image", {"asset_id": item["id"], "speak": False})
            assert quiet["status"] == "displayed" and quiet["speak"] is False
            assert shown.await_count == 2
        finally:
            await cleanup(bridge, worker)
    asyncio.run(run())
