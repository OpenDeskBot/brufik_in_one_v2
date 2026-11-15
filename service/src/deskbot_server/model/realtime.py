"""Realtime session contracts shared by the upstream client and bridge."""

from __future__ import annotations

from dataclasses import dataclass, field

MOSS_URL = "wss://api.mosi.cn/v1/realtime?model=moss-live-1.0"
MOSS_VOICES = {"6c3152b1-1d4f-4e49-ade3-09f447712fec": "暖暖", "6cc93520-3207-4f7a-bca9-2d560b9c306b": "沐沐"}
DEFAULT_VOICE = next(iter(MOSS_VOICES))
DEFAULT_PROMPT = "你是小歪，温柔自然的桌面陪伴机器人。回答简洁口语化。"


@dataclass(frozen=True)
class MossLiveConfig:
    api_key: str = field(repr=False)
    voice: str = DEFAULT_VOICE
    prompt: str = DEFAULT_PROMPT
    camera: bool = False
    url: str = MOSS_URL


@dataclass(frozen=True)
class RealtimeEvent:
    action: str
    data: dict
    audio: bytes = b""


def media_identity(data: dict) -> tuple[str, str]:
    values = tuple(data.get(key) for key in ("run_id", "turn_id"))
    if any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in values):
        raise ValueError("MOSS audio requires run_id and turn_id")
    return values


FLECKET_URL = "ws://127.0.0.1:9110/v1/realtime?model=flecket"


@dataclass(frozen=True)
class FlecketConfig:
    api_key: str = field(repr=False)
    voice: str = ""  # Empty uses the voice configured by the Flecket service.
    prompt: str = ""  # Empty preserves the saved agent's instructions.
    camera: bool = False
    url: str = FLECKET_URL
    allow_barge_in: bool = False
