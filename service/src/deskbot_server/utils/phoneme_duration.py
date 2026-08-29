"""文本 → 音素 + 时长（默认补充函数）。

部分 TTS（如外部 tts-engine）只返回音频、不返回音素时间戳。
此时用本模块把 ``text`` 拆成口型音素序列，并按权重把音频总时长
分给每个音素，输出与豆包 ``TTSSentenceEnd.phonemes`` 一致的格式::

    [
        {"phoneme": "n", "start_time": 0, "end_time": 300, "duration_ms": 300},
        {"phoneme": "i", "start_time": 300, "end_time": 900, "duration_ms": 600},
        ...
    ]

拆分规则（与 ``infrastructure/tts/doubao_words_phoneme`` 一致）：
中文按拼音拆声母/韵母（pypinyin），英文用 G2P（g2p_en）转 ARPA 再映射
到口型键；标点视为停顿 ``_``。时长按单元权重（声母 0.35 / 韵母 0.65、
元音 0.65 / 辅音 0.35、停顿 0.25）在 ``total_ms`` 内比例分配。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("deskbot-server")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EN_WORD_RE = re.compile(r"[A-Za-z]+")
_PUNCT_PAUSE = frozenset("，。！？!?、；;：:…")

_INITIALS = (
    "zh",
    "ch",
    "sh",
    "b",
    "p",
    "m",
    "f",
    "d",
    "t",
    "n",
    "l",
    "g",
    "k",
    "h",
    "r",
    "z",
    "c",
    "s",
    "j",
    "q",
    "x",
    "y",
    "w",
)

_VOWEL_PHONES = frozenset({"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY", "OW", "OY", "UH", "UW"})

# ARPAbet → 口型键（对齐 deskbot-face.json phonemes 已有中文键）
_ARPA_TO_MOUTH: dict[str, str] = {
    "AA": "a",
    "AE": "a",
    "AH": "a",
    "AO": "o",
    "AW": "a",
    "AY": "ai",
    "EH": "e",
    "ER": "e",
    "EY": "ei",
    "IH": "i",
    "IY": "i",
    "OW": "o",
    "OY": "ou",
    "UH": "u",
    "UW": "u",
    "B": "b",
    "CH": "ch",
    "D": "d",
    "DH": "d",
    "F": "f",
    "G": "g",
    "HH": "h",
    "JH": "j",
    "K": "g",
    "L": "l",
    "M": "m",
    "N": "n",
    "NG": "ng",
    "P": "b",
    "R": "r",
    "S": "s",
    "SH": "sh",
    "T": "d",
    "TH": "s",
    "V": "f",
    "W": "w",
    "Y": "y",
    "Z": "s",
    "ZH": "sh",
}

_PAUSE_ALIASES = frozenset({"sil", "sp", "spl", "spn", "sp1", "sp2", "sp3", "sp4"})

# 保证分配后的每段时长下限，避免切出 0ms 分片
_MIN_SEGMENT_MS = 15


@dataclass(frozen=True)
class TimedPhoneme:
    """单个音素及其毫秒时间轴（与豆包 ``phonemes`` 元素语义一致）。"""

    phoneme: str
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def to_api_dict(self) -> dict[str, Any]:
        """豆包风格 dict：``{"phoneme", "start_time", "end_time", "duration_ms"}``。"""
        return {
            "phoneme": self.phoneme,
            "start_time": self.start_ms,
            "end_time": self.end_ms,
            "duration_ms": self.duration_ms,
        }


def _simplify_phoneme_key(ph: str) -> str:
    """口型键：中文去末尾声调 1–5；英文 ARPAbet 去末尾重音 0/1；停顿归 ``_``。"""
    p = str(ph or "").strip()
    if not p or p == "_":
        return "_"
    low = p.lower()
    if low in _PAUSE_ALIASES:
        return "_"
    if len(p) >= 2 and p[-1] in "12345" and p[-2].isalpha():
        return p[:-1]
    if p.isascii() and p.isalpha() and len(p) >= 2 and p[-1] in "01":
        return p[:-1]
    return p


def _strip_arpa_stress(raw: str) -> str:
    s = str(raw or "").strip()
    while len(s) >= 2 and s[-1].isdigit():
        s = s[:-1]
    return s


def _map_arpa_to_mouth(raw: str) -> str:
    base = _strip_arpa_stress(str(raw or ""))
    key = _simplify_phoneme_key(base)
    if not key or key == "_":
        return "_"
    mapped = _ARPA_TO_MOUTH.get(key.upper())
    if mapped:
        return mapped
    if len(key) == 1 and key.isalpha():
        return key.lower()
    return key.lower()


def _split_pinyin_syllable(syllable: str) -> tuple[str, str]:
    """拼音音节 → (声母, 韵母)；无声母时韵母占满。"""
    s = str(syllable or "").strip().lower()
    if not s:
        return "_", "_"
    if s[0].isdigit():
        s = s[1:]
    while s and s[-1].isdigit():
        s = s[:-1]
    if not s or s in ("sil", "sp"):
        return "_", "_"
    ini = ""
    for cand in sorted(_INITIALS, key=len, reverse=True):
        if s.startswith(cand):
            ini = cand
            s = s[len(cand) :]
            break
    fin = s or ini or "_"
    if ini == fin:
        ini = ""
    return _simplify_phoneme_key(ini or "_"), _simplify_phoneme_key(fin)


def _pinyin_units_for_char(ch: str) -> list[tuple[str, float]]:
    try:
        from pypinyin import Style, pinyin
    except ImportError:
        logger.warning("[phoneme] pypinyin 未安装，中文按单字 '_' 处理")
        return [("_", 1.0)]
    rows = pinyin(ch, style=Style.TONE3, errors="ignore")
    if not rows or not rows[0] or not rows[0][0]:
        return [("_", 1.0)]
    ini, fin = _split_pinyin_syllable(rows[0][0])
    units: list[tuple[str, float]] = []
    if ini and ini != "_":
        units.append((ini, 0.35))
    if fin and fin != "_":
        units.append((fin, 0.65))
    return units or [("_", 1.0)]


_g2p: Any | None = None


def _get_g2p():
    global _g2p
    if _g2p is None:
        from g2p_en import G2p

        _g2p = G2p()
    return _g2p


def _english_units_for_word(word: str) -> list[tuple[str, float]]:
    phones: list[tuple[str, float]] = []
    try:
        raw_phones = _get_g2p()(str(word or "").lower())
    except Exception as exc:
        logger.warning("[phoneme] G2P 失败 word=%r err=%s", word, exc)
        raw_phones = []
    for raw in raw_phones:
        mouth = _map_arpa_to_mouth(str(raw))
        if mouth == "_":
            continue
        weight = 0.65 if _strip_arpa_stress(str(raw)).upper() in _VOWEL_PHONES else 0.35
        phones.append((mouth, weight))
    if phones:
        return phones
    w = str(word or "").strip()
    if w and w[0].isalpha():
        return [(_map_arpa_to_mouth(w[0]), 1.0)]
    return [("_", 1.0)]


def _weighted_units_for_text(text: str) -> list[tuple[str, float]]:
    """整段文本 → (口型音素, 权重) 序列；标点/未知字符计停顿 ``_``。"""
    units: list[tuple[str, float]] = []
    s = str(text or "")
    i = 0
    while i < len(s):
        m = _EN_WORD_RE.match(s, i)
        if m:
            units.extend(_english_units_for_word(m.group(0)))
            i = m.end()
            continue
        ch = s[i]
        i += 1
        if ch in _PUNCT_PAUSE:
            units.append(("_", 0.25))
        elif _CJK_RE.match(ch):
            units.extend(_pinyin_units_for_char(ch))
    return units


def _allocate_durations(units: list[tuple[str, float]], *, total_ms: int) -> list[TimedPhoneme]:
    """按权重把 ``total_ms`` 分给每个音素；末段吃掉舍入余量，保证合计 == ``total_ms``。"""
    if not units or total_ms <= 0:
        return []
    total_w = sum(w for _, w in units) or 1.0
    out: list[TimedPhoneme] = []
    cursor = 0
    n = len(units)
    for i, (ph, w) in enumerate(units):
        if i == n - 1:
            end = total_ms
        else:
            end = int(total_ms * (sum(x[1] for x in units[: i + 1]) / total_w))
        end = max(end, cursor + _MIN_SEGMENT_MS)
        end = min(end, total_ms)
        out.append(TimedPhoneme(phoneme=ph, start_ms=cursor, end_ms=end))
        cursor = end
    return out


def text_to_phoneme_durations(text: str, total_ms: int) -> list[TimedPhoneme]:
    """默认音素时长补充：``text`` + 音频总时长(ms) → 带时间轴的音素列表。

    供只返回音频、不返回音素时间戳的 TTS 使用。中文拆拼音（声母/韵母）、
    英文走 G2P 映射到口型键；停顿以 ``_`` 表示。总时长 <= 0 时返回空列表。
    """
    units = _weighted_units_for_text(text)
    timed = _allocate_durations(units, total_ms=total_ms)
    if not timed:
        return [TimedPhoneme("_", 0, max(0, total_ms))] if total_ms > 0 else []
    logger.debug("[phoneme] text=%r total_ms=%d units=%d", text[:40], total_ms, len(timed))
    return timed


def text_to_phoneme_duration_dicts(text: str, total_ms: int) -> list[dict[str, Any]]:
    """``text_to_phoneme_durations`` 的豆包风格 dict 版本（可直接替代 API phonemes）。"""
    return [t.to_api_dict() for t in text_to_phoneme_durations(text, total_ms)]
