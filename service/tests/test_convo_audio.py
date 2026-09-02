"""实验台实时对话音频：内存仓库（ConvoAudioStore）与终态事件字段（publish_chat_turn）。"""

from __future__ import annotations

import io
import tempfile
import wave
from pathlib import Path

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(db_path)
        init_database()
        yield db_path


@pytest.fixture(autouse=True)
def fresh_store():
    from deskbot_server.service.application import convo_audio_store
    from deskbot_server.service.application.convo_audio_store import ConvoAudioStore

    ConvoAudioStore.reset_instance()
    yield convo_audio_store
    ConvoAudioStore.reset_instance()


def _wav_info(wav: bytes) -> tuple[int, int]:
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getframerate(), w.getnframes()


SILENCE_16K = b"\x00\x00" * 16000  # 1s @16k 静音


class TestConvoAudioStore:
    def test_put_get_returns_wav(self, fresh_store):
        store = fresh_store.ConvoAudioStore()
        assert store.put("dev1", "rid1", "asr", SILENCE_16K, 16000) is True
        wav = store.get("dev1", "rid1", "asr")
        assert wav is not None and wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
        sr, frames = _wav_info(wav)
        assert sr == 16000 and frames == 16000
        assert store.has("dev1", "rid1", "asr") is True

    def test_kind_isolated(self, fresh_store):
        store = fresh_store.ConvoAudioStore()
        store.put("dev1", "rid1", "asr", SILENCE_16K, 16000)
        store.put("dev1", "rid1", "tts", SILENCE_16K, 16000)
        assert store.get("dev1", "rid1", "asr") is not None
        assert store.get("dev1", "rid1", "tts") is not None
        assert store.get("dev1", "rid2", "asr") is None
        assert store.get("dev2", "rid1", "asr") is None

    def test_invalid_kind_or_missing_keys_rejected(self, fresh_store):
        store = fresh_store.ConvoAudioStore()
        assert store.put("dev1", "rid1", "mp3", SILENCE_16K, 16000) is False
        assert store.put("", "rid1", "asr", SILENCE_16K, 16000) is False
        assert store.put("dev1", "", "asr", SILENCE_16K, 16000) is False
        assert store.get("dev1", "rid1", "mp3") is None

    def test_put_raw_face_bytes_roundtrip(self, fresh_store):
        store = fresh_store.ConvoAudioStore()
        jpeg = b"\xff\xd8\xff\xe0" + b"fake-jpeg" * 100
        assert store.put_raw("dev1", "rid1", "face", jpeg) is True
        assert store.get("dev1", "rid1", "face") == jpeg  # 原样字节，非 wav
        # face 只能走 put_raw；音频走 put 且不互相污染
        assert store.put_raw("dev1", "rid1", "asr", b"x") is False
        assert store.put_raw("dev1", "rid1", "mp3", b"x") is False
        assert store.put_raw("", "rid1", "face", b"x") is False
        assert store.get("dev1", "rid1", "face") == jpeg

    def test_lru_eviction_and_ttl(self, fresh_store, monkeypatch):
        monkeypatch.setattr(fresh_store, "MAX_ITEMS", 3)
        store = fresh_store.ConvoAudioStore()
        for i in range(4):
            assert store.put("dev1", f"rid{i}", "tts", SILENCE_16K, 16000) is True
        assert store.has("dev1", "rid0", "tts") is False  # 最旧被淘汰
        assert store.has("dev1", "rid3", "tts") is True
        # 命中（has/get）刷新 LRU：再淘汰应轮到 rid1
        assert store.get("dev1", "rid2", "tts") is not None
        assert store.put("dev1", "rid4", "tts", SILENCE_16K, 16000) is True
        assert store.has("dev1", "rid1", "tts") is False
        assert store.has("dev1", "rid4", "tts") is True

        monkeypatch.setattr(fresh_store, "TTL_S", -1)
        assert store.get("dev1", "rid4", "tts") is None  # 过期

    def test_clear_by_device(self, fresh_store):
        store = fresh_store.ConvoAudioStore()
        store.put("dev1", "rid1", "asr", SILENCE_16K, 16000)
        store.put("dev2", "rid1", "asr", SILENCE_16K, 16000)
        store.clear("dev1")
        assert store.has("dev1", "rid1", "asr") is False
        assert store.has("dev2", "rid1", "asr") is True


