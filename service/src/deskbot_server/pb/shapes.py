"""图元规范化、默认设计素材与口型编辑分组校验。"""

from __future__ import annotations

import copy
import re
from typing import Any

from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH, scale_primitive

# pb 下行 ``action`` / ``level``（见 ``docs/esp32_pb_protocol.md``）
PB_ACTION_REPLACE = "replace"
PB_ACTION_APPEND = "append"
PB_ACTION_DEFAULT = "default"

PB_LEVEL_IDLE = 0
PB_LEVEL_TASK = 1
PB_LEVEL_EMERGENCY = 2
PB_LEVEL_DEBUG = 3

PB_DEFAULT_PRIMITIVE_COLOR = "#FFFFFF"
PB_DEFAULT_RGB565 = 0xFFFF
_HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")

# 配置源可写 ``c: "red"`` / ``color: "#F80"``；wire 统一下发 ``c``（RGB565 整数）
_NAMED_RGB888: dict[str, tuple[int, int, int]] = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "orange": (255, 136, 0),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
}


def apply_pb_dispatch_fields(frames: list[dict[str, Any]], *, action: str, level: int) -> None:
    """为 pb 链各分片写入统一的 ``action`` / ``level``。"""
    for one in frames:
        one["action"] = action
        one["level"] = int(level)


_PB_HINT_TYPES = frozenset({"pb_start", "pb_chunk", "pb_end", "pb_single"})


def attach_pb_device_hints(
    msg: dict[str, Any], *, volume: int | None = None, mic: str | None = None
) -> None:
    """为 ``pb_start`` / ``pb_chunk`` / ``pb_end`` / ``pb_single`` 附加设备侧提示。"""
    if msg.get("type") not in _PB_HINT_TYPES:
        return
    if volume is not None:
        msg["volume"] = int(volume)
    if mic is not None and str(mic).strip().lower() in ("mute", "open"):
        msg["mic"] = str(mic).strip().lower()


def apply_pb_device_hints_to_frames(
    frames: list[dict[str, Any]], *, volume: int | None = None, mic: str | None = None
) -> None:
    """为 pb 链各分片写入统一的 ``volume`` / ``mic``（仅适用类型）。"""
    for one in frames:
        attach_pb_device_hints(one, volume=volume, mic=mic)


_PAUSE_PHONEME_ALIASES = frozenset({"sil", "sp", "spl", "spn", "sp1", "sp2", "sp3", "sp4"})


def simplify_phoneme_key(ph: str) -> str:
    """口型查表键：中文去末尾声调 1–5；英文 ARPAbet 去末尾重音 0/1；停顿归 ``_``。"""
    p = str(ph or "").strip()
    if not p or p == "_":
        return "_"
    low = p.lower()
    if low in _PAUSE_PHONEME_ALIASES:
        return "_"
    if len(p) >= 2 and p[-1] in "12345" and p[-2].isalpha():
        return p[:-1]
    if p.isascii() and p.isalpha() and len(p) >= 2 and p[-1] in "01":
        return p[:-1]
    return p


def enumerate_zh_phonemes() -> list[str]:
    finals = [
        "a",
        "o",
        "e",
        "i",
        "u",
        "v",
        "ai",
        "ei",
        "ao",
        "ou",
        "an",
        "en",
        "ang",
        "eng",
        "ong",
        "er",
        "ia",
        "ie",
        "iao",
        "iu",
        "ian",
        "in",
        "iang",
        "ing",
        "iong",
        "ua",
        "uo",
        "uai",
        "ui",
        "uan",
        "un",
        "uang",
        "ve",
        "iou",
        "uei",
        "uen",
    ]
    initials = [
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
        "zh",
        "ch",
        "sh",
        "r",
        "z",
        "c",
        "s",
        "j",
        "q",
        "x",
        "y",
        "w",
    ]
    s: set[str] = {"_", "sil"}
    for f in finals:
        s.add(f)
    for ini in initials:
        s.add(ini)

    def sort_key(a: str) -> tuple[int, str]:
        if a == "_":
            return (-1, "")
        return (0, a)

    return sorted(s, key=sort_key)


