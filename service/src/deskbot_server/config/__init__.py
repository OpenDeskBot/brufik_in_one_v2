"""配置加载：YAML + LLM system prompt 外置文件。"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from deskbot_server.utils.paths import DEFAULT_CONFIG_PATH


def load_config(path: str | None = None) -> dict[str, Any]:
    config_path = Path(path or DEFAULT_CONFIG_PATH).resolve()
    with config_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    cfg = merge_environment(cfg)
    _resolve_llm_system_prompt(cfg, config_path.parent)
    return cfg


def resolve_llm_system_prompt(llm_cfg: dict[str, Any], config_dir: str | Path) -> str:
    """从 llm 配置段解析 system prompt（文件优先于内联）。"""
    if not isinstance(llm_cfg, dict):
        return ""
    file_ref = llm_cfg.get("system_prompt_file")
    if file_ref:
        p = Path(file_ref)
        if not p.is_absolute():
            p = Path(config_dir) / p
        return p.read_text(encoding="utf-8").strip()
    raw = llm_cfg.get("system_prompt")
    return str(raw).strip() if raw else ""


def _resolve_llm_system_prompt(cfg: dict[str, Any], config_dir: Path) -> None:
    llm = cfg.get("llm")
    if not isinstance(llm, dict):
        return
    if llm.get("system_prompt_file"):
        llm["system_prompt"] = resolve_llm_system_prompt(llm, config_dir)


def save_config(cfg: dict[str, Any], path: str | Path | None = None) -> None:
    """写回 ``config.yaml``（会丢失 YAML 注释，与调试页其它保存行为一致）。"""
    validate_config(cfg)
    config_path = Path(path or DEFAULT_CONFIG_PATH).resolve()
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            {key: value for key, value in cfg.items() if not key.startswith("_")},
            f,
            allow_unicode=True,
            sort_keys=False,
        )
    if _RUNTIME_CONFIG is not None and config_path == _RUNTIME_PATH:
        configure_runtime(cfg, config_path)


def _bool(value):
    raw = str(value).strip().lower()
    if raw not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        raise ValueError("invalid boolean configuration")
    return raw in ("1", "true", "yes", "on")


ENV_FIELDS = {
    "DESKBOT_SERVER_HOST": ("server", "host", str),
    "DESKBOT_SERVER_PORT": ("server", "port", int),
    "DESKBOT_WS_PATH": ("server", "ws_path", str),
    "DESKBOT_WS_PING_INTERVAL": ("server", "ws_ping_interval", str),
    "DESKBOT_WS_PING_TIMEOUT": ("server", "ws_ping_timeout", float),
    "DESKBOT_ASR_CHAT_DEVICE_PB_ONLY": ("server", "asr_chat_device_pb_only", _bool),
    "DESKBOT_ASR_CHAT_MINIMAL_DOWNLINK": ("server", "asr_chat_minimal_device_downlink", _bool),
    "DESKBOT_SEND_FACE_INFO": ("server", "send_face_info_to_asr_chat", _bool),
    "TTS_WS_URL": ("tts", "ws_url", str),
    "TTS_SPK_ID": ("tts", "spk_id", int),
    "TTS_SAMPLE_RATE": ("tts", "sample_rate", int),
    "ASR_EXTERNAL_URL": ("asr", "external_url", str),
    "DESKBOT_WEB_PUBLIC_HOST": ("server", "web_public_host", str),
    "DESKBOT_SCHEDULER_ENABLED": ("scheduler", "enabled", _bool),
    "DESKBOT_SCHEDULER_POLL_INTERVAL": ("scheduler", "poll_interval_sec", float),
}
_RUNTIME_CONFIG = None
_RUNTIME_PATH = None


def validate_config(cfg):
    for key in ("server", "audio", "vad", "asr", "tts", "llm", "scheduler", "conversation"):
        if key in cfg and not isinstance(cfg[key], dict):
            raise ValueError(f"config.{key} must be an object")
    flecket = cfg.get("conversation", {}).get("flecket", {})
    if not isinstance(flecket, dict):
        raise ValueError("conversation.flecket must be an object")
    from deskbot_server.client.flecket_live import realtime_url

    if "url" in flecket:
        if not isinstance(flecket["url"], str):
            raise ValueError("conversation.flecket.url must be a string")
        flecket["url"] = realtime_url(flecket["url"])
    agents = flecket.get("agents", [])
    if not isinstance(agents, list):
        raise ValueError("conversation.flecket.agents must be a list")
    for agent in agents:
        if (not isinstance(agent, dict) or set(agent) != {"name", "model"}
                or not isinstance(agent["name"], str) or not agent["name"].strip()
                or not isinstance(agent["model"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", agent["model"])):
            raise ValueError("Agent requires a name and valid model ID")
    server = cfg.get("server", {})
    from deskbot_server.model.device_discovery import DeviceDiscovery

    DeviceDiscovery.from_server(server)
    port = server.get("port", 9000)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("server.port must be an integer from 1 to 65535")
    if not str(server.get("host", "0.0.0.0")).strip():
        raise ValueError("server.host is required")
    scheduler = cfg.get("scheduler", {})
    if not isinstance(scheduler.get("enabled", True), bool):
        raise ValueError("scheduler.enabled must be boolean")
    interval = scheduler.get("poll_interval_sec", 60.0)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 30:
        raise ValueError("scheduler.poll_interval_sec must be at least 30")


def configure_runtime(cfg, path=None):
    global _RUNTIME_CONFIG, _RUNTIME_PATH
    validate_config(cfg)
    _RUNTIME_CONFIG = deepcopy(cfg)
    _RUNTIME_PATH = Path(path or DEFAULT_CONFIG_PATH).resolve()


def get_config(path=None):
    if _RUNTIME_CONFIG is not None and (path is None or Path(path).resolve() == _RUNTIME_PATH):
        return deepcopy(_RUNTIME_CONFIG)
    return load_config(path)


def merge_environment(cfg):
    cfg = deepcopy(cfg)
    if cfg.get("_environment_resolved"):
        return cfg
    for name, (section, key, convert) in ENV_FIELDS.items():
        if name in os.environ:
            cfg.setdefault(section, {})[key] = convert(os.environ[name])
    if "FLECKET_URL" in os.environ:
        cfg.setdefault("conversation", {}).setdefault("flecket", {})["url"] = os.environ["FLECKET_URL"]
    if "DESKBOT_PB_RANDOM_SERVO" in os.environ:
        cfg.setdefault("tts", {}).setdefault("pb_random_servo", {})["enabled"] = _bool(
            os.environ["DESKBOT_PB_RANDOM_SERVO"]
        )
    validate_config(cfg)
    cfg["_environment_resolved"] = True
    return cfg


def read_secret(name):
    var = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
        return ""
    return str(os.environ.get(var) or "").strip()