def test_extract_face_sight_lines_from_assembled_message():
    """气泡展示的识别文本应从装配好的 user 消息中提取（与 prompt 同源）。"""
    from deskbot_server.service.application.chat_flow import _extract_face_sight_lines

    message = (
        "[机器人传感器信息:\n"
        "水平舵机角度: 未知, 垂直舵机角度: 未知\n"
        "摄像头跟随模式: 跟随人脸\n"
        "图像识别:\n"
        "   faceid=2, 人脸置信度=0.91, name=妈妈, 人物识别置信度=0.72, 脸中心位置=(160,120)\n"
        "   faceid=3, 人脸置信度=0.80, name=未知, 人物识别置信度=0.10, 脸中心位置=(200,150)\n"
        "]\n"
        "\n"
        "用户正文: 你好"
    )
    sight = _extract_face_sight_lines(message)
    assert sight is not None and "faceid=2" in sight and "faceid=3" in sight
    assert "用户正文" not in sight
    # 无 faceid 行（未识别到人脸）→ None
    no_faces = "[机器人传感器信息:\n图像识别:\n   (未检测到人脸)\n]\n\n用户正文: 你好"
    assert _extract_face_sight_lines(no_faces) is None
    assert _extract_face_sight_lines("") is None


class _RecEvents:
    def __init__(self) -> None:
        self.turns: list[dict] = []
        self.touched: list[tuple[str, str]] = []

    async def publish_turn(self, event: dict) -> None:
        self.turns.append(event)

    async def touch_device(self, device_id: str, status: str) -> None:
        self.touched.append((device_id, status))


def test_publish_chat_turn_carries_new_fields(temp_db, fresh_store):
    """e2e 事件应带 llm_calls / llm_model / tts_model / audio 标记。"""
    from deskbot_server.model.chat import ChatTurnResult
    from deskbot_server.service.application.chat_flow import publish_chat_turn
    from deskbot_server.service.application.convo_audio_store import ConvoAudioStore

    store = ConvoAudioStore()
    assert store.put("deskbot_audio", "req_1", "asr", SILENCE_16K, 16000)
    assert store.put("deskbot_audio", "req_1", "tts", SILENCE_16K, 16000)
    assert store.put_raw("deskbot_audio", "req_1", "face", b"jpeg-bytes")

    turn = ChatTurnResult(
        llm_text="你好呀",
        llm_calls=[
            {"n": 1, "model": "qwen3.8-2b", "ms": 1200, "text": '{"tts":"你好","tools":[]}', "truncated": False},
            {"n": 2, "model": "qwen3.8-2b", "ms": 800, "text": '{"tts":"你好呀","tools":[]}', "truncated": False},
        ],
        system_prompt="你是小歪，桌面机器人。\n[附录] 可用动作表情……",
        face_sight="图像识别:\n  faceid=2, 人脸置信度=0.91, name=妈妈, 人物识别置信度=0.72",
        t_llm_end=1.0,
        t_tts_synth_end=1.4,
        t_tts_end=2.0,
    )
    rec = _RecEvents()
    evt_holder: dict = {}

    async def _run():
        await publish_chat_turn(
            rec,
            "deskbot_audio",
            source="asr",
            asr_text="你好",
            t_asr_start=0.0,
            t_asr_text=0.2,
            turn=turn,
            request_id="req_1",
        )

    import asyncio

    asyncio.run(_run())
    evt = rec.turns[0]
    evt_holder.update(evt)
    assert len(evt["llm_calls"]) == 2
    assert evt["llm_calls"][0]["model"] == "qwen3.8-2b"
    assert evt["llm_model"] == "qwen3.8-2b"
    assert evt["audio_asr"] is True
    assert evt["audio_tts"] is True
    assert evt["asr_ms"] == 200
    assert evt["tts_ms"] == 400
    # 人脸画面与识别文本 / system prompt 透传
    assert evt["face_img"] is True
    assert evt["face_sight"] == turn.face_sight
    assert evt["system_prompt"] == turn.system_prompt
    # 标签在已初始化 DB 下可按设备解析（未注册设备回落默认 provider）
    assert evt["asr_model"] == "funasr · SenseVoiceSmall"
    assert evt["tts_model"] == "moss-tts-nano · demo-1"
    assert rec.touched == [("deskbot_audio", "ok")]