def _legacy_default_mouth_rect_for_phoneme(ph: str) -> list[dict[str, Any]]:
    """128×64 模板；经 ``scale_primitive`` 映射到当前 LCD。"""
    ph = simplify_phoneme_key(ph)
    cx = 64
    y0 = 44
    closed_h = 4
    open_h = 15
    mid_h = 9
    if ph in ("_", "sil"):
        w, h = 30, closed_h
        raw = [{"shape": "rect", "x": round(cx - w / 2), "y": 52, "w": w, "h": h}]
    elif ph in ("a", "o", "e", "ai", "ei", "ao", "ou", "er", "ua", "uo", "uai", "uei"):
        w, h = 40, open_h
        raw = [{"shape": "rect", "x": round(cx - w / 2), "y": y0, "w": w, "h": h}]
    elif ph.startswith(("i", "u", "v")) or ph in ("iu", "ui", "ve"):
        w, h = 26, mid_h
        raw = [{"shape": "rect", "x": round(cx - w / 2), "y": y0, "w": w, "h": h}]
    elif len(ph) <= 2:
        w, h = 14, mid_h
        raw = [{"shape": "rect", "x": round(cx - w / 2), "y": y0, "w": w, "h": h}]
    else:
        w, h = 32, mid_h
        raw = [{"shape": "rect", "x": round(cx - w / 2), "y": y0, "w": w, "h": h}]
    return [scale_primitive(p) for p in raw]


def default_mouth_rect_for_phoneme(ph: str) -> list[dict[str, Any]]:
    return _legacy_default_mouth_rect_for_phoneme(ph)


def default_face_circles() -> dict[str, list[dict[str, Any]]]:
    legacy = {
        "nose": [{"shape": "circle", "x": 64, "y": 34, "r": 5}],
        "eye_l": [{"shape": "circle", "x": 42, "y": 26, "r": 7}],
        "eye_r": [{"shape": "circle", "x": 86, "y": 26, "r": 7}],
    }
    return {k: [scale_primitive(p) for p in v] for k, v in legacy.items()}


def _default_mouth_fallback_shape() -> dict[str, Any]:
    """Basic mouth used by the active device and home-page previews."""
    return {
        "elements": [scale_primitive({"shape": "rect", "x": 46, "y": 46, "w": 36, "h": 9})],
        "offset": {"x": 0, "y": 0},
    }


# shape 主名与别名（小写键）→ 主名；与 docs/esp32_pb_protocol.md 一致
_SHAPE_TO_CANONICAL: dict[str, str] = {
    "fill_rect": "rect",
    "fillrect": "rect",
    "draw_rect": "rect_outline",
    "drawrect": "rect_outline",
    "fill_circle": "circle",
    "fillcircle": "circle",
    "draw_circle": "circle_outline",
    "drawcircle": "circle_outline",
    "point": "pixel",
    "drawpixel": "pixel",
    "h_line": "hline",
    "drawfasthline": "hline",
    "v_line": "vline",
    "drawfastvline": "vline",
    "draw_ellipse": "ellipse",
    "drawellipse": "ellipse",
    "fill_ellipse": "ellipse_fill",
    "fillellipse": "ellipse_fill",
    "draw_triangle": "triangle",
    "drawtriangle": "triangle",
    "fill_triangle": "triangle_fill",
    "filltriangle": "triangle_fill",
    "fill_round_rect": "round_rect",
    "fillroundrect": "round_rect",
    "draw_round_rect": "round_rect_outline",
    "drawroundrect": "round_rect_outline",
    "draw_rotated_rect": "rotated_rect_outline",
    "drawrotatedrect": "rotated_rect_outline",
    "fill_rotated_rect": "rotated_rect_fill",
    "fillrotatedrect": "rotated_rect_fill",
    "drawline": "line",
}


def normalize_primitive_shape(shape: str) -> str:
    """将 ``shape`` 别名归一为协议主名（小写）。"""
    s = str(shape or "").strip().lower()
    return _SHAPE_TO_CANONICAL.get(s, s)


def rgb888_to_rgb565(r: int, g: int, b: int) -> int:
    """8-bit RGB → RGB565（16 位无符号整数）。"""
    r5 = (int(r) >> 3) & 0x1F
    g6 = (int(g) >> 2) & 0x3F
    b5 = (int(b) >> 3) & 0x1F
    return (r5 << 11) | (g6 << 5) | b5


