"""豆包 ASR 2.0（Seed-ASR Flash）测试：请求格式 / X-Api 响应解析 / 错误处理。

用本地 fake http server 模拟火山 API，不真实调用云端。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deskbot_server.infrastructure.asr.doubao import (
    STATUS_OK,
    STATUS_SILENCE,
    DoubaoAsrConfig,
    load_doubao_asr_config,
    merge_doubao_config,
    transcribe_doubao,
)
from deskbot_server.infrastructure.asr.doubao_adapter import DoubaoAsrAdapter
from deskbot_server.model.settings import AppSettings

PCM_1S = b"\x00\x00" * 16000  # 1s 静音 @16k


class _FakeVolcEngine:
    """模拟火山 Seed-ASR Flash：校验请求格式与鉴权头，回显固定文本。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.last_payload: dict | None = None
        self.last_headers: dict = {}
        self.status_code = STATUS_OK
        self.status_message = ""
        self.result_text = "豆包识别结果"
        self.http_code = 200
        self._server = None
        self._thread: threading.Thread | None = None

    class _Handler(BaseHTTPRequestHandler):
        engine = None  # 由 start 注入

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            self.engine.last_payload = json.loads(raw.decode("utf-8"))
            self.engine.last_headers = {k.lower(): v for k, v in self.headers.items()}
            body = json.dumps({"result": {"text": self.engine.result_text}}).encode("utf-8")
            self.send_response(self.engine.http_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Api-Status-Code", self.engine.status_code)
            self.send_header("X-Api-Message", self.engine.status_message)
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
    kwargs = {"api_key": "test-api-key", "url": f"http://127.0.0.1:{port}/recognize/flash"}
    kwargs.update(overrides)
    return DoubaoAsrConfig(**kwargs)


# ---------- transcribe_doubao：请求格式 ----------


def test_request_format(fake_volc):
    text = asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))
    assert text == "豆包识别结果"
    payload = fake_volc.last_payload
    assert payload["user"]["uid"] == "deskbot"
    assert payload["audio"]["format"] == "wav"
    assert payload["request"] == {"model_name": "bigmodel", "enable_punc": True, "enable_itn": True}
    # audio.data base64 → wav 可解析，采样率一致
    wav_bytes = base64.b64decode(payload["audio"]["data"])
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2


def test_request_headers(fake_volc):
    asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))
    headers = fake_volc.last_headers
    assert headers.get("x-api-key") == "test-api-key"
    assert headers.get("x-api-resource-id") == "volc.seedasr.auc"
    assert headers.get("x-api-request-id")
    assert headers.get("x-api-sequence") == "-1"


def test_request_preserves_sample_rate(fake_volc):
    asyncio.run(transcribe_doubao(PCM_1S, 8000, _cfg(fake_volc.port)))
    # rate 信息体现在 wav 容器内；base64 解码后验证
    wav_bytes = base64.b64decode(fake_volc.last_payload["audio"]["data"])
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getframerate() == 8000


# ---------- transcribe_doubao：响应解析 ----------


def test_response_ok(fake_volc):
    fake_volc.status_code = STATUS_OK
    text = asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))
    assert text == "豆包识别结果"


def test_response_silence_returns_empty(fake_volc):
    fake_volc.status_code = STATUS_SILENCE  # 静音：成功但空文本
    assert asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port))) == ""


def test_response_business_error_raises(fake_volc):
    fake_volc.status_code = "45000000"
    fake_volc.status_message = "invalid auth"
    with pytest.raises(RuntimeError, match="status=45000000"):
        asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))


def test_response_http_error_raises(fake_volc):
    fake_volc.http_code = 502
    with pytest.raises(RuntimeError, match="HTTP 502"):
        asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))


def test_response_missing_result_raises(fake_volc):
    fake_volc.status_code = STATUS_OK
    fake_volc.result_text = None  # body 变 {"result": {"text": null}}
    with pytest.raises(RuntimeError, match="响应异常"):
        asyncio.run(transcribe_doubao(PCM_1S, 16000, _cfg(fake_volc.port)))


def test_empty_pcm_returns_empty(fake_volc):
    assert asyncio.run(transcribe_doubao(b"", 16000, _cfg(fake_volc.port))) == ""


def test_audio_too_long_raises(fake_volc):
    pcm = b"\x00\x00" * 16000 * 61  # 61s
    with pytest.raises(RuntimeError, match="时长上限"):
        asyncio.run(transcribe_doubao(pcm, 16000, _cfg(fake_volc.port)))


# ---------- merge_doubao_config ----------


def test_merge_overrides_nonempty_wins():
    base = DoubaoAsrConfig(api_key="base-key")
    merged = merge_doubao_config(base, {"api_key": "", "uid": "custom-uid"})
    assert merged.api_key == "base-key"  # 空覆盖不生效
    assert merged.uid == "custom-uid"


# ---------- DoubaoAsrAdapter：装配与过滤 ----------


def _adapter(monkeypatch, port: int, **env) -> DoubaoAsrAdapter:
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "env-api-key")
    monkeypatch.setenv("DOUBAO_ASR_URL", f"http://127.0.0.1:{port}/recognize/flash")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    settings = AppSettings.from_config({"asr": {"provider": "doubao"}})
    return DoubaoAsrAdapter(settings)


def test_adapter_transcribe(monkeypatch, fake_volc):
    adapter = _adapter(monkeypatch, fake_volc.port)
    text = asyncio.run(adapter.transcribe(PCM_1S, 16000))
    assert text == "豆包识别结果"
    assert fake_volc.last_headers.get("x-api-key") == "env-api-key"


def test_adapter_overrides_win_over_env(monkeypatch, fake_volc):
    adapter = _adapter(monkeypatch, fake_volc.port)
    adapter2 = DoubaoAsrAdapter(
        AppSettings.from_config({"asr": {"provider": "doubao"}}),
        overrides={"api_key": "device-api-key"},
    )
    asyncio.run(adapter2.transcribe(PCM_1S, 16000))
    assert fake_volc.last_headers.get("x-api-key") == "device-api-key"
    assert adapter._cfg.api_key == "env-api-key"  # 不串扰


def test_adapter_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("DOUBAO_ASR_API_KEY", raising=False)
    settings = AppSettings.from_config({"asr": {"provider": "doubao"}})
    with pytest.raises(RuntimeError, match="API Key"):
        DoubaoAsrAdapter(settings)


def test_adapter_is_valid_text(monkeypatch, fake_volc):
    adapter = _adapter(monkeypatch, fake_volc.port)
    assert adapter.is_valid_text("你好世界")
    assert not adapter.is_valid_text("x")  # 短于 min_text_len=2


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "a")
    monkeypatch.setenv("DOUBAO_ASR_RESOURCE_ID", "r")
    monkeypatch.setenv("DOUBAO_ASR_UID", "custom-uid")
    monkeypatch.setenv("DOUBAO_ASR_URL", "http://custom/asr")
    cfg = load_doubao_asr_config()
    assert (cfg.api_key, cfg.resource_id, cfg.uid) == ("a", "r", "custom-uid")
    assert cfg.url == "http://custom/asr"


def test_load_config_defaults(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "a")
    cfg = load_doubao_asr_config()
    assert cfg.resource_id == "volc.seedasr.auc"
    assert cfg.uid == "deskbot"
    assert cfg.url.startswith("https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash")
