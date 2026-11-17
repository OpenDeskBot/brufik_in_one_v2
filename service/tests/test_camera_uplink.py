from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from deskbot_server.service.application.asr_chat_uplink import pack_ws_downlink_frame
from deskbot_server.service.camera_face_service import CameraFaceService
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


@pytest.mark.parametrize("provider", ["flecket", "moss-live"])
@pytest.mark.parametrize("bridge_ready", [False, True])
def test_realtime_camera_reaches_preview_and_parallel_face_inference(monkeypatch, provider, bridge_ready):
    async def _run():
        DeviceWsService.reset_instance()
        CameraFaceService.reset_instance()
        svc = DeviceWsService()
        asr_ws = AsyncMock()
        await svc.register("preview-robot", asr_ws)
        entry = svc._devices["preview-robot"]
        entry.conversation_provider = provider
        bridge = SimpleNamespace(offer_camera=Mock()) if bridge_ready else None
        entry.conversation_session = bridge

        camera = CameraFaceService()
        viewer, foreign_viewer = AsyncMock(), AsyncMock()
        await camera.subscribe_video_stream("viewer", viewer, device_id="preview-robot")
        await camera.subscribe_video_stream("foreign", foreign_viewer, device_id="other-robot")
        inference = AsyncMock()
        monkeypatch.setattr(camera, "process", inference)
        jpeg = b"\xff\xd8preview-frame\xff\xd9"
        packed = pack_ws_downlink_frame(json.dumps({"type": "camera_frame", "codec": "jpeg"}), jpeg)
        await svc.handle_camera_uplink(_Ws([packed]), "preview-robot")
        await asyncio.sleep(0)

        viewer.assert_awaited_once()
        device, received, meta = viewer.await_args.args
        assert (device, received) == ("preview-robot", jpeg)
        assert meta["source"] == "camera_uplink" and meta["size"] == len(jpeg)
        foreign_viewer.assert_not_awaited()
        inference.assert_awaited_once_with("preview-robot", jpeg, frame_source="camera_uplink", log_channel="/camera_uplink")
        assert svc.latest_camera_frame("preview-robot") == jpeg
        assert entry.ws is asr_ws
        if bridge is not None:
            bridge.offer_camera.assert_called_once_with(jpeg)
        await camera.unsubscribe_video_stream("viewer")
        await camera.unsubscribe_video_stream("foreign")

    asyncio.run(_run())