def parse_color_to_rgb888(raw: Any) -> tuple[int, int, int] | None:
    """解析配置侧颜色：``#RGB`` / ``#RRGGBB`` / 命名色 / RGB888 整数。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        v = int(raw) & 0xFFFFFF
        return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    if low in _NAMED_RGB888:
        return _NAMED_RGB888[low]
    if s.startswith("#"):
        h = s[1:]
    elif _HEX_COLOR_RE.match(s):
        h = s
    else:
        return None
    if len(h) == 3 and all(c in "0123456789abcdefABCDEF" for c in h):
        h = "".join(ch * 2 for ch in h)
    if _HEX_COLOR_RE.match(h):
        v = int(h, 16)
        return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
    return None


def parse_color_to_rgb565(raw: Any, *, default: int = PB_DEFAULT_RGB565) -> int:
    """解析为 RGB565；无效或空则 ``default``（默认白 ``0xFFFF``）。"""
    if raw is None:
        return int(default) & 0xFFFF
    if isinstance(raw, bool):
        return int(default) & 0xFFFF
    if isinstance(raw, int):
        v = int(raw)
        if 0 <= v <= 0xFFFF:
            return v
        return rgb888_to_rgb565((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
    rgb = parse_color_to_rgb888(raw)
    if rgb is None:
        return int(default) & 0xFFFF
    return rgb888_to_rgb565(*rgb)


def normalize_primitive_color(raw: Any) -> int:
    """兼容旧测试名：等价 ``parse_color_to_rgb565``。"""
    return parse_color_to_rgb565(raw)


def normalize_primitive_for_wire(prim: dict[str, Any]) -> dict[str, Any]:
    """下发前规范化单图元：``c`` 为 RGB565 整数；移除 ``color``。"""
    out = copy.deepcopy(prim)
    raw = out.get("c")
    if raw is None:
        raw = out.get("color")
    out.pop("color", None)
    out["c"] = parse_color_to_rgb565(raw)
    return out


def apply_anim_bg_color_elements(elements: dict[str, Any], *, bg: Any = None, color: Any = None) -> dict[str, Any]:
    """将 LLM ``anims[]`` 项上的 ``bg`` / ``color`` 并入 ``elements``（配置侧字符串，wire 前转 ``c``）。"""
    out = copy.deepcopy(elements)
    if bg is not None and str(bg).strip() != "":
        out["bg"] = [{"shape": "rect", "x": 0, "y": 0, "w": FACE_LCD_WIDTH, "h": FACE_LCD_HEIGHT, "color": bg}]
    if color is not None and str(color).strip() != "":
        for layer in ("extra", "mouth", "nose", "eye_l", "eye_r", "bg"):
            prims = out.get(layer)
            if not isinstance(prims, list):
                continue
            for prim in prims:
                if not isinstance(prim, dict):
                    continue
                if str(prim.get("shape") or "").strip().lower() == "image":
                    continue
                if prim.get("c") is None and prim.get("color") is None:
                    prim["color"] = color
    return out


def normalize_elements_for_wire(elements: dict[str, Any]) -> dict[str, Any]:
    """规范化 ``anim.elements`` 各层图元列表。"""
    out: dict[str, Any] = {}
    for key, val in (elements or {}).items():
        if isinstance(val, list):
            out[key] = [
                normalize_primitive_for_wire(p)
                for p in val
                if isinstance(p, dict)
                and str(p.get("shape") or "").strip()
                and str(p.get("shape") or "").strip().lower() != "image"
            ]
        else:
            out[key] = copy.deepcopy(val)
    return out


def normalize_anim_item_for_wire(item: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(item)
    els = out.get("elements")
    if isinstance(els, dict):
        out["elements"] = normalize_elements_for_wire(els)
    return out


def normalize_anim_list_for_wire(anim: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_anim_item_for_wire(x) for x in anim if isinstance(x, dict)]


def is_mouth_phoneme_group_entry(val: Any) -> bool:
    """口型共享条（仅用于 ``mouth_by_phoneme_groups`` 数组项）：``states`` + ``elements`` + ``offset``。"""
    if not isinstance(val, dict):
        return False
    st = val.get("states")
    if not isinstance(st, list) or not st:
        return False
    if not all(isinstance(x, str) and str(x).strip() for x in st):
        return False
    return isinstance(val.get("elements"), list)
