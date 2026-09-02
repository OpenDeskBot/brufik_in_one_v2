"""VprHttpClient 测试：fake vpr 引擎（HTTP），验证请求体 / 响应解析 / 错误码映射。"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from deskbot_server.infrastructure.voice.vpr_http_client import (
    DEFAULT_VPR_TIMEOUT_S,
    VPR_EMBEDDING_DIM,
    VprHttpClient,
    VprHttpError,
)

_EMB_256 = [0.1] * VPR_EMBEDDING_DIM


class _FakeVprEngine:
    """极简 wespeaker 模拟：/voiceprint（响应体/状态码可注入，记录请求）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.response_body: dict = {"embedding": _EMB_256, "dim": VPR_EMBEDDING_DIM, "elapsed_ms": 3}
        self.status: int = 200
        self.last_body: dict | None = None
        self.last_sample_rate: int | None = None
        self.last_audio_len: int | None = None
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    class _Handler(BaseHTTPRequestHandler):
        engine = None  # 由 start 注入

        def do_POST(self):  # /voiceprint
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {}
            self.engine.last_body = body
            self.engine.last_audio_len = len(base64.b64decode(body.get("audio_base64") or ""))
            self.engine.last_sample_rate = int(body.get("sample_rate") or 0)
            payload = json.dumps(self.engine.response_body).encode("utf-8")
            self.send_response(self.engine.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

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
    engine = _FakeVprEngine(port=9241)
    engine.start()
    try:
        yield engine
    finally:
        engine.stop()


@pytest.fixture()
def client(fake_engine):
    return VprHttpClient("http://127.0.0.1:9241", timeout_s=DEFAULT_VPR_TIMEOUT_S)


def test_embedding_posts_json_returns_256d_vector(client, fake_engine):
    pcm = b"\x00\x01\x02\x03" * 4000
    emb = asyncio.run(client.embedding(pcm, sample_rate=16000))
    assert emb == _EMB_256
    assert fake_engine.last_sample_rate == 16000
    assert fake_engine.last_audio_len == len(pcm)  # 裸 PCM base64 直传


def test_embedding_empty_audio_raises(client):
    with pytest.raises(VprHttpError) as ei:
        asyncio.run(client.embedding(b"", sample_rate=16000))
    assert ei.value.code == "INVALID_AUDIO"


def test_embedding_422_raises_error_code(client, fake_engine):
    fake_engine.status = 422
    fake_engine.response_body = {"error": {"code": "AUDIO_TOO_SHORT", "message": "audio too short"}}
    with pytest.raises(VprHttpError) as ei:
        asyncio.run(client.embedding(b"\x00\x01" * 800, sample_rate=16000))
    assert ei.value.code == "AUDIO_TOO_SHORT"


def test_embedding_503_raises_model_not_ready(client, fake_engine):
    fake_engine.status = 503
    fake_engine.response_body = {"error": {"code": "MODEL_NOT_READY", "message": "loading"}}
    with pytest.raises(VprHttpError) as ei:
        asyncio.run(client.embedding(b"\x00\x01" * 800, sample_rate=16000))
    assert ei.value.code == "MODEL_NOT_READY"


def test_embedding_bad_payload_raises(client, fake_engine):
    fake_engine.response_body = {"embedding": "not-a-list"}
    with pytest.raises(VprHttpError):
        asyncio.run(client.embedding(b"\x00\x01" * 800, sample_rate=16000))


def test_embedding_wrong_dim_raises(client, fake_engine):
    fake_engine.response_body = {"embedding": [0.1] * 128, "dim": 128}
    with pytest.raises(VprHttpError):
        asyncio.run(client.embedding(b"\x00\x01" * 800, sample_rate=16000))


def test_embedding_unreachable_raises(client):
    """引擎未启动（关端口）→ 不可达错误，无 code。"""
    engine = _FakeVprEngine(port=9242)
    engine.start()
    engine.stop()  # 立即关掉，模拟未运行
    offline = VprHttpClient("http://127.0.0.1:9242", timeout_s=1.0)
    with pytest.raises(VprHttpError) as ei:
        asyncio.run(offline.embedding(b"\x00\x01" * 800, sample_rate=16000))
    assert ei.value.code is None
    assert "不可达" in str(ei.value)
