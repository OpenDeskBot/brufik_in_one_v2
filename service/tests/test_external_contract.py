"""外部服务类型契约测试：七类契约定义 / test_service 接口 / manifest type/port 解析。"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from deskbot_server.service.external.contract import CONTRACTS, SERVICE_TYPES, SILENCE_PCM, resolve_test_spec
from deskbot_server.service.external.manager import (
    DEFAULT_ASR_TEST_AUDIO,
    DEFAULT_FACE_TEST_IMAGE,
    MAX_ASR_TEST_AUDIO_BYTES,
    MAX_FACE_TEST_IMAGE_BYTES,
    ExternalServiceManager,
    ServiceError,
)
from deskbot_server.service.external.manifest import ManifestError, ServiceManifest, TestSpec

SERVICE_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 契约定义


def test_contract_types_and_shape():
    assert set(SERVICE_TYPES) == {"asr", "tts", "llm", "vlm", "fr", "vpr"}
    for c in CONTRACTS.values():
        assert c["method"] == "POST"
        assert c["path"].startswith("/")
        assert isinstance(c["headers"], dict)
        assert isinstance(c["body"], bytes)
        assert isinstance(c["expect"], list) and c["expect"]
        assert c["description"]


def test_contract_samples_nonempty():
    assert CONTRACTS["asr"]["body"] == SILENCE_PCM and len(SILENCE_PCM) == 32000
    assert CONTRACTS["tts"]["body"]  # JSON text
    assert CONTRACTS["vlm"]["body"]  # JSON + image_base64
    assert CONTRACTS["fr"]["body"]  # JPEG bytes
    # JSON 类契约可解析
    for stype in ("tts", "llm", "vlm", "vpr"):
        assert json.loads(CONTRACTS[stype]["body"].decode("utf-8"))
    # vpr 契约内置样本：PCM int16 base64 + 16kHz
    vpr_body = json.loads(CONTRACTS["vpr"]["body"].decode("utf-8"))
    assert base64.b64decode(vpr_body["audio_base64"]) == SILENCE_PCM
    assert vpr_body["sample_rate"] == 16000


# ------------------------------------------------------------------ manifest type/port


def test_manifest_type_port(tmp_path):
    _write_manifest(tmp_path, "svc", "asr", 9102)
    m = ServiceManifest.load(tmp_path / "externals" / "svc" / "service.yaml")
    assert m.service_type == "asr"
    assert m.port == 9102


def test_manifest_port_from_healthcheck(tmp_path):
    """未显式写 port 时从 healthcheck http url 推导。"""
    svc = tmp_path / "externals" / "svc"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(
        "name: svc\ntype: asr\nstart:\n  command: [x]\n"
        "healthcheck:\n  type: http\n  url: http://127.0.0.1:9456/health\n"
    )
    m = ServiceManifest.load(svc / "service.yaml")
    assert m.port == 9456


def test_manifest_invalid_type(tmp_path):
    _write_manifest(tmp_path, "svc", "bad_type", 9102)
    with pytest.raises(ManifestError, match="type"):
        ServiceManifest.load(tmp_path / "externals" / "svc" / "service.yaml")


def test_manifest_missing_port(tmp_path):
    svc = tmp_path / "externals" / "svc"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text("name: svc\ntype: asr\nstart:\n  command: [x]\n")
    with pytest.raises(ManifestError, match="端口"):
        ServiceManifest.load(svc / "service.yaml")


def _write_manifest(tmp_path, name: str, stype: str, port: int) -> None:
    svc = tmp_path / "externals" / name
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(
        f"name: {name}\ntype: {stype}\nport: {port}\nstart:\n  command: [x]\n"
    )


# ------------------------------------------------------------------ test_service


class _ContractServer:
    """按契约回应的 fake 服务：/transcribe 返回 {"text"}，/synthesize 返回音频字段。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self._server = None
        self._thread: threading.Thread | None = None
        self.last_detect_body: bytes | None = None
        self.last_asr_body: bytes | None = None
        self.last_asr_rate: str | None = None
        self.last_generate_body: bytes | None = None
        self.last_vpr_body: dict | None = None  # /voiceprint 收到的 JSON（audio_base64/sample_rate）

    class _Handler(BaseHTTPRequestHandler):
        server_state = None

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            if self.path == "/transcribe":
                self.server_state.last_asr_body = body
                self.server_state.last_asr_rate = self.headers.get("X-Sample-Rate")
                payload = {"text": "测试文本"}
            elif self.path == "/synthesize":
                # 故意缺 tts 契约期望的 audio_base64/sample_rate，用于缺字段校验测试
                payload = {"text": "只回了文本"}
            elif self.path == "/generate":
                # moss-tts-nano 真实接口：multipart form，校验 text/demo_id 字段存在；
                # audio_base64 给 1600 字符（>1000 触发大字段透传）
                self.server_state.last_generate_body = body
                if b'name="text"' in body and b'name="demo_id"' in body:
                    payload = {"audio_base64": "UklGRg==" * 400, "sample_rate": 48000}
                else:
                    payload = {"error": "missing multipart fields"}
            elif self.path == "/detect":
                self.server_state.last_detect_body = body
                payload = {"faces": []}
            elif self.path == "/voiceprint":
                self.server_state.last_vpr_body = json.loads(body.decode("utf-8"))
                payload = {"embedding": [0.1, 0.2, 0.3], "dim": 3}
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def log_message(self, *a):
            pass

    def start(self) -> None:
        self._Handler.server_state = self
        self._server = HTTPServer(("127.0.0.1", self.port), self._Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture()
def contract_server():
    srv = _ContractServer(port=9202)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def test_test_service_ok(tmp_path, contract_server):
    async def run():
        _write_manifest(tmp_path, "fake", "asr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake")
        assert result["ok"] is True
        assert result["status"] == 200
        assert result["missing"] == []
        assert result["error"] is None
        assert result["type"] == "asr"
        assert result["request"]["url"] == "http://127.0.0.1:9202/transcribe"
        assert "text" in result["body"]
        # 响应时间（毫秒）由 _probe_contract 计时返回
        assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] >= 0
        # 未知服务
        with pytest.raises(ServiceError):
            await mgr.test_service("nope")

    asyncio.run(run())


