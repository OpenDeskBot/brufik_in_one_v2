"""文本 → 逐音素表情行估算（家页 TTS 下发后的说话口型模拟，无需真实合成）。

与设备真实下发共用同一套机器：

- ``phoneme_duration.text_to_phoneme_durations``：文本 → 音素 + 时长（按估算总时长
  比例分配，与豆包 ``TTSSentenceEnd.phonemes`` 语义一致）；
- ``resolve_pb_face_bundle`` + ``phoneme_seq_to_anim_seq``：音素 → 表情行，
  内容与设备 pb wire 实际渲染的行一致（含 ``deskbot-face.json`` 逐音素整脸表达式，
  或口型表模式下 bundle 默认眼鼻 + 按音素嘴型）。

前端拿到行后按 ``ms`` 时间轴逐行播放，即可得到"音素口型"而不是伪随机开口。
"""

from __future__ import annotations

import copy
from typing import Any

from deskbot_server.pb.face_bundle import resolve_pb_face_bundle
from deskbot_server.pb.phoneme_anim import phoneme_seq_to_anim_seq
from deskbot_server.utils.phoneme_duration import text_to_phoneme_durations

# 与前端 home.html 历史估算一致（≈0.28s/字 + 首尾余量，收口在 1.8s–30s）
_MIN_TALK_MS = 1800
_MAX_TALK_MS = 30000
_PER_CHAR_MS = 280
_TAIL_MS = 900


def _estimate_total_ms(text: str) -> int:
    n = len(str(text or ""))
    return max(_MIN_TALK_MS, min(_MAX_TALK_MS, n * _PER_CHAR_MS + _TAIL_MS))


def estimate_talk_sim(
    text: str,
    *,
    tts_cfg: dict[str, Any] | None = None,
    device_id: str | None = None,
    total_ms: int | None = None,
) -> dict[str, Any]:
    """按估算时长把 ``text`` 摊成逐音素表情行，返回 ``{"total_ms", "rows"}``。

    ``rows`` 每项::

        {"idx": int, "phoneme": str, "ms": int, "elements": {...整脸元素...}}

    ``elements`` 与设备 pb wire 中该音素片的 ``anim[].elements`` 一致，可直接喂给
    ``face_preview_2c.js`` 的 ``sceneToSvg``（包成单帧 scene）。估算失败或空文本时
    返回 ``{"total_ms": 0, "rows": []}``，由前端回退到旧正弦口型。
    """
    s = str(text or "").strip()
    if not s:
        return {"total_ms": 0, "rows": []}
    total = int(total_ms or 0)
    if total <= 0:
        total = _estimate_total_ms(s)
    timed = text_to_phoneme_durations(s, total)
    if not timed:
        return {"total_ms": 0, "rows": []}
    segs = [{"phoneme": t.phoneme, "ms": max(1, t.duration_ms)} for t in timed]
    bundle = resolve_pb_face_bundle(tts_cfg, device_id=device_id)
    rows: list[dict[str, Any]] = []
    total_ms_out = 0
    for row in phoneme_seq_to_anim_seq(segs, bundle, device_id=device_id):
        anim_rows = row.get("anim") if isinstance(row.get("anim"), list) else None
        anim = anim_rows[0] if anim_rows else None
        elements = anim.get("elements") if isinstance(anim, dict) else None
        if not isinstance(elements, dict):
            continue
        chunk_ms = max(1, int(anim.get("ms") if isinstance(anim, dict) else row.get("chunk_ms") or 0))
        rows.append(
            {
                "idx": int(row.get("idx", 0)),
                "phoneme": str(anim.get("phoneme") or row.get("phoneme") or ""),
                "ms": chunk_ms,
                "elements": copy.deepcopy(elements),
            }
        )
        total_ms_out += chunk_ms
    return {"total_ms": total_ms_out, "rows": rows}
