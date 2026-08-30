"""机器人设置页 ASR 配置/测试（config-info / config / test）测试。

覆盖：音频归一化（audio_norm）、豆包 .env 读写（env_store）、asr_test 服务层
（funasr 伪引擎 + doubao monkeypatch _post）、三个新 API 端点。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest
import yaml

from deskbot_server.infrastructure.asr.audio_norm import DEFAULT_PCM_SAMPLE_RATE, parse_audio_pcm, resample_pcm
from deskbot_server.infrastructure.asr.env_store import _mask_secret
from deskbot_server.service.robot_capability import CapabilityError, RobotCapabilityService

MINIMAL_ASR_CFG = {
    "asr": {"external_url": "http://127.0.0.1:9102", "text_filter": {"min_text_len": 2, "min_chinese_ratio": 0.0}}
}


# ---------- 通用 helpers / fixtures ----------


def _stereo_wav_bytes(n_samples: int = 4800) -> bytes:
    sr = 48000
    t = np.arange(n_samples) / sr
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.column_stack([tone, tone]).tobytes())
    return buf.getvalue()


def _wav_bytes(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _wav_8bit_bytes(pcm8: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(1)
        w.setframerate(sample_rate)
        w.writeframes(pcm8)
    return buf.getvalue()


def _float_wav_bytes() -> bytes:
    """wFormatTag=3（IEEE float）WAV：协议层不接受的位深/编码。"""
    n = 16
    fmt = struct.pack("<HHIIHH", 3, 1, 16000, 16000 * 4, 4, 32)
    data = struct.pack(f"<{n}f", *([0.0] * n))
    return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeAsrTestEngine:
    """模拟 funasr /transcribe：记录请求头，可配状态码与响应体（port=0 自动分配）。"""

    def __init__(self) -> None:
        self.port = 0
        self.status = 200
        self.body: dict = {"text": "测试识别结果"}
        self.last_sample_rate: str | None = None
        self.last_content_type: str | None = None
        self.last_pcm_len: int | None = None
        self._server = None
        self._thread: threading.Thread | None = None

    class _Handler(BaseHTTPRequestHandler):
        engine = None  # 由 start 注入

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            self.engine.last_sample_rate = self.headers.get("X-Sample-Rate")
            self.engine.last_content_type = self.headers.get("Content-Type")
            self.engine.last_pcm_len = len(body)
            payload = json.dumps(self.engine.body).encode("utf-8")
            self.send_response(self.engine.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    def start(self) -> None:
        self._Handler.engine = self
        self._server = HTTPServer(("127.0.0.1", 0), self._Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture()
def fake_asr_engine():
    engine = _FakeAsrTestEngine()
    engine.start()
    yield engine
    engine.stop()


@pytest.fixture()
def tmp_env(monkeypatch, tmp_path):
    """隔离 .env：read_env_file / update_env_keys 读的是 tts.env_store.ENV_FILE。"""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", env_file)
    return env_file


@pytest.fixture()
def temp_db(monkeypatch):
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        reset_engine()
        init_engine(db_path)
        init_database()
        yield db_path


def _asr_svc(tmp_path: Path, url: str = "http://127.0.0.1:9102") -> RobotCapabilityService:
    cfg = dict(MINIMAL_ASR_CFG)
    cfg["asr"] = {"external_url": url, "text_filter": {"min_text_len": 2, "min_chinese_ratio": 0.0}}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return RobotCapabilityService(config_path=p)


def _clean_doubao_env(monkeypatch):
    """置空并登记清理：save_* 会直接写 os.environ，setenv 保证 teardown 时被还原。"""
    for name in (
        "DOUBAO_ASR_APP_ID",
        "DOUBAO_ASR_ACCESS_TOKEN",
        "DOUBAO_ASR_CLUSTER",
        "DOUBAO_ASR_URL",
        "DOUBAO_ASR_UID",
        "DOUBAO_ASR_WORKFLOW",
    ):
        monkeypatch.setenv(name, "")


# ---------- audio_norm 单测 ----------


def test_parse_audio_stereo48k_to_mono():
    n = 4800
    stereo = np.column_stack([np.full(n, 1000, np.int16), np.full(n, 2000, np.int16)])
    raw = _wav_bytes(stereo.tobytes(), 48000, channels=2)
    pcm, sr = parse_audio_pcm(raw)
    assert sr == 48000
    assert len(pcm) == n * 2
    assert np.frombuffer(pcm, np.int16)[0] == 1000  # 取左声道


def test_parse_audio_8bit_expands():
    raw = _wav_8bit_bytes(bytes(range(256)), 8000, channels=1)
    pcm, sr = parse_audio_pcm(raw)
    assert sr == 8000
    assert len(pcm) == 256 * 2
    assert pcm[0] == 0 and pcm[1] == 0  # 首个样本 0 → 16bit LE 00 00


def test_parse_audio_raw_pcm_treated_16k():
    raw = b"\x00\x00" * 100
    pcm, sr = parse_audio_pcm(raw)
    assert pcm == raw
    assert sr == DEFAULT_PCM_SAMPLE_RATE


def test_parse_audio_empty_and_too_large_raise():
    with pytest.raises(ValueError, match="空"):
        parse_audio_pcm(b"")
    with pytest.raises(ValueError, match="过大"):
        parse_audio_pcm(b"x" * (16 * 1024 * 1024 + 1))


def test_parse_audio_non_pcm_wav_raises():
    with pytest.raises(ValueError):
        parse_audio_pcm(_float_wav_bytes())


def test_resample_48k_to_16k_length():
    pcm = np.zeros(48000, np.int16).tobytes()  # 1s @48k
    out = resample_pcm(pcm, 48000, 16000)
    assert len(out) == 16000 * 2


def test_resample_16k_identity():
    pcm = b"\x01\x02" * 100
    assert resample_pcm(pcm, 16000, 16000) == pcm


def test_resample_integer_downsample_mean():
    pcm = np.full(100, 3000, np.int16).tobytes()
    out = resample_pcm(pcm, 16000, 8000)
    assert len(out) == 100 * 2 // 2
    assert np.frombuffer(out, np.int16)[0] == 3000  # 均值后仍为原值


# ---------- asr_config_info / 保存 ----------


def test_asr_config_info_shape(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    svc = _asr_svc(tmp_path)
    info = svc.asr_config_info()
    assert info["funasr_url"] == "http://127.0.0.1:9102"
    assert info["default_audio"]["path"] == "data/test/asr.wav"
    assert info["doubao"]["app_id"] == ""
    assert info["doubao"]["access_token"] == ""
    assert info["doubao"]["access_token_set"] is False
    assert info["doubao"]["url"] == "https://openspeech.bytedance.com/api/v1/asr"  # 默认兜底
    assert info["doubao"]["uid"] == "deskbot"


def test_save_doubao_asr_config_writes_env(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    svc = _asr_svc(tmp_path)
    info = svc.save_doubao_asr_config({"app_id": "app-1", "access_token": "tok-1", "cluster": "cl-1"})
    text = tmp_env.read_text(encoding="utf-8")
    assert "DOUBAO_ASR_APP_ID=app-1" in text
    assert "DOUBAO_ASR_ACCESS_TOKEN=tok-1" in text
    assert "DOUBAO_ASR_CLUSTER=cl-1" in text
    assert "# 豆包云 ASR" in text
    assert info["doubao"]["app_id"] == "app-1"
    assert info["doubao"]["access_token"] == "*****"  # 掩码展示
    assert info["doubao"]["access_token_set"] is True
    assert os.environ.get("DOUBAO_ASR_APP_ID") == "app-1"  # 进程内同步


def test_save_doubao_asr_config_masked_token_keeps_existing(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    tmp_env.write_text("DOUBAO_ASR_ACCESS_TOKEN=secret-token\n", encoding="utf-8")
    svc = _asr_svc(tmp_path)
    svc.save_doubao_asr_config({"app_id": "app-1", "access_token": "sec*****ken", "cluster": "cl-1"})
    text = tmp_env.read_text(encoding="utf-8")
    assert "DOUBAO_ASR_ACCESS_TOKEN=secret-token" in text
    assert "sec*****ken" not in text


def test_save_doubao_asr_config_empty_fields_keep_existing(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    tmp_env.write_text(
        "DOUBAO_ASR_APP_ID=old-app\nDOUBAO_ASR_CLUSTER=old-cluster\nDOUBAO_ASR_URL=https://custom.example.com/asr\n",
        encoding="utf-8",
    )
    svc = _asr_svc(tmp_path)
    info = svc.save_doubao_asr_config({"app_id": "new-app"})  # 其余留空 → 保留
    text = tmp_env.read_text(encoding="utf-8")
    assert "DOUBAO_ASR_APP_ID=new-app" in text
    assert "DOUBAO_ASR_CLUSTER=old-cluster" in text
    assert "DOUBAO_ASR_URL=https://custom.example.com/asr" in text
    assert info["doubao"]["cluster"] == "old-cluster"


# ---------- asr_test：funasr ----------


def test_asr_test_funasr_success(fake_asr_engine, tmp_path):
    svc = _asr_svc(tmp_path, f"http://127.0.0.1:{fake_asr_engine.port}")
    result = asyncio.run(svc.asr_test("funasr", _stereo_wav_bytes(4800)))
    assert result["success"] is True
    assert result["http_code"] == 200
    assert result["text"] == "测试识别结果"
    assert result["provider"] == "funasr"
    assert result["elapsed_ms"] >= 0
    assert result["used_default"] is False
    assert result["sample_rate"] == DEFAULT_PCM_SAMPLE_RATE
    assert fake_asr_engine.last_sample_rate == "16000"  # 归一化后 16k
    assert fake_asr_engine.last_content_type == "application/octet-stream"


def test_asr_test_funasr_protocol_error(fake_asr_engine, tmp_path):
    fake_asr_engine.status = 503
    fake_asr_engine.body = {"error": {"code": "model_not_ready", "message": "model not ready"}}
    svc = _asr_svc(tmp_path, f"http://127.0.0.1:{fake_asr_engine.port}")
    result = asyncio.run(svc.asr_test("funasr", _stereo_wav_bytes(4800)))
    assert result["success"] is False
    assert result["http_code"] == 503
    assert result["business_code"] == "model_not_ready"
    assert "model not ready" in result["error"]


def test_asr_test_funasr_unreachable_http_code_zero(tmp_path):
    svc = _asr_svc(tmp_path, f"http://127.0.0.1:{_free_port()}")
    result = asyncio.run(svc.asr_test("funasr", _stereo_wav_bytes(4800)))
    assert result["success"] is False
    assert result["http_code"] == 0
    assert "不可达" in result["error"]


def test_asr_test_default_audio_used(fake_asr_engine, tmp_path, monkeypatch):
    sample = tmp_path / "sample.wav"
    sample.write_bytes(_stereo_wav_bytes(4800))
    monkeypatch.setattr("deskbot_server.service.robot_capability.DEFAULT_ASR_TEST_AUDIO", sample)
    svc = _asr_svc(tmp_path, f"http://127.0.0.1:{fake_asr_engine.port}")
    result = asyncio.run(svc.asr_test("funasr", None, use_default=True))
    assert result["used_default"] is True
    assert result["success"] is True
    assert result["text"] == "测试识别结果"


def test_asr_test_no_audio_raises(tmp_path):
    svc = _asr_svc(tmp_path)
    with pytest.raises(CapabilityError, match="未提供测试音频"):
        asyncio.run(svc.asr_test("funasr", None))


def test_asr_test_unknown_provider_raises(tmp_path):
    svc = _asr_svc(tmp_path)
    with pytest.raises(CapabilityError, match="未知的 ASR 能力"):
        asyncio.run(svc.asr_test("bogus", b"\x00\x00" * 100))


def test_asr_test_audio_too_long_raises(tmp_path):
    svc = _asr_svc(tmp_path)
    long_wav = _wav_bytes(b"\x00\x00" * 16000 * 61, 16000, channels=1)  # 61s
    with pytest.raises(CapabilityError, match="超时"):
        asyncio.run(svc.asr_test("funasr", long_wav))


# ---------- asr_test：doubao（monkeypatch _post） ----------


def _fake_doubao_post(monkeypatch, respond):
    captured = {}

    def fake_post(url, body, token):
        header_len = struct.unpack(">I", body[:4])[0]
        captured["payload"] = json.loads(body[4 : 4 + header_len])
        return respond()

    monkeypatch.setattr("deskbot_server.infrastructure.asr.doubao._post", fake_post)
    return captured


def test_asr_test_doubao_success_with_overrides(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    captured = _fake_doubao_post(
        monkeypatch, lambda: (200, {"code": 0, "message": "Success", "result": [{"text": "豆包识别"}]})
    )
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test(
            "doubao",
            _stereo_wav_bytes(4800),
            doubao_overrides={"app_id": "a", "access_token": "t", "cluster": "c"},
        )
    )
    assert result["success"] is True
    assert result["text"] == "豆包识别"
    assert result["http_code"] == 200
    assert result["business_code"] == 0
    assert captured["payload"]["app"] == {"appid": "a", "token": "t", "cluster": "c"}
    assert captured["payload"]["audio"]["rate"] == DEFAULT_PCM_SAMPLE_RATE


def test_asr_test_doubao_env_fallback(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    tmp_env.write_text(
        "DOUBAO_ASR_APP_ID=env-app\nDOUBAO_ASR_ACCESS_TOKEN=env-token\nDOUBAO_ASR_CLUSTER=env-cluster\n",
        encoding="utf-8",
    )
    captured = _fake_doubao_post(monkeypatch, lambda: (200, {"code": 0, "result": "ok"}))
    svc = _asr_svc(tmp_path)
    result = asyncio.run(svc.asr_test("doubao", _stereo_wav_bytes(4800)))
    assert result["success"] is True
    assert captured["payload"]["app"] == {"appid": "env-app", "token": "env-token", "cluster": "env-cluster"}


def test_asr_test_doubao_masked_token_falls_back_env(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    tmp_env.write_text("DOUBAO_ASR_ACCESS_TOKEN=env-token\n", encoding="utf-8")
    masked = _mask_secret("env-token")
    captured = _fake_doubao_post(monkeypatch, lambda: (200, {"code": 0, "result": "ok"}))
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test(
            "doubao",
            _stereo_wav_bytes(4800),
            doubao_overrides={"app_id": "a", "access_token": masked, "cluster": "c"},
        )
    )
    assert result["success"] is True
    assert captured["payload"]["app"]["token"] == "env-token"  # 掩码回落 env


def test_asr_test_doubao_business_error(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _fake_doubao_post(monkeypatch, lambda: (200, {"code": 2207, "message": "invalid cluster", "result": []}))
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test(
            "doubao",
            _stereo_wav_bytes(4800),
            doubao_overrides={"app_id": "a", "access_token": "t", "cluster": "c"},
        )
    )
    assert result["success"] is False
    assert result["http_code"] == 200
    assert result["business_code"] == 2207
    assert "invalid cluster" in result["error"]


def test_asr_test_doubao_http_error(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _fake_doubao_post(monkeypatch, lambda: (401, {}))
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test(
            "doubao",
            _stereo_wav_bytes(4800),
            doubao_overrides={"app_id": "a", "access_token": "t", "cluster": "c"},
        )
    )
    assert result["success"] is False
    assert result["http_code"] == 401


def test_asr_test_doubao_missing_config_returns_result(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    svc = _asr_svc(tmp_path)
    result = asyncio.run(svc.asr_test("doubao", _stereo_wav_bytes(4800)))
    assert result["success"] is False
    assert "缺少配置" in result["error"]
    assert result["http_code"] == 0


# ---------- API 层 ----------


def _login_client(email: str):
    from deskbot_server.web.app import create_app
    from tests._auth_compat import create_user

    create_user(email, "password1234")
    client = create_app().test_client()
    client.post("/login", data={"email": email, "password": "password1234"})
    return client


def test_api_asr_config_info_endpoint(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    client = _login_client("asr-cfg@example.com")
    resp = client.get("/api/robot-settings/asr/config-info")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["default_audio"]["path"] == "data/test/asr.wav"
    assert payload["funasr_url"]
    assert isinstance(payload["doubao"]["access_token_set"], bool)
    assert "url" in payload["doubao"]


def test_api_asr_default_audio_endpoint(temp_db, monkeypatch, tmp_path):
    """默认音频端点：返回 data/test/asr.wav 供对话框播放试听。"""
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    client = _login_client("asr-audio@example.com")

    resp = client.get("/api/robot-settings/asr/default-audio")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("audio/wav")
    assert resp.data[:4] == b"RIFF"  # WAV 魔数
    assert len(resp.data) > 1000


def test_api_asr_config_save_endpoint(temp_db, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", env_file)
    _clean_doubao_env(monkeypatch)
    client = _login_client("asr-cfg-save@example.com")
    resp = client.post(
        "/api/robot-settings/asr/config", json={"app_id": "app-1", "access_token": "tok-1", "cluster": "cl-1"}
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["doubao"]["app_id"] == "app-1"
    assert payload["doubao"]["access_token_set"] is True
    assert "DOUBAO_ASR_APP_ID=app-1" in env_file.read_text(encoding="utf-8")


def test_api_asr_test_endpoint_multipart(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    client = _login_client("asr-test@example.com")

    # 未提供音频且不用默认 → 400
    resp = client.post("/api/robot-settings/asr/test", data={"provider": "funasr", "use_default": "0"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    # 上传 WAV → 执行结果恒 200 + success 字段（funasr 9102 可能运行也可能没运行，只断言形状）
    resp = client.post(
        "/api/robot-settings/asr/test",
        data={
            "provider": "funasr",
            "use_default": "0",
            "audio": (io.BytesIO(_stereo_wav_bytes(4800)), "a.wav"),
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["provider"] == "funasr"
    assert payload["success"] in (True, False)
    assert "http_code" in payload
    assert "elapsed_ms" in payload
    assert "used_default" in payload
