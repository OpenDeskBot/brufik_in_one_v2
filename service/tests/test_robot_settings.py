"""机器人设置（ASR/LLM/TTS 能力热切换）测试。

用临时 config.yaml + fake adapter 隔离，不加载真实 FunASR / 豆包 adapter。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from deskbot_server.infrastructure.llm.runtime import QWEN_LLM_BASE_URL
from deskbot_server.service.robot_capability import (
    ASR_CANDIDATES,
    LLM_CANDIDATES,
    TTS_CANDIDATES,
    CapabilityError,
    RobotCapabilityService,
)

MINIMAL_CONFIG = {
    "asr": {
        "external_url": "http://127.0.0.1:9102",
        "text_filter": {"min_text_len": 2, "min_chinese_ratio": 0.0},
    },
    "llm": {
        "protocol": "ark_responses",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model_name": "ep-test",
    },
    "tts": {"sample_rate": 16000},
}


@pytest.fixture()
def temp_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture()
def svc(temp_cfg: Path) -> RobotCapabilityService:
    return RobotCapabilityService(config_path=temp_cfg)


@pytest.fixture()
def clean_llm_env(monkeypatch):
    """隔离宿主环境的 LLM env 覆盖，保证 resolve_system_llm_config 走 config.yaml。"""
    for name in ("LLM_PROTOCOL", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "ARK_API_KEY", "VOLCENGINE_API_KEY", "DOUBAO_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY", "ASR_PROVIDER", "TTS_PROVIDER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def fake_asr_build(monkeypatch):
    """fake build_asr_adapter：记录构造，可配置抛异常。"""
    from deskbot_server.ports.asr import AsrPort

    state = {"built": [], "fail": None}

    class FakeAsr(AsrPort):
        def __init__(self, provider: str):
            self.provider = provider

        async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
            return ""

        def is_valid_text(self, text: str) -> bool:
            return bool(text)

    def build(settings):
        if state["fail"]:
            raise state["fail"]
        adapter = FakeAsr(settings.asr.provider)
        state["built"].append(adapter)
        return adapter

    monkeypatch.setattr("deskbot_server.service.robot_capability.build_asr_adapter", build)
    return state


@pytest.fixture()
def bound_asr():
    """给 AsrService 单例绑一个初始 fake，测试后复位。"""
    from deskbot_server.service.asr_service import AsrService

    class _Initial:
        async def transcribe(self, pcm_bytes: bytes, sample_rate: int) -> str:
            return "initial"

        def is_valid_text(self, text: str) -> bool:
            return True

    initial = _Initial()
    AsrService().bind(initial)
    yield initial
    AsrService().bind(_Initial())


# ---------- 1. 能力目录 ----------

def test_capability_catalogs_structure():
    assert [c.id for c in ASR_CANDIDATES] == ["funasr", "doubao"]
    # 本地模型在前，火山方舟 Ark 置底
    assert [c.id for c in LLM_CANDIDATES] == ["minicpm", "qwen", "ark"]
    assert [c.id for c in TTS_CANDIDATES] == ["moss-tts-nano", "doubao"]

    minicpm = LLM_CANDIDATES[0]
    qwen = LLM_CANDIDATES[1]
    ark = LLM_CANDIDATES[2]
    assert ark.experimental is False
    assert minicpm.experimental is True
    assert minicpm.requires_service == "llm-minicpm"
    assert qwen.requires_service == "llm-qwen"
    assert ASR_CANDIDATES[0].requires_service == "funasr"
    assert TTS_CANDIDATES[0].requires_service == "moss-tts-nano"
    for cap in (*ASR_CANDIDATES, *LLM_CANDIDATES, *TTS_CANDIDATES):
        assert cap.id and cap.name and cap.description


# ---------- 2. ASR 设备级配置 ----------

@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile

    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        reset_engine()
        init_engine(db_path)
        init_database()  # 含 _migrate_devices_schema（asr_provider 列）
        yield db_path


@pytest.fixture()
def device(temp_db):
    """绑定一台测试设备。"""
    from tests.device_bind_helpers import bind_device_online
    from deskbot_server.service.user_service import UserService

    user = UserService().register("asr-device@example.com", "password1234")
    return bind_device_online(user.id, "deskbot_asr")


def test_apply_asr_writes_device_table(svc, device, clean_llm_env):
    """ASR 为设备级：apply_asr 写 device 表，动态解析即时生效（不落 config）。"""
    from deskbot_server.dao.device_mapper import get_asr_provider
    from deskbot_server.config import load_config

    assert get_asr_provider("deskbot_asr") == "funasr"  # 默认

    svc.apply_asr("doubao", "deskbot_asr")

    assert get_asr_provider("deskbot_asr") == "doubao"
    assert "provider" not in load_config(svc._config_path).get("asr", {})  # config 无 provider

    status = svc.get_status("deskbot_asr")
    assert status["capabilities"]["asr"]["current"] == "doubao"


def test_apply_asr_requires_device(svc):
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.apply_asr("doubao", None)


def test_apply_asr_unknown_provider_rejected(svc, device):
    from deskbot_server.dao.device_mapper import get_asr_provider

    with pytest.raises(CapabilityError, match="未知的 ASR 能力"):
        svc.apply_asr("bogus", "deskbot_asr")
    assert get_asr_provider("deskbot_asr") == "funasr"


def test_clear_device_asr_override_resets_to_funasr(svc, device):
    from deskbot_server.dao.device_mapper import get_asr_param, get_asr_provider

    svc.apply_asr("doubao", "deskbot_asr")
    svc.save_device_asr_config("deskbot_asr", {"doubao": {"api_key": "dev-key"}})
    assert get_asr_provider("deskbot_asr") == "doubao"
    assert get_asr_param("deskbot_asr")["doubao"]["api_key"] == "dev-key"

    svc.clear_device_asr_override("deskbot_asr")
    assert get_asr_provider("deskbot_asr") == "funasr"
    assert get_asr_param("deskbot_asr") == {}  # asr_param 一并清空
    assert svc.get_status("deskbot_asr")["capabilities"]["asr"]["current"] == "funasr"


def test_asr_status_default_when_no_device(svc, temp_db):
    """匿名/未选择设备 → 默认 funasr。"""
    assert svc.get_status(None)["capabilities"]["asr"]["current"] == "funasr"


# ---------- 3. LLM 切换（设备级 llm_provider） ----------

def test_apply_llm_writes_device_table(svc, device, clean_llm_env):
    """LLM 为设备级：apply_llm 写 device 表 llm_provider，动态解析即时生效（不落 config）。"""
    from deskbot_server.config import load_config
    from deskbot_server.dao.device_mapper import get_llm_param, get_llm_provider

    assert get_llm_provider("deskbot_asr") == ""  # 默认未配置 → 系统默认
    before = load_config(svc._config_path)

    svc.apply_llm("qwen", "deskbot_asr")

    assert get_llm_provider("deskbot_asr") == "qwen"
    assert load_config(svc._config_path) == before  # config.yaml 不被改写
    status = svc.get_status("deskbot_asr")
    assert status["capabilities"]["llm"]["current"] == "qwen"
    assert status["capabilities"]["llm"]["effective"]["base_url"] == QWEN_LLM_BASE_URL
    assert status["capabilities"]["llm"]["device_params"]["configured"] is False

    # ark 应用后 current=ark；密钥/模型走 llm_param（配置对话框）
    svc.apply_llm("ark", "deskbot_asr")
    assert get_llm_provider("deskbot_asr") == "ark"
    assert get_llm_param("deskbot_asr") == {}


def test_apply_llm_requires_device(svc, clean_llm_env):
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.apply_llm("qwen", None)


def test_apply_llm_unknown_provider_rejected(svc, device, clean_llm_env):
    from deskbot_server.dao.device_mapper import get_llm_provider

    with pytest.raises(CapabilityError, match="未知的 LLM 能力"):
        svc.apply_llm("bogus", "deskbot_asr")
    assert get_llm_provider("deskbot_asr") == ""


def test_llm_status_system_default_when_unset(svc, device, clean_llm_env):
    """llm_provider 未配置 → 回落系统默认（MINIMAL_CONFIG = ark 云端、无密钥）并给出引导 warning。"""
    from deskbot_server.infrastructure.llm.runtime import ARK_OPENAI_BASE_URL

    llm = svc.get_status("deskbot_asr")["capabilities"]["llm"]
    assert llm["current"] == "ark"
    assert llm["effective"]["protocol"] == "ark_responses"
    assert llm["effective"]["base_url"] == ARK_OPENAI_BASE_URL
    assert llm["effective"]["api_key_set"] is False
    assert llm["device_params"]["configured"] is False
    assert llm["warning"]  # 系统默认指向云端但没有密钥的引导


def test_llm_status_ark_with_llm_param(svc, device, clean_llm_env):
    """设备 provider=ark 且 llm_param["ark"] 已配置 → effective 同源展示，warning 消失。"""
    from deskbot_server.dao.device_mapper import update_llm_param
    from deskbot_server.infrastructure.llm.runtime import ARK_OPENAI_BASE_URL

    update_llm_param(
        "deskbot_asr",
        json.dumps({"ark": {"api_key": "sk-test", "model_name": "ep-test", "base_url": ""}}),
    )
    svc.apply_llm("ark", "deskbot_asr")

    llm = svc.get_status("deskbot_asr")["capabilities"]["llm"]
    assert llm["current"] == "ark"
    assert llm["effective"]["api_key_set"] is True
    assert llm["effective"]["model_name"] == "ep-test"
    assert llm["effective"]["base_url"] == ARK_OPENAI_BASE_URL  # 空 base_url 回落内置默认
    assert llm["warning"] is None
    assert llm["device_params"]["configured"] is True


# ---------- 4. 本地 URL 判定 / key 豁免 / 流式禁用 ----------

def test_is_local_llm_url():
    from deskbot_server.infrastructure.llm.runtime import is_local_llm_url

    assert is_local_llm_url("http://127.0.0.1:9104/v1")
    assert is_local_llm_url("http://localhost:9104")
    assert is_local_llm_url("http://10.0.0.5/v1")
    assert is_local_llm_url("http://192.168.1.10/chat")
    assert not is_local_llm_url("https://ark.cn-beijing.volces.com/api/v3")
    assert not is_local_llm_url("")
    assert not is_local_llm_url(None)


def test_validate_api_key_exempts_local_url():
    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig, _validate_api_key

    _validate_api_key(
        ResolvedLlmConfig(model="m", api_key="", api_base="http://127.0.0.1:9104/v1", protocol="openai", source="system", display_name="x")
    )  # 本地：空 key 不抛

    with pytest.raises(ValueError, match="API Key"):
        _validate_api_key(
            ResolvedLlmConfig(model="m", api_key="", api_base="https://ark.cn-beijing.volces.com/api/v3", protocol="ark", source="system", display_name="x")
        )


def test_local_llm_forces_non_stream(monkeypatch):
    from deskbot_server.infrastructure.llm import runtime as llm_runtime

    captured = {"stream": 0, "plain": 0}

    def fake_stream(*args, **kwargs):
        captured["stream"] += 1
        return '{"ok":true}', None

    def fake_plain(*args, **kwargs):
        captured["plain"] += 1
        return '{"ok":true}', None

    monkeypatch.setattr(llm_runtime, "_request_chat_completion_stream", fake_stream)
    monkeypatch.setattr(llm_runtime, "_request_chat_completion", fake_plain)

    cfg = llm_runtime.ResolvedLlmConfig(
        model="minicpm5-1b", api_key="", api_base="http://127.0.0.1:9105/v1", protocol="openai", source="system", display_name="minicpm"
    )

    import asyncio

    asyncio.run(llm_runtime.chat_acompletion([{"role": "user", "content": "hi"}], config=cfg, stream=True))

    assert captured["stream"] == 0  # 本地强制非流式
    assert captured["plain"] == 1


# ---------- 5. 设备级 LLM 配置清除（回到系统默认） ----------

def test_llm_clear_device_config_back_to_system_default(svc, device, clean_llm_env):
    """clear_device_llm_override：llm_provider 置空 + llm_param 清空，回落系统默认（MINIMAL_CONFIG ark）。"""
    from deskbot_server.dao.device_mapper import get_llm_param, get_llm_provider
    from deskbot_server.infrastructure.llm.runtime import ARK_OPENAI_BASE_URL

    svc.apply_llm("qwen", "deskbot_asr")
    svc.save_device_llm_config("deskbot_asr", "ark", {"api_key": "sk-1", "model_name": "ep-1"})
    assert get_llm_provider("deskbot_asr") == "qwen"
    assert get_llm_param("deskbot_asr")["ark"]["api_key"] == "sk-1"

    svc.clear_device_llm_override("deskbot_asr")

    assert get_llm_provider("deskbot_asr") == ""  # 回到系统默认
    assert get_llm_param("deskbot_asr") == {}
    llm = svc.get_status("deskbot_asr")["capabilities"]["llm"]
    assert llm["current"] == "ark"  # MINIMAL_CONFIG llm 段 = ark_responses
    assert llm["effective"]["base_url"] == ARK_OPENAI_BASE_URL
    assert llm["device_params"]["configured"] is False


def test_clear_device_override_requires_device(svc):
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.clear_device_llm_override(None)


# ---------- 6. API 层 ----------

@pytest.fixture()
def temp_db(monkeypatch):
    import tempfile

    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        reset_engine()
        init_engine(db_path)
        init_database()
        yield db_path


def test_api_robot_settings_endpoints(temp_db):
    from tests._auth_compat import create_user
    from deskbot_server.web.app import create_app

    create_user("robot-settings@example.com", "password1234")
    app = create_app()
    client = app.test_client()

    # 未登录 → 401
    assert client.get("/api/robot-settings").status_code == 401

    client.post("/login", data={"email": "robot-settings@example.com", "password": "password1234"})

    resp = client.get("/api/robot-settings")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["capabilities"]["asr"]["current"] in ("funasr", "doubao")
    assert payload["capabilities"]["tts"]["current"] in ("moss-tts-nano", "doubao")
    assert payload["capabilities"]["llm"]["current"] in ("ark", "minicpm", "qwen", "custom")

    # 行为开关（未选设备）：GET 回落默认值，写操作 → 400
    assert payload["behavior"] == {"auto_reply": True, "follow_mode": ""}
    assert client.post("/api/robot-settings/behavior", json={"auto_reply": False}).status_code == 400

    # 非法 provider → 400
    bad = client.post("/api/robot-settings/asr", json={"provider": "bogus"})
    assert bad.status_code == 400
    assert bad.get_json()["ok"] is False

    # 未选择设备时清除覆盖 → 400
    clear = client.post("/api/robot-settings/llm/clear-device-override")
    assert clear.status_code == 400


def test_robot_settings_page_renders(temp_db):
    from tests._auth_compat import create_user
    from deskbot_server.web.app import create_app

    create_user("robot-settings-page@example.com", "password1234")
    app = create_app()
    client = app.test_client()

    assert client.get("/robot-settings").status_code == 302  # 匿名跳登录

    client.post("/login", data={"email": "robot-settings-page@example.com", "password": "password1234"})
    resp = client.get("/robot-settings")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "机器人配置" in html
    assert "语音识别 ASR" in html
    assert "大脑 LLM" in html
    assert "语音合成 TTS" in html
    assert "/api/robot-settings" in html
    assert "cap-opt-test" in html  # 候选行配置按钮（ASR/LLM/TTS）
    assert "openLlmCfg" in html    # LLM 配置对话框（字段 + 试聊）
    assert "/api/robot-settings/llm/config-info" in html
    assert "openTtsCfg" in html    # TTS 配置对话框（音色/凭证设置 + 试听）
    assert "保存配置" in html
    assert "/api/robot-settings/tts/config" in html
    assert "openAsrCfg" in html    # ASR 候选行配置按钮 + 对话框
    assert "/api/robot-settings/asr/config-info" in html
    # 顶部控制面板：自动回复 / 人脸跟随
    assert "控制面板" in html
    assert "/api/robot-settings/behavior" in html
    assert "自动回复" in html
    assert "跟随人脸" in html
    assert "跟随正脸" in html
    assert "注视人脸" in html


def test_robot_settings_behavior_writes_device(temp_db):
    """控制面板 behavior API：写 devices.auto_reply / servo_mode，关自动回复连带清跟随。"""
    from tests.device_bind_helpers import bind_device_online
    from deskbot_server.dao.device_mapper import get_auto_reply, get_camera_servo_auto_mode
    from deskbot_server.service.user_service import UserService
    from deskbot_server.web.app import create_app

    user = UserService().register("behavior-device@example.com", "password1234")
    bind_device_online(user.id, "deskbot_behavior")
    app = create_app()
    client = app.test_client()

    client.post("/login", data={"email": "behavior-device@example.com", "password": "password1234"})
    select = client.post("/app/api/devices/select", json={"device_id": "deskbot_behavior"})
    assert select.status_code == 200

    # 已选设备：非法 follow_mode → 400（参数校验分支）
    bad = client.post("/api/robot-settings/behavior", json={"auto_reply": True, "follow_mode": "bogus"})
    assert bad.status_code == 400
    assert bad.get_json()["ok"] is False

    # 只开跟随三档之一 → 落库 servo_mode
    ok = client.post("/api/robot-settings/behavior", json={"follow_mode": "gaze"})
    assert ok.status_code == 200
    assert ok.get_json()["behavior"] == {"auto_reply": True, "follow_mode": "gaze"}
    assert get_camera_servo_auto_mode("deskbot_behavior") == "gaze"

    # 关自动回复 → 连带清空跟随（与设备调试页一致）
    ok = client.post("/api/robot-settings/behavior", json={"auto_reply": False})
    assert ok.status_code == 200
    assert ok.get_json()["behavior"] == {"auto_reply": False, "follow_mode": ""}
    assert get_auto_reply("deskbot_behavior") is False
    assert get_camera_servo_auto_mode("deskbot_behavior") == ""

    # 再开自动回复 → 开跟随；点亮的模式再点一次（空串）→ 关闭；GET 回读一致
    ok = client.post("/api/robot-settings/behavior", json={"auto_reply": True, "follow_mode": "follow"})
    assert ok.get_json()["behavior"] == {"auto_reply": True, "follow_mode": "follow"}
    ok = client.post("/api/robot-settings/behavior", json={"follow_mode": ""})
    assert ok.get_json()["behavior"] == {"auto_reply": True, "follow_mode": ""}
    snap = client.get("/api/robot-settings").get_json()
    assert snap["behavior"] == {"auto_reply": True, "follow_mode": ""}


# ------------------------------------------------------------------ TTS 测试（test-info / test）

import base64
import io
import json
import re
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np


class _FakeTtsEngine:
    """极简 moss-tts-nano 模拟：POST /api/generate（multipart → stereo WAV base64）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.last_text: str | None = None
        self.last_demo_id: str | None = None
        self._server = None
        self._thread: threading.Thread | None = None

    class _Handler(BaseHTTPRequestHandler):
        engine = None  # 由 start 注入

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            fields = _parse_form(body, self.headers.get("Content-Type", ""))
            self.engine.last_text = fields.get("text", "")
            self.engine.last_demo_id = fields.get("demo_id", "")
            payload = json.dumps(
                {"audio_base64": base64.b64encode(_stereo_wav_bytes()).decode("ascii"), "sample_rate": 48000}
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


def _parse_form(body: bytes, content_type: str) -> dict[str, str]:
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


@pytest.fixture()
def fake_tts_engine():
    engine = _FakeTtsEngine(port=9205)
    engine.start()
    try:
        yield engine
    finally:
        engine.stop()


def test_tts_test_info(tmp_path, monkeypatch):
    from deskbot_server.infrastructure.tts.doubao import DEFAULT_SPEAKER

    monkeypatch.delenv("DOUBAO_TTS_SPEAKER", raising=False)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    svc = RobotCapabilityService(config_path=p)
    info = svc.tts_test_info()
    assert info["text"] == "你好，这是语音合成测试。"
    assert info["voices"], "moss-tts-nano 音色列表应从 demo.jsonl 枚举"
    assert info["voices"][0]["id"] == "demo-1"
    assert info["voices"][0]["name"]
    assert info["demo_id"] == "demo-1"  # 设备无 tts_param → 默认 demo-1
    assert info["doubao_speaker"] == DEFAULT_SPEAKER  # 空字段回落内置默认（无 env 回填）
    assert info["doubao_voices"], "豆包音色预设应从 data/doubao_tts_speakers.json 枚举"
    assert info["doubao_voices"][0]["id"]
    assert info["doubao_voices"][0]["label"]


def test_tts_test_moss_synthesize(fake_tts_engine, tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    svc = RobotCapabilityService(config_path=p)
    result = asyncio.run(
        svc.tts_test("moss-tts-nano", "你好，测试", voice_id="demo-3", overrides={"base_url": "http://127.0.0.1:9205"})
    )
    assert result["provider"] == "moss-tts-nano"
    assert result["sample_rate"] == 16000  # 48k 引擎原生 → 统一下发 16k（config tts.sample_rate）
    assert result["wav_base64"]
    assert result["pcm_total_bytes"] == 1600 * 2  # mono 100ms @ 16k（4800 样本 @48k 降采样）
    assert result["segments"] and any(s["phoneme"] for s in result["segments"])
    assert fake_tts_engine.last_text == "你好，测试"
    assert fake_tts_engine.last_demo_id == "demo-3"


def test_apply_tts_writes_device_table(svc, device, clean_llm_env):
    """TTS 为设备级：apply_tts 写 device 表，动态解析即时生效（不落 config）。"""
    from deskbot_server.dao.device_mapper import get_tts_provider
    from deskbot_server.config import load_config

    assert get_tts_provider("deskbot_asr") == "moss-tts-nano"  # 默认

    svc.apply_tts("doubao", "deskbot_asr")

    assert get_tts_provider("deskbot_asr") == "doubao"
    assert "provider" not in load_config(svc._config_path).get("tts", {})  # config 无 provider

    status = svc.get_status("deskbot_asr")
    assert status["capabilities"]["tts"]["current"] == "doubao"


def test_apply_tts_requires_device(svc):
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.apply_tts("doubao", None)


def test_apply_tts_unknown_provider_rejected(svc, device):
    from deskbot_server.dao.device_mapper import get_tts_provider

    with pytest.raises(CapabilityError, match="未知的 TTS 能力"):
        svc.apply_tts("bogus", "deskbot_asr")
    assert get_tts_provider("deskbot_asr") == "moss-tts-nano"


def test_clear_device_tts_override_resets_to_moss(svc, device):
    from deskbot_server.dao.device_mapper import get_tts_param, get_tts_provider

    svc.apply_tts("doubao", "deskbot_asr")
    svc.save_device_tts_config("deskbot_asr", {"doubao": {"api_key": "dev-key"}})
    assert get_tts_provider("deskbot_asr") == "doubao"
    assert get_tts_param("deskbot_asr")["doubao"]["api_key"] == "dev-key"

    svc.clear_device_tts_override("deskbot_asr")
    assert get_tts_provider("deskbot_asr") == "moss-tts-nano"
    assert get_tts_param("deskbot_asr") == {}  # tts_param 一并清空
    assert svc.get_status("deskbot_asr")["capabilities"]["tts"]["current"] == "moss-tts-nano"


def test_tts_status_default_when_no_device(svc, temp_db):
    """匿名/未选择设备 → 默认 moss-tts-nano。"""
    assert svc.get_status(None)["capabilities"]["tts"]["current"] == "moss-tts-nano"


def test_save_device_tts_config_writes_device_not_env(svc, device, tmp_path, monkeypatch):
    from deskbot_server.dao.device_mapper import get_tts_param

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", env_file)

    svc.save_device_tts_config(
        "deskbot_asr", {"doubao": {"api_key": "dev-key", "speaker": "zh_female_vv_uranus_bigtts", "sample_rate": "16000"}}
    )
    params = get_tts_param("deskbot_asr")
    assert params["doubao"]["api_key"] == "dev-key"
    assert params["doubao"]["sample_rate"] == 16000  # 数值字段归一为 int
    assert env_file.read_text(encoding="utf-8") == ""  # .env 不被写


def test_save_device_tts_config_masked_api_key_keeps_existing(svc, device):
    from deskbot_server.dao.device_mapper import get_tts_param

    svc.save_device_tts_config("deskbot_asr", {"doubao": {"api_key": "dev-key"}})
    svc.save_device_tts_config("deskbot_asr", {"doubao": {"api_key": "dev***key"}})  # 掩码占位
    assert get_tts_param("deskbot_asr")["doubao"]["api_key"] == "dev-key"


def test_save_device_tts_config_empty_fields_not_persisted(svc, device):
    """设备级保存只落 payload / 设备已有值：空字段不落键、不回填 .env（密钥设备自配）。"""
    from deskbot_server.dao.device_mapper import get_tts_param

    svc.save_device_tts_config("deskbot_asr", {"doubao": {"api_key": "dev-key", "speaker": ""}})
    params = get_tts_param("deskbot_asr")
    assert params["doubao"]["api_key"] == "dev-key"  # payload 优先
    assert "speaker" not in params["doubao"]  # 空字段不再回填 env → 不落键

    # 再次保存（payload 全空）→ 设备已有值保留
    svc.save_device_tts_config("deskbot_asr", {})
    params = get_tts_param("deskbot_asr")
    assert params["doubao"]["api_key"] == "dev-key"
    assert "speaker" not in params["doubao"]


def test_save_device_tts_config_empty_clears(svc, device, tmp_path, monkeypatch):
    from deskbot_server.dao.device_mapper import get_tts_param

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", env_file)

    svc.save_device_tts_config("deskbot_asr", {"doubao": {"api_key": "dev-key"}})
    assert get_tts_param("deskbot_asr")

    # 显式空字符串 / 全空 payload = 保留已有（回填链：payload > 设备 > env）
    svc.save_device_tts_config("deskbot_asr", {"doubao": {"api_key": ""}})
    assert get_tts_param("deskbot_asr")["doubao"]["api_key"] == "dev-key"
    svc.save_device_tts_config("deskbot_asr", {})
    assert get_tts_param("deskbot_asr")["doubao"]["api_key"] == "dev-key"


def test_save_device_tts_config_no_device_raises(svc):
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.save_device_tts_config(None, {"doubao": {"api_key": "k"}})


def test_tts_config_info_device_param_wins_and_masks(svc, device):
    from deskbot_server.infrastructure.tts.doubao import DEFAULT_RESOURCE_ID, _mask_secret

    info = svc.tts_config_info("deskbot_asr")
    assert info["doubao"]["api_key"] == ""  # 设备未配置 key → 空（无 env 回填）
    assert info["doubao"]["resource_id"] == DEFAULT_RESOURCE_ID  # 非密钥字段回落内置默认

    svc.save_device_tts_config("deskbot_asr", {"doubao": {"api_key": "dev-key", "resource_id": "my-res-1"}})
    info = svc.tts_config_info("deskbot_asr")
    assert info["doubao"]["api_key"] == _mask_secret("dev-key")  # 掩码回填密码框
    assert info["doubao"]["resource_id"] == "my-res-1"  # 设备值优先于内置默认


def test_resolve_tts_adapter_device_params_win_over_env(svc, device, monkeypatch):
    """运行期解析：设备 tts_param 的 api_key / sample_rate 优先于 env。"""
    from deskbot_server.infrastructure.tts.doubao import load_doubao_tts_config
    from deskbot_server.infrastructure.tts.doubao_phoneme import DoubaoPhonemeTtsAdapter
    from deskbot_server.infrastructure.tts.resolve import resolve_tts_adapter

    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "env-key")
    svc.apply_tts("doubao", "deskbot_asr")
    svc.save_device_tts_config("deskbot_asr", {"doubao": {"api_key": "dev-key", "sample_rate": "16000"}})

    adapter = resolve_tts_adapter("deskbot_asr")
    assert isinstance(adapter, DoubaoPhonemeTtsAdapter)
    cfg = load_doubao_tts_config(None, adapter._overrides)
    assert cfg.api_key == "dev-key"
    assert cfg.sample_rate == 16000


def test_tts_test_invalid_provider(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    svc = RobotCapabilityService(config_path=p)
    with pytest.raises(CapabilityError):
        asyncio.run(svc.tts_test("bogus", "你好"))


def test_tts_test_empty_text(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    svc = RobotCapabilityService(config_path=p)
    with pytest.raises(CapabilityError, match="请输入测试文本"):
        asyncio.run(svc.tts_test("moss-tts-nano", "   "))


# ---------- 人脸识别（none / insightface） ----------


def _face_svc(tmp_path, monkeypatch):
    """隔离单例：fake configure（真实 configure 已在主服务运行验证），只验证 config 落盘与状态。"""
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.CameraFaceService.configure",
        lambda self, runtime: None,
    )
    return RobotCapabilityService(config_path=p)


def test_face_status_default_insightface(tmp_path, monkeypatch):
    svc = _face_svc(tmp_path, monkeypatch)
    status = svc.get_status(None)
    face = status["capabilities"]["face"]
    assert face["current"] == "insightface"
    assert [c["id"] for c in face["candidates"]] == ["none", "insightface"]


def test_apply_face_none_writes_mode(tmp_path, monkeypatch):
    svc = _face_svc(tmp_path, monkeypatch)
    status = svc.apply_face("none")
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["camera_face"]["mode"] == "none"
    assert status["capabilities"]["face"]["current"] == "none"


def test_apply_face_insightface_switches_back(tmp_path, monkeypatch):
    svc = _face_svc(tmp_path, monkeypatch)
    svc.apply_face("none")
    status = svc.apply_face("insightface")
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["camera_face"]["mode"] == "insightface"
    assert status["capabilities"]["face"]["current"] == "insightface"


def test_apply_face_idempotent_same_mode(tmp_path, monkeypatch):
    svc = _face_svc(tmp_path, monkeypatch)
    status = svc.apply_face("insightface")
    assert status["capabilities"]["face"]["current"] == "insightface"


def test_apply_face_unknown_mode_rejected(tmp_path, monkeypatch):
    from deskbot_server.service.robot_capability import CapabilityError

    svc = _face_svc(tmp_path, monkeypatch)
    try:
        svc.apply_face("bogus")
    except CapabilityError as exc:
        assert "未知的 FACE 能力" in str(exc)
    else:
        raise AssertionError("expected CapabilityError")
