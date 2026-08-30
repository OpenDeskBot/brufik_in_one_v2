"""豆包 ASR（火山一句话识别 v1）测试：请求格式 / 响应解析 / 错误处理。

用本地 fake http server 模拟火山 API，不真实调用云端。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from deskbot_server.infrastructure.asr.doubao import (
    DoubaoAsrConfig,
    load_doubao_asr_config,
    transcribe_doubao,
)
from deskbot_server.infrastructure.asr.doubao_adapter import DoubaoAsrAdapter
from deskbot_server.model.settings import AppSettings

PCM_1S = b"\x00\x00" * 16000  # 1s 静音 @16k


class _FakeVolcEngine:
    """模拟火山一句话识别：校验请求格式，回显固定文本。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.last_payload: dict | None = None
        self.response_code = 0
        self.response_message = "Success"
        self.result: list[dict] | str = [{"text": "豆包识别结果"}]
        self._server = None
        self._thread: threading.Thread | None = None

    class _Handler(BaseHTTPRequestHandler):
        engine = None  # 由 start 注入

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            (header_len,) = struct.unpack(">I", raw[:4])
            self.engine.last_payload = json.loads(raw[4 : 4 + header_len])
            body = json.dumps(
                {"code": self.engine.response_code, "message": self.engine.response_message, "result": self.engine.result}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    def start(self) -> None:
        self._Handler.engine = self
        self._server = HTTPServer(("127.0.0.1", self.port), self._Handler)
        self.port = self._server.server_address[1]  # port=0 时回填实际分配端口
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)


@pytest.fixture
def fake_volc():
    engine = _FakeVolcEngine(0)  # 端口 0 → 自动分配
    engine.start()
    yield engine
    engine.stop()


def _cfg(port: int, **overrides) -> DoubaoAsrConfig:
    kwargs = {"app_id": "test-app", "access_token": "test-token", "cluster": "test-cluster", "url": f"http://127.0.0.1:{port}/asr"}
    kwargs.update(overrides)
    return DoubaoAsrConfig(**kwargs)


# ---------- transcribe_doubao：请求格式 ----------


def test_request_format(fake_volc):
    text = asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))
    assert text == "豆包识别结果"
    payload = fake_volc.last_payload
    assert payload["app"] == {"appid": "test-app", "token": "test-token", "cluster": "test-cluster"}
    assert payload["user"]["uid"] == "deskbot"
    assert payload["audio"] == {"format": "wav", "rate": 16000, "bits": 16, "channel": 1}
    assert payload["request"]["workflow"].startswith("audio_in,")
    assert payload["request"]["sequence"] == -1
    # audio_data base64 → wav 可解析，采样率一致
    wav_bytes = base64.b64decode(payload["audio_data"])
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2


def test_request_preserves_sample_rate(fake_volc):
    asyncio.run(transcribe_doubao(PCM_1S, 8000, _cfg(fake_volc.port)))
    assert fake_volc.last_payload["audio"]["rate"] == 8000


# ---------- transcribe_doubao：响应解析 ----------


def test_response_result_str_variant(fake_volc):
    fake_volc.result = "字符串形式的识别结果"
    text = asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))
    assert text == "字符串形式的识别结果"


def test_response_nonzero_code_raises(fake_volc):
    fake_volc.response_code = 4000
    fake_volc.response_message = "invalid auth"
    with pytest.raises(RuntimeError, match="code=4000"):
        asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))


def test_empty_pcm_returns_empty(fake_volc):
    assert asyncio.run(transcribe_doubao(b"", 16000, _cfg(fake_volc.port))) == ""


def test_audio_too_long_raises(fake_volc):
    pcm = b"\x00\x00" * 16000 * 61  # 61s
    with pytest.raises(RuntimeError, match="超时上限"):
        asyncio.run(transcribe_doubao(pcm, 16000, _cfg(fake_volc.port)))


# ---------- DoubaoAsrAdapter：装配与过滤 ----------


def _adapter(monkeypatch, port: int, **env) -> DoubaoAsrAdapter:
    monkeypatch.setenv("DOUBAO_ASR_APP_ID", "test-app")
    monkeypatch.setenv("DOUBAO_ASR_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("DOUBAO_ASR_CLUSTER", "test-cluster")
    monkeypatch.setenv("DOUBAO_ASR_URL", f"http://127.0.0.1:{port}/asr")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    settings = AppSettings.from_config({"asr": {"provider": "doubao"}})
    return DoubaoAsrAdapter(settings)


def test_adapter_transcribe(monkeypatch, fake_volc):
    adapter = _adapter(monkeypatch, fake_volc.port)
    text = asyncio.run(adapter.transcribe(PCM_1S, 16000))
    assert text == "豆包识别结果"
    assert fake_volc.last_payload["app"]["appid"] == "test-app"


def test_adapter_missing_config_raises(monkeypatch):
    monkeypatch.delenv("DOUBAO_ASR_APP_ID", raising=False)
    monkeypatch.delenv("DOUBAO_ASR_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DOUBAO_ASR_CLUSTER", raising=False)
    settings = AppSettings.from_config({"asr": {"provider": "doubao"}})
    with pytest.raises(RuntimeError, match="DOUBAO_ASR"):
        DoubaoAsrAdapter(settings)


def test_adapter_is_valid_text(monkeypatch, fake_volc):
    adapter = _adapter(monkeypatch, fake_volc.port)
    assert adapter.is_valid_text("你好世界")
    assert not adapter.is_valid_text("x")  # 短于 min_text_len=2


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_APP_ID", "a")
    monkeypatch.setenv("DOUBAO_ASR_ACCESS_TOKEN", "t")
    monkeypatch.setenv("DOUBAO_ASR_CLUSTER", "c")
    monkeypatch.setenv("DOUBAO_ASR_UID", "custom-uid")
    cfg = load_doubao_asr_config()
    assert (cfg.app_id, cfg.access_token, cfg.cluster, cfg.uid) == ("a", "t", "c", "custom-uid")
    assert cfg.url.startswith("https://openspeech.bytedance.com")
