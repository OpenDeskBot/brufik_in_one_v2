from __future__ import annotations

import os

from deskbot_server.model.settings import AppSettings
from deskbot_server.service.llm_context import parse_llm_reply


def test_app_settings_from_config_defaults():
    cfg = {
        "server": {"port": 9000, "asr_chat_device_pb_only": True},
        "audio": {"input_codec": "opus"},
        "vad": {"mode": 3},
        "asr": {"text_filter": {"min_text_len": 2, "min_chinese_ratio": 0.0}},
        "llm": {"base_url": "https://example.com/v1", "model_name": "qwen-flash"},
        "tts": {"provider": "doubao", "sample_rate": 16000},
    }
    s = AppSettings.from_config(cfg)
    assert s.server.port == 9000
    assert s.server.asr_chat_device_pb_only is True
    assert s.audio.input_codec == "opus"
    assert s.vad.mode == 3


def test_app_settings_env_override_pb_only(tmp_path, monkeypatch):
    from deskbot_server.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("server:\n  asr_chat_device_pb_only: true\n")
    monkeypatch.setenv("DESKBOT_ASR_CHAT_DEVICE_PB_ONLY", "0")
    s = AppSettings.from_config(load_config(path))
    assert s.server.asr_chat_device_pb_only is False
    # Model conversion itself must not re-read environment variables.
    assert AppSettings.from_config({"server": {"asr_chat_device_pb_only": True}}).server.asr_chat_device_pb_only


def test_parse_llm_reply_json():
    raw = '{"need_reply": true, "tts": "你好", "moves": [], "anims": []}'
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["reply"] == "你好"
    assert parsed["need_reply"] is True
    assert parsed["moves"] == []
    assert parsed["anims"] == []


def test_parse_llm_reply_plain_text_fallback():
    parsed = parse_llm_reply("纯文本回复")
    assert parsed["reply"] == "纯文本回复"
    assert parsed["json_ok"] is False