def test_test_service_missing_expect_field(tmp_path, contract_server):
    """200 但缺期望字段 → 判失败并列出缺失字段。"""
    async def run():
        svc = tmp_path / "externals" / "fake"
        svc.mkdir(parents=True)
        # tts 契约期望 audio_base64+sample_rate，fake 只回 {"text"}
        (svc / "service.yaml").write_text(
            "name: fake\ntype: tts\nport: 9202\nstart:\n  command: [x]\n"
        )
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake")
        assert result["ok"] is False
        assert "audio_base64" in result["missing"]
        assert "sample_rate" in result["missing"]
        assert "期望字段" in (result["error"] or "")

    asyncio.run(run())


def test_test_service_unreachable(tmp_path):
    async def run():
        _write_manifest(tmp_path, "fake", "asr", 19998)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake")
        assert result["ok"] is False
        assert "不可达" in (result.get("error") or "")

    asyncio.run(run())


# ------------------------------------------------------------------ fr 测试图片


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "data" / "test" / "face.jpg").is_file(),
    reason="缺少 data/test/face.jpg 默认样本",
)
def test_test_service_fr_default_sample(tmp_path, contract_server):
    """不传图片时 fr 默认样本 = data/test/face.jpg，响应回 image_base64 供前端预览。"""
    async def run():
        _write_manifest(tmp_path, "fake", "fr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake")
        assert result["ok"] is True
        assert result["type"] == "fr"
        assert contract_server.last_detect_body == DEFAULT_FACE_TEST_IMAGE.read_bytes()
        assert result["image_name"] == "face.jpg"
        assert result["image_path"] == "data/test/face.jpg"
        assert result["image_base64"] == base64.b64encode(DEFAULT_FACE_TEST_IMAGE.read_bytes()).decode()

    asyncio.run(run())


