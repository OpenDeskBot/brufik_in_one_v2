import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from deskbot_server.dao import user_mapper
from deskbot_server.service.application.realtime_voiceprint import RealtimeVoiceprint
from deskbot_server.service.external.manager import ExternalServiceManager, ServiceError
from deskbot_server.service.perception_services import PERCEPTION_SERVICES
from tests.test_controller_auth import _login
from tests.test_controller_auth import api_client as api_client


def test_production_manager_excludes_voice_models(tmp_path):
    from deskbot_server.service.external.manager import EXTERNALS_DIR
    manager = ExternalServiceManager(EXTERNALS_DIR, tmp_path, allowed_names=PERCEPTION_SERVICES)
    manager.discover()
    assert set(manager.names()) == PERCEPTION_SERVICES
    for name in ("funasr", "llm-realtime", "moss-tts-nano"):
        with pytest.raises(ServiceError):
            asyncio.run(manager.start(name))


def test_service_controls_are_developer_only_and_allowlisted(api_client):
    snapshot = SimpleNamespace(to_dict=lambda: {"name": "insightface-engine", "state": "running"})
    manager = SimpleNamespace(status_all=lambda: [snapshot], start=AsyncMock(return_value=snapshot),
                              set_auto_start=lambda name, enabled: snapshot)
    api_client.app.state.perception_services = manager
    path = "/api/perception-services"
    assert api_client.get(path).status_code == 401
    _login(api_client)
    assert api_client.get(path).status_code == 403
    user_mapper.create("developer", "developer@example.com", "unused", True)
    _login(api_client, "developer")
    assert api_client.get(path).json()["services"][0]["name"] == "insightface-engine"
    assert api_client.post(path+"/funasr/start").status_code == 404
    assert api_client.post(path+"/insightface-engine/uninstall").status_code == 404
    assert api_client.post(path+"/insightface-engine/auto-start", json={"enabled": "yes"}).status_code == 409
    assert api_client.post(path+"/insightface-engine/start").status_code == 200
    manager.start.assert_awaited_once_with("insightface-engine")
    api_client.app.state.perception_services = None


def test_model_not_ready_is_unhealthy_even_with_http_200(monkeypatch):
    class Response(io.BytesIO):
        status = 200
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Response(b'{"model_ready":false}'))
    with pytest.raises(RuntimeError, match="尚未就绪"):
        ExternalServiceManager._http_get("http://127.0.0.1/health", 1)


def test_stale_pid_never_terminates_an_unverified_process(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from deskbot_server.service.external.manager import EXTERNALS_DIR
    manager = ExternalServiceManager(EXTERNALS_DIR, tmp_path, allowed_names=PERCEPTION_SERVICES)
    manager.discover()
    handle = manager._handles["insightface-engine"]
    path = handle.pid_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("999999999")
    monkeypatch.setattr("deskbot_server.service.external.manager.pid_alive", lambda pid: True)
    kill = Mock()
    monkeypatch.setattr("deskbot_server.service.external.manager.os.kill", kill)
    manager._reap_orphan(handle)
    kill.assert_not_called()
    assert not path.exists()


def test_voiceprint_segments_are_bounded_and_ignore_playback(monkeypatch):
    async def run():
        clock = [0.0]
        monkeypatch.setattr("deskbot_server.service.application.realtime_voiceprint.time.monotonic", lambda: clock[0])
        sampler = RealtimeVoiceprint("robot")
        entered = asyncio.Event()
        release = asyncio.Event()
        captured = []

        async def identify(**kwargs):
            captured.append(kwargs["pcm_bytes"])
            entered.set()
            await release.wait()

        sampler.service = SimpleNamespace(enabled=lambda: True, identify=identify)
        voice, silence = b"\xe8\x03"*320, bytes(640)
        for _ in range(100):
            sampler.feed(voice, playback=True)
        assert not sampler.audio and sampler.task is None
        clock[0] = 1.0
        for _ in range(35):
            sampler.feed(voice)
        for _ in range(30):
            sampler.feed(silence)
        await entered.wait()
        assert len(captured) == 1 and len(captured[0]) == 65*640
        for _ in range(1200):
            sampler.feed(voice)
        assert len(sampler.audio) < 8*32000 and len(captured) == 1
        await sampler.close()
        assert sampler.task.cancelled() and not sampler.audio
    asyncio.run(run())


def test_face_inference_never_overlaps_or_builds_frame_backlog(monkeypatch):
    from deskbot_server.service.camera_face_service import CameraFaceRuntime, CameraFaceService

    async def run():
        CameraFaceService.reset_instance()
        service = CameraFaceService()
        service.configure(CameraFaceRuntime())
        blocked = asyncio.Event()

        async def detect_frame(**kwargs):
            await blocked.wait()
        monkeypatch.setattr(service, "_detect_then_post", detect_frame)
        for _ in range(50):
            await service.process("robot", b"jpeg")
        assert len(service._inflight) == 1
        task = service._inflight["robot"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CameraFaceService.reset_instance()
    asyncio.run(run())
