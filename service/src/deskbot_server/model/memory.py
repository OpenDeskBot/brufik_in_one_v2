"""File memory contracts; person IDs are independent of display names/biometrics."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MEMORY_TZ = ZoneInfo("Asia/Shanghai")
SECTIONS = ("基本信息", "爱好", "偏好", "最近关注", "任务", "项目", "健康", "情绪")
TRANSIENT_DAYS = {"最近关注": 14, "任务": 30, "项目": 30, "健康": 7, "情绪": 1}
MAX_ENTRY_CHARS = 2000
LINE_SEPARATOR = "\t# "


class MemoryVersionConflict(ValueError):
    """The caller must read the new profile before proposing another revision."""


class MemoryEpochChanged(ValueError):
    """An erase invalidated this session and all its delayed work."""


def now_iso() -> str:
    return datetime.now(MEMORY_TZ).isoformat(timespec="milliseconds")


def clean_text(value: Any, *, maximum: int = MAX_ENTRY_CHARS) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError(f"内容需要 1～{maximum} 字")
    return value.strip()


def encode_line(label: str, metadata: dict) -> str:
    # One upstream utterance must never forge another log record/section.
    visible = label.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return visible + LINE_SEPARATOR + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_line(line: str) -> tuple[str, dict] | None:
    visible, sep, raw = line.rstrip("\n").partition(LINE_SEPARATOR)
    if not sep:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("记忆记录格式无效")
    return visible, value


def event_text(visible: str) -> str:
    return visible.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