def test_test_service_fr_custom_image(tmp_path, contract_server):
    """传 image_base64 → 用用户图片做样本，且不回 image_base64（前端本地已有预览）。"""
    async def run():
        _write_manifest(tmp_path, "fake", "fr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        custom = b"\xff\xd8\xff\xe0custom-face"
        result = await mgr.test_service("fake", image_base64=base64.b64encode(custom).decode())
        assert result["ok"] is True
        assert contract_server.last_detect_body == custom
        assert "image_base64" not in result

    asyncio.run(run())


def test_test_service_fr_bad_input(tmp_path, contract_server):
    """非法/过大 image_base64 → ServiceError。"""
    async def run():
        _write_manifest(tmp_path, "fake", "fr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        with pytest.raises(ServiceError, match="非法"):
            await mgr.test_service("fake", image_base64="A")
        with pytest.raises(ServiceError, match="过大"):
            huge = base64.b64encode(b"\x00" * (MAX_FACE_TEST_IMAGE_BYTES + 1)).decode()
            await mgr.test_service("fake", image_base64=huge)

    asyncio.run(run())


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "data" / "test" / "face.jpg").is_file(),
    reason="缺少 data/test/face.jpg 默认样本",
)
def test_test_info_fr_default_image(tmp_path):
    """test_info 打开对话框即带默认测试图（供前端预览），不发请求。"""
    async def run():
        _write_manifest(tmp_path, "fake", "fr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        info = await mgr.test_info("fake")
        assert info["type"] == "fr"
        assert info["image_name"] == "face.jpg"
        assert info["image_path"] == "data/test/face.jpg"
        assert info["image_base64"] == base64.b64encode(DEFAULT_FACE_TEST_IMAGE.read_bytes()).decode()
        assert "curl" in info  # 元信息照常携带

    asyncio.run(run())


# ------------------------------------------------------------------ asr 测试音频


def _mono_wav(sample_rate: int = 16000, seconds: float = 0.1) -> bytes:
    """构造 int16 mono WAV 容器（正弦波，避免纯静音被 text_filter 剪掉语义）。"""
    import io
    import math
    import wave

    buf = io.BytesIO()
    frames = 16000
    pcm = b"".join(
        (int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate))).to_bytes(2, "little", signed=True)
        for i in range(int(sample_rate * seconds))
    )
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _asr_pcm(wav_bytes: bytes, channels: int = 1) -> bytes:
    """按 _asr_sample 相同的语义剥头（取左声道）——测试断言用。"""
    import array
    import io
    import wave

    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        frames = w.readframes(w.getnframes())
    if channels > 1:
        return array.array("h", frames)[0::channels].tobytes()
    return frames


@pytest.mark.skipif(
    not DEFAULT_ASR_TEST_AUDIO.is_file(),
    reason="缺少 data/test/asr.wav 默认样本",
)
def test_test_service_asr_default_sample(tmp_path, contract_server):
    """不传音频时 asr 默认样本 = data/test/asr.wav：剥头取 mono PCM、X-Sample-Rate 用真实采样率、
    响应回 audio_base64（可播放 WAV）供前端预览。"""
    async def run():
        _write_manifest(tmp_path, "fake", "asr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake")
        assert result["ok"] is True
        assert result["type"] == "asr"
        # asr.wav 为 48kHz stereo：剥头转左声道 mono，采样率 48000
        assert contract_server.last_asr_body == _asr_pcm(DEFAULT_ASR_TEST_AUDIO.read_bytes(), channels=2)
        assert contract_server.last_asr_rate == "48000"
        assert result["audio_name"] == "asr.wav"
        assert result["audio_path"] == "data/test/asr.wav"
        assert result["sample_rate"] == 48000
        assert result["audio_base64"] == base64.b64encode(DEFAULT_ASR_TEST_AUDIO.read_bytes()).decode()
        assert "X-Sample-Rate: 48000" in result["curl"]  # curl 与真实采样率同步

    asyncio.run(run())


