from __future__ import annotations

import asyncio

from deskbot_server.service.camera_face_service import CameraFaceService, capture_camera_for_device_async
from deskbot_server.infrastructure.llm.utils import parse_llm_reply


def _fake_jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"


def test_capture_camera_for_device_async_via_video_subscribe():
    CameraFaceService.reset_instance()
    svc = CameraFaceService()
    # capture 不依赖 dp_broker；直接 _emit 模拟上行帧
    dev = "dev_async_cam"

    async def _run():
        async def _publisher():
            await asyncio.sleep(0.05)
            await svc.try_emit_video_frame(
                dev,
                _fake_jpeg(),
                meta={"frame_w": 320, "frame_h": 240, "source": "test"},
            )

        pub = asyncio.create_task(_publisher())
        cap = await capture_camera_for_device_async(dev, wait_timeout_s=1.0)
        await pub
        return cap

    cap = asyncio.run(_run())
    assert cap["ok"] is True
    assert cap["jpeg_bytes"] > 0
    assert len(svc._video_subs) == 0


def test_parse_llm_reply_ignores_cam_fps():
    """cam_fps 全链路已移除（ROM+服务端）：LLM 信封字段不再解析下发。"""
    parsed = parse_llm_reply('{"tts":"好","cam_fps":5,"tools":[]}')
    assert parsed["json_ok"] is True
    assert "cam_fps" not in parsed
