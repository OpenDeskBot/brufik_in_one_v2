"""TTS 文案按句末标点分句，用于分段合成与下发。"""

from __future__ import annotations

import re

# 仅句末标点：。！？!? 及换行；逗号/分号/顿号不切分
_TTS_SENTENCE_END_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")


def split_tts_by_punctuation(text: str) -> list[str]:
    """按句末标点切分 TTS 文本，每段含末尾标点（若有）。

    逗号、分号、顿号不切分；无句末标点时返回整句单段；空文本返回 []。
    """
    s = str(text or "").strip()
    if not s:
        return []
    parts = _TTS_SENTENCE_END_RE.findall(s)
    chunks = [p.strip() for p in parts if p.strip()]
    return chunks if chunks else [s]


# 逗号/分号/顿号：句末切分后仍过长时，用这些标点再切成更短的 TTS 段，
# 让第一段更早合成完（MOSS 本地推理约 0.5–1.2s，首段越短开口越快）。
_TTS_CLAUSE_END_RE = re.compile(r"[^，,；;、]+[，,；;、]?")


def split_tts_for_latency(text: str, *, max_chars: int = 16) -> list[str]:
    """在句末标点切分基础上，把过长句子再切短以降低首段 TTS 延迟。

    ``max_chars<=0`` 时等价于 ``split_tts_by_punctuation``（保留旧行为）。
    仅在语音对话主链路使用；scene/playbook 等固定文本仍走原切分。
    """
    s = str(text or "").strip()
    if not s:
        return []
    if max_chars <= 0:
        return split_tts_by_punctuation(s)
    out: list[str] = []
    for sentence in split_tts_by_punctuation(s):
        if len(sentence) <= max_chars:
            out.append(sentence)
            continue
        parts = _TTS_CLAUSE_END_RE.findall(sentence)
        if len(parts) <= 1:
            parts = [sentence]
        buf = ""
        for part in parts:
            # 超长无标点片段硬切，保证首段不会等整句
            while len(part) > max_chars:
                if buf:
                    out.append(buf.strip())
                    buf = ""
                out.append(part[:max_chars].strip())
                part = part[max_chars:]
            if buf and len(buf) + len(part) > max_chars:
                out.append(buf.strip())
                buf = part
            else:
                buf += part
        if buf.strip():
            out.append(buf.strip())
    chunks = [c for c in out if c]
    return chunks if chunks else [s]
