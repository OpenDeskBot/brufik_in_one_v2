"""Exact clock questions use the host clock, avoiding generation and stale history."""
from __future__ import annotations

import datetime
import re
from zoneinfo import ZoneInfo


def local_clock_reply(text: str, *, now: datetime.datetime | None = None) -> str | None:
    compact = re.sub(r"[\s，。！？,.!?]", "", text or "")
    if not re.fullmatch(r"(?:小歪)?(?:请问)?(?:现在)?(?:是)?几点(?:钟)?(?:了|啦|呢|啊)?", compact):
        return None
    clock = now or datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    return f"现在是{clock.hour}点{clock.minute:02d}分。"