def test_publish_chat_turn_audio_flags_false_when_absent(temp_db, fresh_store):
    """仓库无音频 / source 非 asr 时，audio_* 与模型字段应为 False/None。"""
    from deskbot_server.model.chat import ChatTurnResult
    from deskbot_server.service.application.chat_flow import publish_chat_turn

    turn = ChatTurnResult(llm_text="文本问答回复", t_llm_end=1.0, t_tts_synth_end=1.3, t_tts_end=2.0)
    rec = _RecEvents()

    async def _run():
        await publish_chat_turn(
            rec,
            "deskbot_audio",
            source="text",
            asr_text="文本问题",
            t_asr_start=None,
            t_asr_text=0.2,
            turn=turn,
            request_id="req_2",
        )

    import asyncio

    asyncio.run(_run())
    evt = rec.turns[0]
    assert evt["llm_calls"] == []
    assert evt["llm_model"] is None
    assert evt["audio_asr"] is False
    assert evt["audio_tts"] is False
    assert evt["face_img"] is False
    assert evt["face_sight"] is None  # text/scheduled 轮无视觉
    assert evt["system_prompt"] is None
    assert evt["asr_model"] is None  # source=text 不解析 asr
    assert evt["tts_model"] == "moss-tts-nano · demo-1"


@pytest.fixture()
def pipeline_client(fresh_store):
    """仅挂 controller 路由的微型 FastAPI（鉴权语义与 /api/pipeline_recent 一致）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from deskbot_server.controller.web_controller import router

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="convo-audio-test-secret")
    app.include_router(router)
    return TestClient(app)


class TestPipelineAudioApi:
    def test_get_wav_when_present(self, pipeline_client, fresh_store):
        store = fresh_store.ConvoAudioStore()
        assert store.put("dev1", "rid1", "tts", SILENCE_16K, 16000)
        resp = pipeline_client.get("/api/pipeline_audio", params={"device_id": "dev1", "request_id": "rid1", "kind": "tts"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/wav")
        assert resp.content[:4] == b"RIFF"

    def test_get_face_jpeg_when_present(self, pipeline_client, fresh_store):
        store = fresh_store.ConvoAudioStore()
        assert store.put_raw("dev1", "rid1", "face", b"\xff\xd8jpeg")
        resp = pipeline_client.get("/api/pipeline_audio", params={"device_id": "dev1", "request_id": "rid1", "kind": "face"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/jpeg")
        assert resp.content == b"\xff\xd8jpeg"

    def test_miss_returns_404(self, pipeline_client, fresh_store):
        resp = pipeline_client.get(
            "/api/pipeline_audio", params={"device_id": "dev1", "request_id": "nope", "kind": "asr"}
        )
        assert resp.status_code == 404
        assert resp.json()["ok"] is False

    def test_invalid_params(self, pipeline_client, fresh_store):
        bad_kind = pipeline_client.get(
            "/api/pipeline_audio", params={"device_id": "dev1", "request_id": "rid1", "kind": "mp3"}
        )
        assert bad_kind.status_code == 400
        missing = pipeline_client.get("/api/pipeline_audio", params={"device_id": "dev1", "kind": "asr"})
        assert missing.status_code == 400
