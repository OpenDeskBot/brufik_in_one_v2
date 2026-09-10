from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from deskbot_server.service.application.asr_chat_uplink import pack_ws_downlink_frame
from deskbot_server.service.device_ws_service import DeviceWsService


class _Ws:
    remote_address = ("127.0.0.1", 1234)

    def __init__(self, messages):
        self._messages = iter(messages)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._messages)
        except StopIteration:
            raise StopAsyncIteration from None

    async def send(self, message):
        self.sent.append(message)


def test_camera_uplink_accepts_packed_frame_without_claiming_asr_slot(monkeypatch):
    async def _run() -> None:
        DeviceWsService.reset_instance()
        svc = DeviceWsService()
        asr_ws = AsyncMock()
        await svc.register("dev1", asr_ws)

        process = AsyncMock()
        monkeypatch.setattr("deskbot_server.service.device_ws_service.CameraFaceService.process", process)
        packed = pack_ws_downlink_frame(json.dumps({"type": "camera_frame", "codec": "jpeg"}), b"jpeg")
        camera_ws = _Ws([packed])

        await svc.handle_camera_uplink(camera_ws, "dev1")
        await asyncio.sleep(0)

        assert svc._devices["dev1"].ws is asr_ws
        assert svc.latest_camera_frame("dev1") == b"jpeg"
        assert json.loads(camera_ws.sent[0])["channel"] == "camera"
        process.assert_awaited_once()

    asyncio.run(_run())
