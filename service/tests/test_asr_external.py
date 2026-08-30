"""外部 ASR（funasr）测试：FunAsrAdapter 转发 / provider 切换 / server 路由。

用本地 fake http server 模拟 funasr 进程，不加载真实模型。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deskbot_server.infrastructure.asr.doubao_adapter import DoubaoAsrAdapter
from deskbot_server.infrastructure.asr.funasr_adapter import FunAsrAdapter
from deskbot_server.infrastructure.bootstrap import build_asr_adapter
from deskbot_server.model.settings import AppSettings, AsrTextFilterSettings


class _FakeAsrEngine:
    """极简 funasr 模拟：/health + /transcribe（回显固定文本）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.last_sample_rate: int | None = None
        self.last_pcm_len: int | None = None
        self._server = None
        self._thread: threading.Thread | None = None

    class _Handler(BaseHTTPRequestHandler):
        engine = None  # 由 start 注入

        def do_GET(self):  # /health
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "service": "funasr"}')

        def do_POST(self):  # /transcribe
            length = int(self.headers.get("Content-Length", 0))
            pcm = self.rfile.read(length)
            self.engine.last_pcm_len = len(pcm)
            self.engine.last_sample_rate = int(self.headers.get("X-Sample-Rate", 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"text": "你好，外部ASR引擎"}).encode("utf-8"))

        def log_message(self, *a):
            pass

    def start(self) -> None:
        self._Handler.engine = self
        self._server = HTTPServer(("127.0.0.1", self.port), self._Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture()
def fake_engine():
    engine = _FakeAsrEngine(port=9201)
    engine.start()
    try:
        yield engine
    finally:
        engine.stop()


# ------------------------------------------------------------------ FunAsrAdapter


def test_funasr_adapter_transcribe(fake_engine):
    adapter = FunAsrAdapter("http://127.0.0.1:9201", AsrTextFilterSettings())

    async def run():
        text = await adapter.transcribe(b"\x00\x01\x02" * 100, 16000)
        return text

    text = asyncio.run(run())
    assert text == "你好，外部ASR引擎"
    assert fake_engine.last_sample_rate == 16000
    assert fake_engine.last_pcm_len == 300


def test_funasr_adapter_unreachable():
    adapter = FunAsrAdapter("http://127.0.0.1:19999", AsrTextFilterSettings())

    async def run():
        with pytest.raises(RuntimeError, match="不可达"):
            await adapter.transcribe(b"x" * 10, 16000)

    asyncio.run(run())


def test_funasr_adapter_is_valid_text_local():
    """文本过滤是纯逻辑，本地执行（不依赖远程）。"""
    adapter = FunAsrAdapter("http://127.0.0.1:9201", AsrTextFilterSettings(min_text_len=2, min_chinese_ratio=0.0))
    assert adapter.is_valid_text("你好世界")
    assert not adapter.is_valid_text("a")  # 短于 min_text_len


# ------------------------------------------------------------------ provider 切换


def test_provider_switch(monkeypatch):
    """build_asr_adapter 按 provider 参数装配：funasr 默认 / doubao / 未知回落 funasr。"""
    settings = AppSettings.from_config({"asr": {"external_url": "http://127.0.0.1:9201"}})
    assert isinstance(build_asr_adapter("funasr", settings), FunAsrAdapter)
    assert isinstance(build_asr_adapter("bogus", settings), FunAsrAdapter)  # 未知回落
    for name in ("DOUBAO_ASR_APP_ID", "DOUBAO_ASR_ACCESS_TOKEN", "DOUBAO_ASR_CLUSTER"):
        monkeypatch.setenv(name, "test")
    assert isinstance(build_asr_adapter("doubao", settings), DoubaoAsrAdapter)


# ------------------------------------------------------------------ funasr server 路由


def test_asr_engine_server_routes(fake_engine):
    """server.py 的 /health 与 /transcribe 路由（注入 fake adapter，不加载模型）。"""
    import sys
    from pathlib import Path

    server_path = Path(__file__).resolve().parents[1] / "externals" / "funasr" / "server.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("asr_engine_server", server_path)
    server = importlib.util.module_from_spec(spec)
    sys.modules["asr_engine_server"] = server
    spec.loader.exec_module(server)

    class _FakeAdapter:
        def __init__(self):
            self.calls = []

        async def transcribe(self, pcm, sample_rate):
            self.calls.append((len(pcm), sample_rate))
            return "fake-text"

    fake = _FakeAdapter()
    server._adapter = fake

    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    # /health：未加载模型时 model_ready=False（不触发 startup，避免真实加载）
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True

    # /transcribe：注入 fake adapter 后正常返回（协议允许可选字段，断言必填 text）
    r = client.post("/transcribe", content=b"\x00\x01" * 50, headers={"X-Sample-Rate": "8000"})
    assert r.status_code == 200
    assert r.json()["text"] == "fake-text"
    assert fake.calls == [(100, 8000)]

    # 空 PCM → 400 empty_pcm（统一错误结构）
    r = client.post("/transcribe", content=b"", headers={"X-Sample-Rate": "16000"})
    assert r.status_code == 400
    assert r.json() == {"error": {"code": "empty_pcm", "message": "empty pcm"}}

    # 非法采样率 → 400 invalid_sample_rate
    r = client.post("/transcribe", content=b"\x00\x01" * 10, headers={"X-Sample-Rate": "abc"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_sample_rate"

    # 不支持的 Content-Type → 415
    r = client.post("/transcribe", content=b"x" * 10, headers={"Content-Type": "audio/ogg"})
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_media_type"

    # WAV 输入（自描述采样率 8000，忽略 X-Sample-Rate）→ 200
    import io
    import wave

    from deskbot_server.utils.audio import pcm_to_wav_bytes

    fake.calls.clear()
    wav_bytes = pcm_to_wav_bytes(b"\x00\x01" * 50, 8000)
    r = client.post(
        "/transcribe", content=wav_bytes, headers={"Content-Type": "audio/wav", "X-Sample-Rate": "99999"}
    )
    assert r.status_code == 200
    assert r.json()["text"] == "fake-text"
    assert fake.calls == [(100, 8000)]

    # 立体声 WAV → 400 invalid_wav
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x01" * 50)
    r = client.post("/transcribe", content=buf.getvalue(), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_wav"

    # adapter 抛异常 → 500 transcribe_failed
    async def _boom(pcm, sample_rate):
        raise RuntimeError("infer crash")

    fake.transcribe = _boom
    r = client.post("/transcribe", content=b"\x00\x01" * 50, headers={"X-Sample-Rate": "8000"})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "transcribe_failed"
