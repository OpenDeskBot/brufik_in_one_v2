"""机器人设置页 TTS 配置（config-info / config / test）设备级测试。

覆盖：tts_param 设备级读写（save_device_tts_config / tts_config_info）、
掩码回填链（payload > 设备 > .env）、tts_test 覆盖字段优先、
resolve_tts_adapter 优先级（设备 > env > 默认）与三个 API 端点。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
import yaml

from deskbot_server.dao.device_mapper import get_tts_param, get_tts_provider
from deskbot_server.infrastructure.tts.doubao import _mask_secret
from deskbot_server.service.robot_capability import CapabilityError, RobotCapabilityService

MINIMAL_TTS_CFG = {
    "asr": {"external_url": "http://127.0.0.1:9102", "text_filter": {"min_text_len": 2, "min_chinese_ratio": 0.0}},
    "llm": {"protocol": "ark_responses", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model_name": "ep-test"},
    "tts": {"sample_rate": 48000},
}


@pytest.fixture()
def tmp_env(monkeypatch, tmp_path):
    """隔离 .env：read_env_file 读的是 tts.env_store.ENV_FILE。"""
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
        init_database()  # 含 _migrate_devices_schema（tts_provider / tts_param 列）
        yield db_path


@pytest.fixture()
def svc(tmp_path: Path) -> RobotCapabilityService:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_TTS_CFG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return RobotCapabilityService(config_path=p)


def _insert_device(device_id: str = "dev-1"):
    """建用户并绑定设备（temp_db fixture 下，先置为在线）。"""
    from tests._auth_compat import create_user
    from tests.device_bind_helpers import bind_device_online

    user = create_user(f"{device_id}@example.com", "password1234")
    bind_device_online(user.id, device_id)
    return user


# ---------- config-info ----------


def test_tts_config_info_device_param_wins_and_masks(svc, temp_db, tmp_env):
    # config-info 的 env 回填读 .env 文件（read_env_file），不读 os.environ
    tmp_env.write_text("DOUBAO_TTS_API_KEY=env-key\nDOUBAO_TTS_RESOURCE_ID=seed-tts-1.0\n", encoding="utf-8")
    _insert_device("dev-1")

    info = svc.tts_config_info("dev-1")
    assert info["doubao"]["api_key"] == _mask_secret("env-key")  # 设备未配置 → env 回填并掩码
    assert info["doubao"]["resource_id"] == "seed-tts-1.0"
    assert info["demo_id"] == "demo-1"  # moss 未配置 → 默认

    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": "dev-key", "resource_id": "seed-tts-2.0"}})
    info = svc.tts_config_info("dev-1")
    assert info["doubao"]["api_key"] == _mask_secret("dev-key")  # 设备优先
    assert info["doubao"]["resource_id"] == "seed-tts-2.0"


# ---------- save_device_tts_config ----------


def test_save_device_tts_config_writes_db_not_env(svc, temp_db, tmp_env):
    _insert_device("dev-1")

    svc.save_device_tts_config(
        "dev-1",
        {"moss": {"demo_id": "demo-3"}, "doubao": {"api_key": "key-1", "speaker": "s1", "sample_rate": "16000"}},
    )
    params = get_tts_param("dev-1")
    assert params["moss"]["demo_id"] == "demo-3"
    assert params["doubao"]["api_key"] == "key-1"
    assert params["doubao"]["sample_rate"] == 16000  # 数值字段归一为 int
    assert tmp_env.read_text(encoding="utf-8") == ""  # .env 不被写


def test_save_device_tts_config_masked_api_key_keeps_existing(svc, temp_db, tmp_env):
    _insert_device("dev-1")
    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": "dev-key", "speaker": "s1"}})
    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": _mask_secret("dev-key"), "speaker": "s2"}})
    params = get_tts_param("dev-1")
    assert params["doubao"]["api_key"] == "dev-key"  # 掩码占位 → 保留已有
    assert params["doubao"]["speaker"] == "s2"  # 非掩码字段正常更新


def test_save_device_tts_config_empty_fields_fill_from_device_then_env(svc, temp_db, tmp_env):
    tmp_env.write_text("DOUBAO_TTS_API_KEY=env-key\nDOUBAO_TTS_SPEAKER=env-speaker\n", encoding="utf-8")
    _insert_device("dev-1")

    # 首次保存：空字段从 .env 回填
    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": "dev-key"}})
    params = get_tts_param("dev-1")
    assert params["doubao"]["api_key"] == "dev-key"
    assert params["doubao"]["speaker"] == "env-speaker"

    # 二次保存：设备已有值优先于 .env
    tmp_env.write_text("DOUBAO_TTS_API_KEY=env-key\nDOUBAO_TTS_SPEAKER=env-speaker-2\n", encoding="utf-8")
    svc.save_device_tts_config("dev-1", {"doubao": {"speaker": ""}})
    params = get_tts_param("dev-1")
    assert params["doubao"]["api_key"] == "dev-key"
    assert params["doubao"]["speaker"] == "env-speaker"  # 保留设备值，不随 .env 变化


def test_save_device_tts_config_empty_payload_preserves(svc, temp_db, tmp_env):
    _insert_device("dev-1")
    # 无设备已有、无 env → 全空 → 置 NULL
    svc.save_device_tts_config("dev-1", {})
    assert get_tts_param("dev-1") == {}

    # 已有配置时全空 payload = 保留已有（回填链：payload > 设备 > env）
    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": "key-1"}})
    svc.save_device_tts_config("dev-1", {})
    assert get_tts_param("dev-1")["doubao"]["api_key"] == "key-1"

    # 显式空字符串同样保留
    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": ""}})
    assert get_tts_param("dev-1")["doubao"]["api_key"] == "key-1"


def test_save_device_tts_config_no_device_raises(svc, temp_db):
    with pytest.raises(CapabilityError, match="未选择当前设备"):
        svc.save_device_tts_config(None, {"doubao": {"api_key": "k"}})


def test_save_device_tts_config_unknown_provider_payload_ignored(svc, temp_db, tmp_env):
    _insert_device("dev-1")
    svc.save_device_tts_config("dev-1", {"bogus": {"x": "1"}})
    assert get_tts_param("dev-1") == {}  # 未知 provider 段忽略


# ---------- tts_test ----------


def test_tts_test_doubao_device_param_wins_over_env(svc, temp_db, tmp_env, monkeypatch):
    """合成测试：设备 tts_param 的 api_key 优先于 env（overrides 注入 adapter）。"""
    from deskbot_server.ports.tts import PhonemeSegment

    captured: dict = {}
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "env-key")
    _insert_device("dev-1")
    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": "dev-key", "speaker": "s1"}})

    async def fake_synth(self, text):
        captured["overrides"] = dict(self._overrides)
        return 24000, [PhonemeSegment(phoneme="a", ms=100, pcm=b"\x00" * 480)]

    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.DoubaoPhonemeTtsAdapter.synthesize_phoneme_segments", fake_synth
    )
    result = asyncio.run(svc.tts_test("doubao", "你好", device_id="dev-1"))
    assert result["provider"] == "doubao"
    assert captured["overrides"]["api_key"] == "dev-key"
    assert captured["overrides"]["speaker"] == "s1"


def test_tts_test_form_overrides_beat_device_params(svc, temp_db, tmp_env, monkeypatch):
    from deskbot_server.ports.tts import PhonemeSegment

    captured: dict = {}
    _insert_device("dev-1")
    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": "dev-key", "speaker": "s1", "resource_id": "seed-tts-2.0"}})

    async def fake_synth(self, text):
        captured["overrides"] = dict(self._overrides)
        return 24000, [PhonemeSegment(phoneme="a", ms=100, pcm=b"\x00" * 480)]

    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.DoubaoPhonemeTtsAdapter.synthesize_phoneme_segments", fake_synth
    )
    # 表单未保存的临时值：voice_id（speaker）+ resource_id 覆盖设备参数
    asyncio.run(
        svc.tts_test(
            "doubao",
            "你好",
            device_id="dev-1",
            voice_id="s2",
            overrides={"api_key": "form-key", "resource_id": "seed-tts-1.0"},
        )
    )
    assert captured["overrides"]["api_key"] == "form-key"  # 表单覆盖 > 设备
    assert captured["overrides"]["speaker"] == "s2"
    assert captured["overrides"]["resource_id"] == "seed-tts-1.0"


def test_tts_test_masked_form_api_key_falls_back_to_device(svc, temp_db, tmp_env, monkeypatch):
    """试听场景：保存后表单回填的掩码 api_key 不应挡住设备已有值（此前报「未配置 DOUBAO_TTS_API_KEY」）。"""
    from deskbot_server.ports.tts import PhonemeSegment

    captured: dict = {}
    _insert_device("dev-1")
    svc.save_device_tts_config("dev-1", {"doubao": {"api_key": "dev-key", "speaker": "s1"}})

    async def fake_synth(self, text):
        captured["overrides"] = dict(self._overrides)
        return 24000, [PhonemeSegment(phoneme="a", ms=100, pcm=b"\x00" * 480)]

    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.DoubaoPhonemeTtsAdapter.synthesize_phoneme_segments", fake_synth
    )
    # 表单携带保存后回填的掩码 api_key（前端原样提交）→ 回落设备已有值
    asyncio.run(
        svc.tts_test(
            "doubao", "你好", device_id="dev-1", voice_id="s1", overrides={"api_key": _mask_secret("dev-key")}
        )
    )
    assert captured["overrides"]["api_key"] == "dev-key"
    assert captured["overrides"]["speaker"] == "s1"


def test_tts_test_moss_uses_device_demo_id(svc, temp_db, tmp_env, monkeypatch):
    from deskbot_server.ports.tts import PhonemeSegment

    captured: dict = {}
    _insert_device("dev-1")
    svc.save_device_tts_config("dev-1", {"moss": {"demo_id": "demo-5"}})

    async def fake_synth(self, text):
        captured["demo_id"] = self.demo_id  # moss 覆盖值落在实例属性
        return 48000, [PhonemeSegment(phoneme="a", ms=100, pcm=b"\x00" * 960)]

    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.MossTtsAdapter.synthesize_phoneme_segments", fake_synth
    )
    asyncio.run(svc.tts_test("moss-tts-nano", "你好", device_id="dev-1"))
    assert captured["demo_id"] == "demo-5"


# ---------- resolve 优先级 ----------


def test_resolve_tts_provider_device_and_fallback(temp_db):
    from deskbot_server.infrastructure.tts.resolve import resolve_tts_provider

    assert resolve_tts_provider(None) == "moss-tts-nano"  # 匿名 → 默认
    assert resolve_tts_provider("no-such-device") == "moss-tts-nano"

    _insert_device("dev-1")
    set_provider("dev-1", "doubao")
    assert resolve_tts_provider("dev-1") == "doubao"
    set_provider("dev-1", "bogus")
    assert resolve_tts_provider("dev-1") == "moss-tts-nano"  # 非法回落


def set_provider(device_id: str, provider: str) -> None:
    from deskbot_server.dao.device_mapper import set_tts_provider as _set

    _set(device_id, provider)


def test_resolve_tts_adapter_doubao_device_overrides_env(temp_db, tmp_env, monkeypatch):
    from deskbot_server.infrastructure.tts.doubao import load_doubao_tts_config
    from deskbot_server.infrastructure.tts.doubao_phoneme import DoubaoPhonemeTtsAdapter
    from deskbot_server.infrastructure.tts.resolve import resolve_tts_adapter

    _insert_device("dev-1")
    set_provider("dev-1", "doubao")
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "env-key")
    save_config("dev-1", {"doubao": {"api_key": "dev-key", "sample_rate": "16000"}})

    adapter = resolve_tts_adapter("dev-1")
    assert isinstance(adapter, DoubaoPhonemeTtsAdapter)
    cfg = load_doubao_tts_config(None, adapter._overrides)
    assert cfg.api_key == "dev-key"  # 设备 > env
    assert cfg.sample_rate == 16000
    assert cfg.audio_format == "pcm"  # 未配置字段回落默认


def test_resolve_tts_adapter_doubao_env_fallback(temp_db, tmp_env, monkeypatch):
    from deskbot_server.infrastructure.tts.doubao import load_doubao_tts_config
    from deskbot_server.infrastructure.tts.resolve import resolve_tts_adapter

    _insert_device("dev-1")
    set_provider("dev-1", "doubao")
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "env-key")

    adapter = resolve_tts_adapter("dev-1")
    cfg = load_doubao_tts_config(None, adapter._overrides)
    assert cfg.api_key == "env-key"  # 设备未配置 → env


def test_resolve_tts_adapter_moss_device_demo_id(temp_db):
    from deskbot_server.infrastructure.tts.moss_adapter import MossTtsAdapter
    from deskbot_server.infrastructure.tts.resolve import resolve_tts_adapter

    _insert_device("dev-1")
    save_config("dev-1", {"moss": {"demo_id": "demo-7"}})

    adapter = resolve_tts_adapter("dev-1")
    assert isinstance(adapter, MossTtsAdapter)
    assert adapter.demo_id == "demo-7"


def test_resolve_tts_adapter_masked_param_skipped(temp_db, tmp_env, monkeypatch):
    """防呆：tts_param 中存了掩码占位 → resolve 跳过，回落 env。"""
    from deskbot_server.infrastructure.tts.doubao import load_doubao_tts_config
    from deskbot_server.infrastructure.tts.resolve import resolve_tts_adapter

    _insert_device("dev-1")
    set_provider("dev-1", "doubao")
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "env-key")
    save_config("dev-1", {"doubao": {"api_key": _mask_secret("dev-key")}})  # 掩码占位被直接入库

    adapter = resolve_tts_adapter("dev-1")
    cfg = load_doubao_tts_config(None, adapter._overrides)
    assert cfg.api_key == "env-key"  # 掩码不参与合成，回落 env


def save_config(device_id: str, payload: dict) -> None:
    svc = RobotCapabilityService(config_path=None)
    svc.save_device_tts_config(device_id, payload)


# ---------- API 端点 ----------


def _login_client(email: str):
    from deskbot_server.web.app import create_app

    client = create_app().test_client()
    client.post("/login", data={"email": email, "password": "password1234"})
    return client


def test_api_tts_config_save_endpoint(temp_db, monkeypatch, tmp_path):
    """保存 → 写当前设备 tts_param（DB），不再写 .env。"""
    import os

    env_file = tmp_path / ".env"
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", env_file)
    for name in ("DOUBAO_TTS_API_KEY", "DOUBAO_TTS_SPEAKER", "DOUBAO_TTS_RESOURCE_ID", "DOUBAO_TTS_MODEL"):
        monkeypatch.delenv(name, raising=False)

    _insert_device("brfk_tts")
    client = _login_client("brfk_tts@example.com")
    client.post("/app/api/devices/select", json={"device_id": "brfk_tts"})

    resp = client.post(
        "/api/robot-settings/tts/config",
        json={
            "moss": {"demo_id": "demo-3"},
            "doubao": {"api_key": "key-1", "speaker": "s1", "resource_id": "seed-tts-2.0"},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["demo_id"] == "demo-3"
    assert payload["doubao"]["speaker"] == "s1"
    assert payload["doubao"]["resource_id"] == "seed-tts-2.0"

    params = get_tts_param("brfk_tts")
    assert params["doubao"]["api_key"] == "key-1"  # DB 落库
    assert params["moss"]["demo_id"] == "demo-3"
    assert not env_file.exists()  # .env 未创建 = 不再写全局 env


def test_api_tts_config_save_endpoint_no_device_400(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    _insert_device("nodev_tts")
    client = _login_client("nodev_tts@example.com")  # 未选设备
    resp = client.post("/api/robot-settings/tts/config", json={"doubao": {"api_key": "k"}})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_api_tts_apply_endpoint_writes_device(temp_db, monkeypatch, tmp_path):
    from deskbot_server.web.app import create_app

    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    _insert_device("apply_tts")
    client = _login_client("apply_tts@example.com")
    client.post("/app/api/devices/select", json={"device_id": "apply_tts"})

    resp = client.post("/api/robot-settings/tts", json={"provider": "doubao"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["capabilities"]["tts"]["current"] == "doubao"
    assert get_tts_provider("apply_tts") == "doubao"

    # 未选设备 → 400
    client2 = create_app().test_client()
    client2.post("/login", data={"email": "apply_tts@example.com", "password": "password1234"})
    resp = client2.post("/api/robot-settings/tts", json={"provider": "doubao"})
    assert resp.status_code == 400


def test_api_tts_clear_override_endpoint(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    _insert_device("clear_tts")
    client = _login_client("clear_tts@example.com")
    client.post("/app/api/devices/select", json={"device_id": "clear_tts"})
    client.post("/api/robot-settings/tts", json={"provider": "doubao"})
    client.post("/api/robot-settings/tts/config", json={"doubao": {"api_key": "k"}})
    assert get_tts_provider("clear_tts") == "doubao"

    resp = client.post("/api/robot-settings/tts/clear-device-override")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["capabilities"]["tts"]["current"] == "moss-tts-nano"
    assert get_tts_provider("clear_tts") == "moss-tts-nano"
    assert get_tts_param("clear_tts") == {}


def test_api_tts_test_info_endpoint_device_aware(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr("deskbot_server.infrastructure.tts.env_store.ENV_FILE", tmp_path / ".env")
    _insert_device("info_tts")
    client = _login_client("info_tts@example.com")
    client.post("/app/api/devices/select", json={"device_id": "info_tts"})
    client.post("/api/robot-settings/tts/config", json={"doubao": {"speaker": "s1"}})

    resp = client.get("/api/robot-settings/tts/test-info")
    assert resp.status_code == 200
    assert resp.get_json()["doubao_speaker"] == "s1"  # 设备 tts_param 优先
