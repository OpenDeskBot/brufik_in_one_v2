"""Conversation selection, device-scoped secrets and live connection switching."""

from __future__ import annotations

import json

from deskbot_server.client.flecket_live import realtime_url
from deskbot_server.config import get_config, read_secret
from deskbot_server.dao.conversation_mapper import get_conversation, set_conversation
from deskbot_server.model.realtime import (
    DEFAULT_PROMPT,
    DEFAULT_VOICE,
    FLECKET_URL,
    MOSS_VOICES,
    FlecketConfig,
    MossLiveConfig,
)


def conversation_provider(device_id: str | None) -> str:
    row = get_conversation(device_id) if device_id else None
    return row["conversation_provider"] if row and row["conversation_provider"] in {"moss-live", "flecket"} else "flecket"


def _params(row) -> dict:
    try:
        value = json.loads(row.get("conversation_param") or "{}") if row else {}
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _flecket_defaults() -> dict:
    return get_config().get("conversation", {}).get("flecket", {})


def moss_config(device_id: str) -> MossLiveConfig:
    raw = _params(get_conversation(device_id))
    return MossLiveConfig(
        api_key=str(raw.get("api_key") or read_secret("MOSS_API_KEY")),
        voice=raw.get("voice") or DEFAULT_VOICE,
        prompt=raw.get("prompt") or DEFAULT_PROMPT,
        camera=raw.get("camera") is True,
    )


def flecket_config(device_id: str) -> FlecketConfig:
    raw = _params(get_conversation(device_id)).get("flecket") or {}
    return FlecketConfig(
        api_key=str(raw.get("api_key") or read_secret("FLECKET_API_KEY")),
        url=raw.get("url") or _flecket_defaults().get("url") or FLECKET_URL,
        allow_barge_in=raw.get("allow_barge_in") is True,
        voice=raw.get("voice") or "",
        prompt=raw.get("prompt") or "",
        camera=raw.get("camera") is True,
    )


def conversation_status(device_id: str | None) -> dict:
    row = get_conversation(device_id) if device_id else None
    raw = _params(row)
    flecket = raw.get("flecket") or {}
    defaults = _flecket_defaults()
    profiles = {
        "moss-live": {
            "configured": bool(raw.get("api_key") or read_secret("MOSS_API_KEY")),
            "voice": raw.get("voice") or DEFAULT_VOICE,
            "voices": MOSS_VOICES,
            "prompt": raw.get("prompt") or DEFAULT_PROMPT,
            "camera": raw.get("camera") is True,
        },
        "flecket": {
            "configured": bool(flecket.get("api_key") or read_secret("FLECKET_API_KEY")),
            "url": flecket.get("url") or defaults.get("url") or FLECKET_URL,
            "agents": defaults.get("agents", []),
            "allow_barge_in": flecket.get("allow_barge_in") is True,
            "voice": flecket.get("voice") or "",
            "prompt": flecket.get("prompt") or "",
            "camera": flecket.get("camera") is True,
        },
    }
    provider = conversation_provider(device_id)
    current = profiles[provider]
    return {"provider": provider, **current, "profiles": profiles}


async def save_conversation(device_id: str, body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("请求需要 JSON 对象")
    if not device_id or not (row := get_conversation(device_id)):
        raise ValueError("请先选择已绑定的设备")
    provider = body.get("provider")
    if not isinstance(provider, str) or provider not in {"moss-live", "flecket"}:
        raise ValueError("请选择外部 Agent（flecket）或 MOSS Live；本地模型组合已移除")
    raw = _params(row)
    target = dict(raw.get("flecket") or {}) if provider == "flecket" else raw
    for key in ("voice", "prompt", "camera", "api_key", "url", "allow_barge_in"):
        if key not in body:
            continue
        value = body[key]
        if key in {"camera", "allow_barge_in"}:
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须为布尔值")
        elif not isinstance(value, str):
            raise ValueError(f"{key} 必须为字符串")
        if key == "voice" and provider != "flecket" and value not in MOSS_VOICES:
            raise ValueError("未知 MOSS 音色")
        if key == "voice" and provider == "flecket" and len(value) > 128:
            raise ValueError("flecket 音色名称最多 128 字符")
        if key == "url":
            if provider != "flecket":
                continue
            if not 1 <= len(value.strip()) <= 2048:
                raise ValueError("flecket 地址无效")
            value = realtime_url(value)
        if key == "prompt" and not (0 if provider == "flecket" else 1) <= len(value.strip()) <= 4000:
            raise ValueError("人设最多 4000 字，可留空使用 Agent 配置" if provider == "flecket" else "人设需要 1～4000 字")
        if key == "api_key":
            if not value.strip() or "*" in value or "•" in value:
                continue  # Blank/masked form preserves the existing credential.
            if len(value) > 512 or any(c.isspace() for c in value):
                raise ValueError("密钥格式无效")
        target[key] = value
    if provider == "moss-live" and not (raw.get("api_key") or read_secret("MOSS_API_KEY")):
        raise ValueError("请填写 MOSS Live 密钥，或设置服务端 MOSS_API_KEY")
    if provider == "flecket":
        if not (target.get("api_key") or read_secret("FLECKET_API_KEY")):
            raise ValueError("请填写 flecket API Key，或设置服务端 FLECKET_API_KEY")
        raw["flecket"] = target
    set_conversation(device_id, provider, json.dumps(raw, ensure_ascii=False))
    # A reconnect gives each upstream session one ROM connection generation.
    from deskbot_server.service.device_ws_service import DeviceWsService

    hub = DeviceWsService.instance()
    if hub:
        await hub.interrupt_current_playback(device_id, reason="conversation_switch")
        ws = hub._get_ws(device_id)
        if ws:
            await hub._close_old_connection(device_id, ws)
    return conversation_status(device_id)