def test_test_service_asr_custom_audio(tmp_path, contract_server):
    """传 audio_base64（WAV 容器）→ 剥头发 PCM、采样率随文件、不回 audio_base64（前端本地已有预览）。"""
    async def run():
        _write_manifest(tmp_path, "fake", "asr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        wav = _mono_wav(sample_rate=22050)
        result = await mgr.test_service("fake", audio_base64=base64.b64encode(wav).decode())
        assert result["ok"] is True
        assert contract_server.last_asr_body == _asr_pcm(wav)
        assert contract_server.last_asr_rate == "22050"
        assert "audio_base64" not in result

    asyncio.run(run())


def test_test_service_asr_raw_pcm(tmp_path, contract_server):
    """非 WAV 容器（原始 PCM）→ 按契约默认 16kHz 直接透传。"""
    async def run():
        _write_manifest(tmp_path, "fake", "asr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        pcm = b"\x00\x00" * 8000
        result = await mgr.test_service("fake", audio_base64=base64.b64encode(pcm).decode())
        assert result["ok"] is True
        assert contract_server.last_asr_body == pcm
        assert contract_server.last_asr_rate == "16000"

    asyncio.run(run())


def test_test_service_asr_bad_input(tmp_path, contract_server):
    """非法/过大 audio_base64 → ServiceError。"""
    async def run():
        _write_manifest(tmp_path, "fake", "asr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        with pytest.raises(ServiceError, match="非法"):
            await mgr.test_service("fake", audio_base64="A")
        with pytest.raises(ServiceError, match="过大"):
            huge = base64.b64encode(b"\x00" * (MAX_ASR_TEST_AUDIO_BYTES + 1)).decode()
            await mgr.test_service("fake", audio_base64=huge)

    asyncio.run(run())


@pytest.mark.skipif(
    not DEFAULT_ASR_TEST_AUDIO.is_file(),
    reason="缺少 data/test/asr.wav 默认样本",
)
def test_test_info_asr_default_audio(tmp_path):
    """test_info 打开对话框即带默认测试音频（可播放 WAV），curl 采样率同步。"""
    async def run():
        _write_manifest(tmp_path, "fake", "asr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        info = await mgr.test_info("fake")
        assert info["type"] == "asr"
        assert info["audio_name"] == "asr.wav"
        assert info["audio_path"] == "data/test/asr.wav"
        assert info["sample_rate"] == 48000
        assert info["audio_base64"] == base64.b64encode(DEFAULT_ASR_TEST_AUDIO.read_bytes()).decode()
        assert "X-Sample-Rate: 48000" in info["curl"]

    asyncio.run(run())


# ------------------------------------------------------------------ vpr 测试音频（JSON body）


@pytest.mark.skipif(
    not DEFAULT_ASR_TEST_AUDIO.is_file(),
    reason="缺少 data/test/asr.wav 默认样本",
)
def test_test_service_vpr_default_sample(tmp_path, contract_server):
    """不传音频时 vpr 默认样本 = data/test/asr.wav：剥头取 mono PCM base64、
    JSON body 的 sample_rate 用真实采样率、curl 同步，响应回 audio_base64（可播放）供前端预览。"""
    async def run():
        _write_manifest(tmp_path, "fake", "vpr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake")
        assert result["ok"] is True
        assert result["type"] == "vpr"
        # asr.wav 为 48kHz stereo：剥头转左声道 mono PCM，sample_rate 48000
        assert contract_server.last_vpr_body is not None
        assert base64.b64decode(contract_server.last_vpr_body["audio_base64"]) == _asr_pcm(
            DEFAULT_ASR_TEST_AUDIO.read_bytes(), channels=2
        )
        assert contract_server.last_vpr_body["sample_rate"] == 48000
        assert result["audio_name"] == "asr.wav"
        assert result["audio_path"] == "data/test/asr.wav"
        assert result["sample_rate"] == 48000
        assert result["audio_base64"] == base64.b64encode(DEFAULT_ASR_TEST_AUDIO.read_bytes()).decode()
        assert '"sample_rate": 48000' in result["curl"]  # curl 与真实采样率同步

    asyncio.run(run())


def test_test_service_vpr_custom_audio(tmp_path, contract_server):
    """传 audio_base64（WAV 容器）→ 剥头发 PCM、采样率随文件、不回 audio_base64（前端本地已有预览）。"""
    async def run():
        _write_manifest(tmp_path, "fake", "vpr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        wav = _mono_wav(sample_rate=22050)
        result = await mgr.test_service("fake", audio_base64=base64.b64encode(wav).decode())
        assert result["ok"] is True
        assert base64.b64decode(contract_server.last_vpr_body["audio_base64"]) == _asr_pcm(wav)
        assert contract_server.last_vpr_body["sample_rate"] == 22050
        assert "audio_base64" not in result

    asyncio.run(run())


def test_test_service_vpr_raw_pcm(tmp_path, contract_server):
    """非 WAV 容器（原始 PCM）→ sample_rate 用契约默认 16kHz。"""
    async def run():
        _write_manifest(tmp_path, "fake", "vpr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        pcm = b"\x00\x00" * 8000
        result = await mgr.test_service("fake", audio_base64=base64.b64encode(pcm).decode())
        assert result["ok"] is True
        assert base64.b64decode(contract_server.last_vpr_body["audio_base64"]) == pcm
        assert contract_server.last_vpr_body["sample_rate"] == 16000

    asyncio.run(run())


def test_test_service_vpr_bad_input(tmp_path, contract_server):
    """非法/过大 audio_base64 → ServiceError。"""
    async def run():
        _write_manifest(tmp_path, "fake", "vpr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        with pytest.raises(ServiceError, match="非法"):
            await mgr.test_service("fake", audio_base64="A")
        with pytest.raises(ServiceError, match="过大"):
            huge = base64.b64encode(b"\x00" * (MAX_ASR_TEST_AUDIO_BYTES + 1)).decode()
            await mgr.test_service("fake", audio_base64=huge)

    asyncio.run(run())


@pytest.mark.skipif(
    not DEFAULT_ASR_TEST_AUDIO.is_file(),
    reason="缺少 data/test/asr.wav 默认样本",
)
def test_test_info_vpr_default_audio(tmp_path):
    """test_info 打开对话框即带默认测试音频（可播放 WAV），curl 采样率同步。"""
    async def run():
        _write_manifest(tmp_path, "fake", "vpr", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        info = await mgr.test_info("fake")
        assert info["type"] == "vpr"
        assert info["audio_name"] == "asr.wav"
        assert info["audio_path"] == "data/test/asr.wav"
        assert info["sample_rate"] == 48000
        assert info["audio_base64"] == base64.b64encode(DEFAULT_ASR_TEST_AUDIO.read_bytes()).decode()
        assert '"sample_rate": 48000' in info["curl"]

    asyncio.run(run())


# ------------------------------------------------------------------ tts 测试文本


def test_test_service_tts_custom_text(tmp_path, contract_server):
    """tts 传 text → multipart body 的 text 字段被覆盖，curl 同步。"""
    async def run():
        svc = tmp_path / "externals" / "fake"
        svc.mkdir(parents=True)
        (svc / "service.yaml").write_text(
            "name: fake\ntype: tts\nport: 9202\nstart:\n  command: [x]\n"
            "test:\n  path: /generate\n  form:\n    text: 默认文本\n    demo_id: demo-1\n"
        )
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake", text="自定义文本")
        assert result["ok"] is True
        # multipart body 里 text 字段为自定义值（demo_id 保留默认）
        assert b'name="text"' in contract_server.last_generate_body
        assert "自定义文本".encode() in contract_server.last_generate_body
        assert b'name="demo_id"' in contract_server.last_generate_body
        assert "demo-1".encode() in contract_server.last_generate_body
        assert "-F 'text=自定义文本'" in result["curl"]
        assert "-F 'demo_id=demo-1'" in result["curl"]

    asyncio.run(run())


def test_test_service_tts_contract_text(tmp_path, contract_server):
    """无 manifest test 覆盖时，tts 契约 JSON body 的 text 也可覆盖。"""
    async def run():
        _write_manifest(tmp_path, "fake", "tts", 9202)
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake", text="你好世界")
        assert result["ok"] is False  # fake 服务没实现 /synthesize → 404
        assert '"text": "你好世界"' in result["curl"]  # 但 curl 已同步自定义文本
        assert "你好世界".encode() in result["curl"].encode()

    asyncio.run(run())


def test_test_info_tts_default_text(tmp_path):
    """test_info 打开即带默认测试文本（manifest test 的 text）。"""
    async def run():
        svc = tmp_path / "externals" / "fake"
        svc.mkdir(parents=True)
        (svc / "service.yaml").write_text(
            "name: fake\ntype: tts\nport: 9202\nstart:\n  command: [x]\n"
            "test:\n  path: /generate\n  form:\n    text: 默认合成文本\n    demo_id: demo-2\n"
        )
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        info = await mgr.test_info("fake")
        assert info["type"] == "tts"
        assert info["text"] == "默认合成文本"
        assert "-F 'text=默认合成文本'" in info["curl"]

    asyncio.run(run())


def test_test_info_tts_voices(tmp_path):
    """test_info 枚举 voices_file（demo.jsonl）音色列表，默认 demo_id 取 manifest。"""
    async def run():
        svc = tmp_path / "externals" / "fake"
        svc.mkdir(parents=True)
        (svc / "service.yaml").write_text(
            "name: fake\ntype: tts\nport: 9202\nstart:\n  command: [x]\n"
            "test:\n  path: /generate\n  form:\n    text: 测试\n    demo_id: demo-3\n"
            "  voices_file: assets/demo.jsonl\n"
        )
        (svc / "assets").mkdir()
        (svc / "assets" / "demo.jsonl").write_text(
            '{"name": "温柔女声", "role": "a.wav"}\n{"name": "浑厚男声", "role": "b.wav"}\n'
            '{"name": "台湾腔", "role": "c.wav"}\n',
            encoding="utf-8",
        )
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        info = await mgr.test_info("fake")
        assert info["voices"] == [
            {"id": "demo-1", "name": "温柔女声"},
            {"id": "demo-2", "name": "浑厚男声"},
            {"id": "demo-3", "name": "台湾腔"},
        ]
        assert info["demo_id"] == "demo-3"  # manifest form.demo_id 优先

    asyncio.run(run())


def test_test_service_tts_custom_voice(tmp_path, contract_server):
    """tts 传 text + demo_id → multipart body 两个字段都覆盖，curl 同步。"""
    async def run():
        svc = tmp_path / "externals" / "fake"
        svc.mkdir(parents=True)
        (svc / "service.yaml").write_text(
            "name: fake\ntype: tts\nport: 9202\nstart:\n  command: [x]\n"
            "test:\n  path: /generate\n  form:\n    text: 默认文本\n    demo_id: demo-1\n"
        )
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake", text="新文本", demo_id="demo-5")
        assert result["ok"] is True
        assert "新文本".encode() in contract_server.last_generate_body
        assert b'name="demo_id"' in contract_server.last_generate_body
        assert "demo-5".encode() in contract_server.last_generate_body
        assert "-F 'text=新文本'" in result["curl"]
        assert "-F 'demo_id=demo-5'" in result["curl"]

    asyncio.run(run())


# ------------------------------------------------------------------ 真实 manifest 契约


def test_real_manifests_have_type_port():
    for dirname, expected_name, stype, port in (
        ("funasr", "funasr", "asr", 9102),
        ("moss-tts-nano", "moss-tts-nano", "tts", 9101),
        ("vpr-engine", "wespeaker-resnet34", "vpr", 9104),
    ):
        m = ServiceManifest.load(SERVICE_ROOT / "externals" / dirname / "service.yaml")
        assert m.name == expected_name, dirname
        assert m.service_type == stype, dirname
        assert m.port == port, dirname


# ------------------------------------------------------------------ test 覆盖段与 curl


def test_manifest_test_override_parsing(tmp_path):
    """service.yaml test 段解析：form/json/expect/headers。"""
    svc = tmp_path / "externals" / "svc"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(
        "name: svc\ntype: tts\nport: 9301\nstart:\n  command: [x]\n"
        "test:\n"
        "  path: /api/generate\n"
        "  method: post\n"
        "  headers: {X-Extra: '1'}\n"
        "  form: {text: '你好', demo_id: demo-1}\n"
        "  expect: [audio_base64]\n"
    )
    m = ServiceManifest.load(svc / "service.yaml")
    assert m.test is not None
    assert m.test.path == "/api/generate"
    assert m.test.method == "POST"  # 归一化大写
    assert m.test.headers == {"X-Extra": "1"}
    assert m.test.form_body == {"text": "你好", "demo_id": "demo-1"}
    assert m.test.expect == ["audio_base64"]


def test_manifest_test_override_conflict(tmp_path):
    """json 与 form 互斥。"""
    svc = tmp_path / "externals" / "svc"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(
        "name: svc\ntype: tts\nport: 9301\nstart:\n  command: [x]\n"
        "test:\n  json: {text: hi}\n  form: {text: hi}\n"
    )
    with pytest.raises(ManifestError, match="二选一"):
        ServiceManifest.load(svc / "service.yaml")


def test_resolve_test_spec_form_generates_multipart_and_curl():
    """form 覆盖：multipart body（Content-Type 覆盖契约默认）+ curl -F 参数。"""
    spec = resolve_test_spec(
        "tts", 9101, TestSpec(path="/api/generate", form_body={"text": "测试", "demo_id": "demo-1"})
    )
    assert spec["url"] == "http://127.0.0.1:9101/api/generate"
    assert spec["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    body_text = spec["body"].decode("utf-8")
    assert 'name="text"' in body_text and "测试" in body_text
    assert spec["description"] == "TTS：POST /api/generate，multipart form"
    assert spec["curl"] == "curl -sS -X POST 'http://127.0.0.1:9101/api/generate' -F 'text=测试' -F 'demo_id=demo-1'"


def test_resolve_test_spec_json_override():
    spec = resolve_test_spec("tts", 9101, TestSpec(path="/v2/synth", json_body={"text": "hi"}))
    assert spec["headers"]["Content-Type"] == "application/json"
    assert json.loads(spec["body"]) == {"text": "hi"}
    assert "-d '{\"text\": \"hi\"}'" in spec["curl"]
    assert "9101/v2/synth" in spec["curl"]


def test_resolve_test_spec_default_binary_curl():
    """无覆盖：契约内置二进制样本 → curl 占位文件 + 提示。"""
    spec = resolve_test_spec("fr", 9202, None)
    assert spec["body"] == CONTRACTS["fr"]["body"]
    assert "--data-binary @face.jpg" in spec["curl"]
    assert "替换" in spec["curl_hint"]
    assert spec["expect"] == ["faces"]


def test_test_info_no_request(tmp_path, contract_server):
    """test_info 只返回元信息（curl/请求），不发请求；test_service 用覆盖端点发请求。"""
    async def run():
        svc = tmp_path / "externals" / "fake"
        svc.mkdir(parents=True)
        (svc / "service.yaml").write_text(
            "name: fake\ntype: tts\nport: 9202\nstart:\n  command: [x]\n"
            "test:\n  path: /generate\n  form: {text: hi, demo_id: demo-1}\n"
        )
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        info = await mgr.test_info("fake")
        assert info["service"] == "fake"
        assert info["request"]["url"] == "http://127.0.0.1:9202/generate"
        assert "curl" in info and "-F 'text=hi'" in info["curl"]
        # 覆盖端点的测试请求打到 /generate（multipart 字段齐全 → 200）
        result = await mgr.test_service("fake")
        assert result["ok"] is True
        assert result["status"] == 200
        assert "audio_base64" in result["body"]

    asyncio.run(run())


def test_test_service_audio_base64_passthrough(tmp_path, contract_server):
    """大 audio_base64 完整透传（供前端播放器），展示用 body 里替换为占位。"""
    async def run():
        svc = tmp_path / "externals" / "fake"
        svc.mkdir(parents=True)
        (svc / "service.yaml").write_text(
            "name: fake\ntype: tts\nport: 9202\nstart:\n  command: [x]\n"
            "test:\n  path: /generate\n  form: {text: hi, demo_id: demo-1}\n"
        )
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake")
        assert result["ok"] is True
        full = "UklGRg==" * 400
        assert result["audio_base64"] == full  # 顶层完整透传
        assert full not in result["body"]  # 展示 body 已占位
        assert "见上方播放器" in result["body"]

    asyncio.run(run())


def test_test_service_override_missing_fields_rejected(tmp_path, contract_server):
    """multipart body 缺 demo_id → 服务端缺字段响应 → 契约校验失败（missing 非空）。"""
    async def run():
        svc = tmp_path / "externals" / "fake"
        svc.mkdir(parents=True)
        (svc / "service.yaml").write_text(
            "name: fake\ntype: tts\nport: 9202\nstart:\n  command: [x]\n"
            "test:\n  path: /generate\n  form: {text: hi}\n"  # 缺 demo_id
        )
        mgr = ExternalServiceManager(externals_dir=tmp_path / "externals", data_dir=tmp_path / "data")
        mgr.discover()
        result = await mgr.test_service("fake")
        assert result["ok"] is False
        assert result["missing"]  # audio_base64/sample_rate 缺失

    asyncio.run(run())
