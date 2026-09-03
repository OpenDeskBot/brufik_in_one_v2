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

from deskbot_server.dao.device_mapper import get_asr_param, set_asr_provider, update_asr_param
from deskbot_server.infrastructure.asr.audio_norm import DEFAULT_PCM_SAMPLE_RATE, parse_audio_pcm, resample_pcm
from deskbot_server.infrastructure.asr.env_store import _mask_secret
from deskbot_server.infrastructure.asr.resolve import resolve_asr_adapter
from deskbot_server.model.settings import AppSettings
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
        "DOUBAO_ASR_API_KEY",
        "DOUBAO_ASR_RESOURCE_ID",
        "DOUBAO_ASR_UID",
        "DOUBAO_ASR_URL",
    ):
        monkeypatch.setenv(name, "")


def _insert_device(device_id: str = "dev-1"):
    """建用户并绑定设备（temp_db fixture 下，先置为在线）。"""
    from tests._auth_compat import create_user
    from tests.device_bind_helpers import bind_device_online

    user = create_user(f"{device_id}@example.com", "password1234")
    bind_device_online(user.id, device_id)
    return user


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
    assert info["doubao"]["api_key"] == ""
    assert info["doubao"]["api_key_set"] is False
    assert info["doubao"]["url"].startswith("https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash")
    assert info["doubao"]["resource_id"] == "volc.seedasr.auc"
    assert info["doubao"]["uid"] == "deskbot"


