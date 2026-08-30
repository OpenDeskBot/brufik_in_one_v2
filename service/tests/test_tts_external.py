"""外部 TTS（moss-tts-nano）测试：MossTtsAdapter 转发 / 音素分片 / 错误处理。

用本地 fake http server 模拟 moss-tts-nano 进程（multipart /api/generate → WAV base64），
不加载真实模型。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from deskbot_server.infrastructure.tts.factory import DEFAULT_TTS_PROVIDER, build_tts_adapter
from deskbot_server.infrastructure.tts.moss_adapter import MossTtsAdapter
from deskbot_server.model.settings import AppSettings

FAKE_PORT = 9202
FAKE_BASE_URL = f"http://127.0.0.1:{FAKE_PORT}"


def _stereo_wav_bytes(n_samples: int = 4800) -> bytes:
    """48000 Hz 双声道 16-bit WAV（模拟 MOSS /api/generate 返回）。"""
    sr = 48000
    t = np.arange(n_samples) / sr
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    stereo = np.column_stack([tone, tone])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(stereo.tobytes())
    return buf.getvalue()


def _parse_form(body: bytes, content_type: str) -> dict[str, str]:
    """解析 multipart/form-data（测试用，只取文本字段）。"""
    boundary = content_type.split("boundary=", 1)[1].strip('"')
    fields: dict[str, str] = {}
    for part in body.split(f"--{boundary}".encode()):
        if b"Content-Disposition" not in part:
            continue
        head, _, value = part.partition(b"\r\n\r\n")
        m = re.search(rb'name="([^"]+)"', head)
        if m:
            fields[m.group(1).decode()] = value.rstrip(b"\r\n").decode("utf-8")
    return fields


class _FakeTtsEngine:
    """极简 moss-tts-nano 模拟：/api/generate（multipart → WAV base64，可注入错误）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.last_text: str | None = None
        self.last_demo_id: str | None = None
        self.error_status: int | None = None  # 非 None 时返回错误 JSON
        self._server = None
        self._thread: threading.Thread | None = None

    class _Handler(BaseHTTPRequestHandler):
        engine = None  # 由 start 注入

        def do_POST(self):  # /api/generate
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            fields = _parse_form(body, self.headers.get("Content-Type", ""))
            self.engine.last_text = fields.get("text", "")
            self.engine.last_demo_id = fields.get("demo_id", "")
            if self.engine.error_status is not None:
                payload = json.dumps({"error": "warmup not ready"}).encode("utf-8")
                self.send_response(self.engine.error_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = json.dumps(
                {
                    "audio_base64": base64.b64encode(_stereo_wav_bytes()).decode("ascii"),
                    "sample_rate": 48000,
                }
            ).encode("utf-8")
            self.send_response(200)
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
    engine = _FakeTtsEngine(port=FAKE_PORT)
    engine.start()
    try:
        yield engine
    finally:
        engine.stop()


def _adapter(base_url: str = FAKE_BASE_URL, **tts_extra) -> MossTtsAdapter:
    settings = AppSettings.from_config(
        {"tts": {"provider": "moss-tts-nano", "sample_rate": 48000, **tts_extra}}
    )
    adapter = MossTtsAdapter(settings)
    adapter.base_url = base_url.rstrip("/")  # 测试必须覆盖默认 9101，避免打到真实服务
    return adapter


# ------------------------------------------------------------------ MossTtsAdapter


def test_moss_adapter_synthesize(fake_engine):
    adapter = _adapter()

    async def run():
        return await adapter.synthesize_phoneme_segments("你好，这是测试")

    sr, segs = asyncio.run(run())
    assert sr == 48000
    assert segs, "应产出音素分片"
    assert any(s.phoneme for s in segs)  # 至少一个真实口型音素（非纯静音片）
    # 分片 PCM 合计 = WAV 的 mono PCM 总量（100ms @ 48k mono = 9600 字节）
    total = sum(len(s.pcm) for s in segs)
    assert total == 4800 * 2
    # 文本与默认音色透传到服务端
    assert fake_engine.last_text == "你好，这是测试"
    assert fake_engine.last_demo_id == "demo-1"


def test_moss_adapter_custom_demo_id(fake_engine):
    adapter = _adapter(demo_id="demo-3")

    async def run():
        return await adapter.synthesize_phoneme_segments("切换音色")

    asyncio.run(run())
    assert fake_engine.last_demo_id == "demo-3"


def test_moss_adapter_empty_text(fake_engine):
    adapter = _adapter()
    sr, segs = asyncio.run(adapter.synthesize_phoneme_segments("   "))
    assert sr == 48000
    assert segs == []
    assert fake_engine.last_text is None  # 未请求服务端


def test_moss_adapter_unreachable():
    adapter = _adapter(base_url="http://127.0.0.1:19999")
    with pytest.raises(RuntimeError, match="moss-tts-nano 引擎不可达"):
        asyncio.run(adapter.synthesize_phoneme_segments("你好"))


def test_moss_adapter_http_error(fake_engine):
    fake_engine.error_status = 500
    adapter = _adapter()
    with pytest.raises(RuntimeError, match="warmup not ready"):
        asyncio.run(adapter.synthesize_phoneme_segments("你好"))


def test_moss_adapter_no_audio_field(fake_engine):
    fake_engine.error_status = 200  # 200 但 body 无 audio_base64
    adapter = _adapter()
    with pytest.raises(RuntimeError, match="moss-tts-nano 无音频"):
        asyncio.run(adapter.synthesize_phoneme_segments("你好"))


def test_build_tts_adapter_default_is_moss():
    """默认 provider（无配置）解析为 MossTtsAdapter。"""
    settings = AppSettings.from_config({"tts": {}})
    assert settings.tts.provider == DEFAULT_TTS_PROVIDER
    assert isinstance(build_tts_adapter(settings), MossTtsAdapter)


def test_build_tts_adapter_unknown_falls_back_to_moss():
    settings = AppSettings.from_config({"tts": {"provider": "bogus"}})
    assert isinstance(build_tts_adapter(settings), MossTtsAdapter)


def test_doubao_config_speaker_precedence(monkeypatch):
    """豆包音色优先级：config tts.doubao_speaker > env DOUBAO_TTS_SPEAKER。"""
    from deskbot_server.infrastructure.tts.doubao import load_doubao_tts_config

    monkeypatch.setenv("DOUBAO_TTS_SPEAKER", "env_speaker")
    assert load_doubao_tts_config({}).speaker == "env_speaker"
    assert load_doubao_tts_config({"doubao_speaker": "cfg_speaker"}).speaker == "cfg_speaker"
