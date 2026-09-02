"""声纹识别能力开关测试（镜像人脸 apply_face 段）：config.yaml 落盘 + 状态 + 非法 mode。"""

from __future__ import annotations

import yaml

from deskbot_server.service.robot_capability import CapabilityError, RobotCapabilityService

MINIMAL_CONFIG = {
    "asr": {"external_url": "http://127.0.0.1:9102"},
    "llm": {"base_url": "https://example.invalid", "model_name": "ep-test"},
    "tts": {"sample_rate": 24000},
}


def _voiceprint_svc(tmp_path, monkeypatch):
    """隔离单例：fake configure（只验证 config 落盘与状态，不做引擎装配）。"""
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(MINIMAL_CONFIG, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(
        "deskbot_server.service.robot_capability.VoiceprintService.configure",
        lambda self, runtime: None,
    )
    return RobotCapabilityService(config_path=p)


def test_voiceprint_status_default_none(tmp_path, monkeypatch):
    svc = _voiceprint_svc(tmp_path, monkeypatch)
    status = svc.get_status(None)
    vp = status["capabilities"]["voiceprint"]
    assert vp["current"] == "none"
    assert [c["id"] for c in vp["candidates"]] == ["none", "vpr"]
    assert vp["warning"] is None


def test_apply_voiceprint_vpr_writes_mode(tmp_path, monkeypatch):
    svc = _voiceprint_svc(tmp_path, monkeypatch)
    status = svc.apply_voiceprint("vpr")
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["voiceprint"]["mode"] == "vpr"
    assert status["capabilities"]["voiceprint"]["current"] == "vpr"


def test_apply_voiceprint_none_switches_back(tmp_path, monkeypatch):
    svc = _voiceprint_svc(tmp_path, monkeypatch)
    svc.apply_voiceprint("vpr")
    status = svc.apply_voiceprint("none")
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["voiceprint"]["mode"] == "none"
    assert status["capabilities"]["voiceprint"]["current"] == "none"


def test_apply_voiceprint_idempotent_same_mode(tmp_path, monkeypatch):
    svc = _voiceprint_svc(tmp_path, monkeypatch)
    status = svc.apply_voiceprint("none")
    assert status["capabilities"]["voiceprint"]["current"] == "none"


def test_apply_voiceprint_unknown_mode_rejected(tmp_path, monkeypatch):
    svc = _voiceprint_svc(tmp_path, monkeypatch)
    try:
        svc.apply_voiceprint("bogus")
    except CapabilityError as exc:
        assert "未知的 VOICEPRINT 能力" in str(exc)
    else:
        raise AssertionError("expected CapabilityError")


def test_get_status_includes_voiceprint_capability(tmp_path, monkeypatch):
    svc = _voiceprint_svc(tmp_path, monkeypatch)
    status = svc.get_status(None)
    assert "voiceprint" in status["capabilities"]
    assert "face" in status["capabilities"]  # 人脸能力不受影响