def test_asr_config_info_device_param_wins(temp_db, tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _insert_device()
    svc = _asr_svc(tmp_path)
    svc.save_device_asr_config(
        "dev-1",
        {"funasr": {"url": "http://127.0.0.1:9999"}, "doubao": {"api_key": "dev-key", "uid": "dev-uid"}},
    )
    info = svc.asr_config_info("dev-1")
    assert info["funasr_url"] == "http://127.0.0.1:9999"  # 设备 url > config.yaml
    assert info["doubao"]["api_key"] == _mask_secret("dev-key")
    assert info["doubao"]["api_key_set"] is True
    assert info["doubao"]["uid"] == "dev-uid"


def test_save_device_asr_config_writes_db_not_env(temp_db, tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _insert_device()
    svc = _asr_svc(tmp_path)
    info = svc.save_device_asr_config(
        "dev-1", {"funasr": {"url": "http://127.0.0.1:9102"}, "doubao": {"api_key": "key-1", "uid": "u"}}
    )
    params = get_asr_param("dev-1")
    assert params["doubao"]["api_key"] == "key-1"
    assert params["funasr"]["url"] == "http://127.0.0.1:9102"
    assert info["doubao"]["api_key"] == _mask_secret("key-1")  # 掩码展示
    assert info["doubao"]["api_key_set"] is True
    assert tmp_env.read_text(encoding="utf-8") == ""  # 不再写 .env
    assert not os.environ.get("DOUBAO_ASR_API_KEY")


def test_save_device_asr_config_masked_api_key_keeps_existing(temp_db, tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _insert_device()
    svc = _asr_svc(tmp_path)
    svc.save_device_asr_config("dev-1", {"doubao": {"api_key": "secret-key"}})
    svc.save_device_asr_config("dev-1", {"doubao": {"api_key": _mask_secret("secret-key")}})
    assert get_asr_param("dev-1")["doubao"]["api_key"] == "secret-key"  # 掩码不覆盖


def test_save_device_asr_config_empty_fields_fill_from_device_only(temp_db, tmp_path, monkeypatch, tmp_env):
    """保存只落 payload / 设备已有值：空字段不落键，无全局 env 播种（密钥设备自配）。"""
    _clean_doubao_env(monkeypatch)
    _insert_device()
    svc = _asr_svc(tmp_path)
    # 首存：api_key 留空 → 不落键（env 已移除，不播种）
    svc.save_device_asr_config("dev-1", {"doubao": {"uid": "custom-uid"}})
    params = get_asr_param("dev-1")
    assert "api_key" not in params["doubao"]
    assert params["doubao"]["uid"] == "custom-uid"
    # 二次存：api_key 落新值，uid 留空 → 设备已有
    svc.save_device_asr_config("dev-1", {"doubao": {"api_key": "new-key"}})
    assert get_asr_param("dev-1")["doubao"]["uid"] == "custom-uid"


def test_save_device_asr_config_empty_payload_clears(temp_db, tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _insert_device()
    svc = _asr_svc(tmp_path)
    svc.save_device_asr_config("dev-1", {})  # 无设备已有、无 env → 全空 → 列置 NULL
    assert get_asr_param("dev-1") == {}


def test_save_device_asr_config_no_device_raises(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    svc = _asr_svc(tmp_path)
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.save_device_asr_config(None, {"doubao": {"api_key": "k"}})


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

    def fake_post(url, body, cfg, reqid):
        captured["cfg"] = cfg
        captured["payload"] = json.loads(body.decode("utf-8"))
        return respond()

    monkeypatch.setattr("deskbot_server.infrastructure.asr.doubao._post", fake_post)
    return captured


def _doubao_ok(result_text: str = "豆包识别"):
    return lambda: (200, {"X-Api-Status-Code": "20000000"}, {"result": {"text": result_text}})


def test_asr_test_doubao_success_with_overrides(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    captured = _fake_doubao_post(monkeypatch, _doubao_ok())
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test(
            "doubao",
            _stereo_wav_bytes(4800),
            doubao_overrides={"api_key": "k-1", "uid": "u-1"},
        )
    )
    assert result["success"] is True
    assert result["text"] == "豆包识别"
    assert result["http_code"] == 200
    assert result["business_code"] == "20000000"
    assert captured["cfg"].api_key == "k-1"
    assert captured["payload"]["user"]["uid"] == "u-1"
    assert captured["payload"]["request"]["model_name"] == "bigmodel"
    assert captured["payload"]["audio"]["format"] == "wav"
    assert captured["payload"]["audio"]["data"]  # base64 wav


def test_asr_test_doubao_override_key_and_builtin_defaults(tmp_path, monkeypatch, tmp_env):
    """无覆盖/设备参数时 key 不再回落 env：override 给 key，其余字段回落内置默认。"""
    _clean_doubao_env(monkeypatch)
    captured = _fake_doubao_post(monkeypatch, _doubao_ok())
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test("doubao", _stereo_wav_bytes(4800), doubao_overrides={"api_key": "key-1"})
    )
    assert result["success"] is True
    cfg = captured["cfg"]
    assert cfg.api_key == "key-1"
    assert cfg.resource_id and cfg.uid and cfg.url  # 未覆盖字段回落内置默认


def test_asr_test_doubao_missing_key_fails_cleanly(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _fake_doubao_post(monkeypatch, _doubao_ok())
    svc = _asr_svc(tmp_path)
    result = asyncio.run(svc.asr_test("doubao", _stereo_wav_bytes(4800)))
    assert result["success"] is False  # 无任何 key 源（env 已移除）→ 校验失败


def test_asr_test_doubao_device_param_wins_over_env(temp_db, tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    tmp_env.write_text("DOUBAO_ASR_API_KEY=env-key\n", encoding="utf-8")
    _insert_device()
    svc = _asr_svc(tmp_path)
    svc.save_device_asr_config("dev-1", {"doubao": {"api_key": "dev-key"}})
    captured = _fake_doubao_post(monkeypatch, _doubao_ok())
    result = asyncio.run(svc.asr_test("doubao", _stereo_wav_bytes(4800), device_id="dev-1"))
    assert result["success"] is True
    assert captured["cfg"].api_key == "dev-key"  # 设备 asr_param > env


def test_asr_test_doubao_masked_key_without_backing_fails(tmp_path, monkeypatch, tmp_env):
    """掩码占位视为空；无设备参数、无全局 env（已移除）→ 缺 key 失败。"""
    _clean_doubao_env(monkeypatch)
    masked = _mask_secret("env-key")
    captured = _fake_doubao_post(monkeypatch, _doubao_ok())
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test(
            "doubao",
            _stereo_wav_bytes(4800),
            doubao_overrides={"api_key": masked},
        )
    )
    assert result["success"] is False  # 掩码不生效，无 env 兜底


def test_asr_test_doubao_silence_ok(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _fake_doubao_post(
        monkeypatch, lambda: (200, {"X-Api-Status-Code": "20000003"}, {"result": {"text": ""}})
    )
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test("doubao", _stereo_wav_bytes(4800), doubao_overrides={"api_key": "k"})
    )
    assert result["success"] is True  # 静音 = 成功语义
    assert result["text"] == ""
    assert result["business_code"] == "20000003"


def test_asr_test_doubao_business_error(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _fake_doubao_post(
        monkeypatch,
        lambda: (200, {"X-Api-Status-Code": "45000000", "X-Api-Message": "invalid auth"}, {}),
    )
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test("doubao", _stereo_wav_bytes(4800), doubao_overrides={"api_key": "k"})
    )
    assert result["success"] is False
    assert result["http_code"] == 200
    assert result["business_code"] == "45000000"
    assert "invalid auth" in result["error"]


def test_asr_test_doubao_http_error(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _fake_doubao_post(monkeypatch, lambda: (401, {}, {}))
    svc = _asr_svc(tmp_path)
    result = asyncio.run(
        svc.asr_test("doubao", _stereo_wav_bytes(4800), doubao_overrides={"api_key": "k"})
    )
    assert result["success"] is False
    assert result["http_code"] == 401


def test_asr_test_doubao_missing_config_returns_result(tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    svc = _asr_svc(tmp_path)
    result = asyncio.run(svc.asr_test("doubao", _stereo_wav_bytes(4800)))
    assert result["success"] is False
    assert "API Key" in result["error"]
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
    assert isinstance(payload["doubao"]["api_key_set"], bool)
    assert "url" in payload["doubao"]
    assert "api_key" in payload["doubao"]


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
    """保存 → 写当前设备 asr_param（DB），不再写 .env。"""
    env_file = tmp_path / ".env"
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", env_file)
    _clean_doubao_env(monkeypatch)
    from deskbot_server.web.app import create_app
    from tests._auth_compat import create_user
    from tests.device_bind_helpers import bind_device_online

    email = "asr-cfg-save@example.com"
    user = create_user(email, "password1234")
    bind_device_online(user.id, "brfk_asr")
    client = create_app().test_client()
    client.post("/login", data={"email": email, "password": "password1234"})
    client.post("/app/api/devices/select", json={"device_id": "brfk_asr"})

    resp = client.post(
        "/api/robot-settings/asr/config",
        json={"funasr": {"url": "http://127.0.0.1:9102"}, "doubao": {"api_key": "key-1", "uid": "u-1"}},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["doubao"]["api_key_set"] is True
    assert payload["doubao"]["uid"] == "u-1"
    assert payload["funasr_url"] == "http://127.0.0.1:9102"
    params = get_asr_param("brfk_asr")
    assert params["doubao"]["api_key"] == "key-1"  # DB 落库
    assert params["funasr"]["url"] == "http://127.0.0.1:9102"
    assert not env_file.exists()  # .env 未创建 = 不再写全局 env


def test_api_asr_config_save_endpoint_no_device_400(temp_db, monkeypatch, tmp_path):
    _clean_doubao_env(monkeypatch)
    client = _login_client("asr-cfg-nodev@example.com")  # 未选设备
    resp = client.post(
        "/api/robot-settings/asr/config", json={"doubao": {"api_key": "k"}}
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


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


# ---------- resolve_asr_adapter：设备 asr_param 优先级 ----------


def _settings() -> AppSettings:
    return AppSettings.from_config(MINIMAL_ASR_CFG)


def test_resolve_funasr_device_url_wins(temp_db, tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _insert_device()
    svc = _asr_svc(tmp_path)
    svc.save_device_asr_config("dev-1", {"funasr": {"url": "http://127.0.0.1:9999"}})
    adapter = resolve_asr_adapter("dev-1", settings=_settings())
    assert adapter.base_url == "http://127.0.0.1:9999"  # 设备 url > config.yaml


def test_resolve_funasr_falls_back_config_url(temp_db, tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    _insert_device()
    adapter = resolve_asr_adapter("dev-1", settings=_settings())
    assert adapter.base_url == "http://127.0.0.1:9102"  # 无设备参数 → config.yaml


def test_resolve_doubao_device_overrides_env(temp_db, tmp_path, monkeypatch, tmp_env):
    _clean_doubao_env(monkeypatch)
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "env-key")
    _insert_device()
    set_asr_provider("dev-1", "doubao")
    svc = _asr_svc(tmp_path)
    svc.save_device_asr_config("dev-1", {"doubao": {"api_key": "dev-key"}})
    adapter = resolve_asr_adapter("dev-1", settings=_settings())
    assert adapter._cfg.api_key == "dev-key"  # 设备 asr_param > env


def test_resolve_doubao_masked_param_skipped(temp_db, tmp_path, monkeypatch, tmp_env):
    """防御：asr_param 里存了掩码占位（绕过保存语义）→ 视为缺 key，构造即抛引导错误（无 env 兜底）。"""
    _clean_doubao_env(monkeypatch)
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "env-key")  # 残留 env 也不应生效
    _insert_device()
    set_asr_provider("dev-1", "doubao")
    update_asr_param("dev-1", json.dumps({"doubao": {"api_key": _mask_secret("x")}}))
    with pytest.raises(RuntimeError, match="API Key"):
        resolve_asr_adapter("dev-1", settings=_settings())
