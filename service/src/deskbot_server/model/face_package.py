"""Pure, bounded expression-program compiler shared by HTTP and firmware tooling."""

from __future__ import annotations

import hashlib
import json
import re

LAYERS = ("bg", "nose", "mouth", "eye_l", "eye_r", "extra")
SHAPES = (
    "none",
    "rect",
    "rect_outline",
    "circle",
    "circle_outline",
    "line",
    "ellipse",
    "ellipse_fill",
    "round_rect",
    "round_rect_outline",
    "text",
)
FIELDS = ("x", "y", "w", "h", "r", "x1", "y1", "x2", "y2")


def integer(value, lo, hi, where):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"{where}: expected integer in [{lo}, {hi}]")
    return value


def compile_document(doc):
    if not isinstance(doc, dict):
        raise ValueError("source must be a JSON object")
    scenes = doc.get("emotions")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("emotions must be a nonempty list")
    phonemes = doc.get("phonemes")
    if not isinstance(phonemes, list) or not phonemes:
        raise ValueError("phonemes must be a nonempty list")
    assets, mouth_assets, primitives, frames, blocks = [], [], [], [], {}
    names_by_kind = {"emotion": set(), "phoneme": set()}
    for kind, scene in [("emotion", x) for x in scenes] + [("phoneme", x) for x in phonemes]:
        names = names_by_kind[kind]
        if not isinstance(scene, dict):
            raise ValueError("expression must be an object")
        name = scene.get("name")
        aliases = scene.get("alias", [])
        if not isinstance(aliases, list):
            raise ValueError(f"{name}: alias must be a list")
        keys = [name] + aliases
        for key in keys:
            pattern = r"[a-z][a-z0-9_]{0,47}" if kind == "emotion" else r"[A-Za-z_üÜ][A-Za-z0-9_üÜ]{0,14}"
            if (
                not isinstance(key, str)
                or not re.fullmatch(pattern, key)
                or (kind == "phoneme" and len(key.encode("utf-8")) > 15)
            ):
                raise ValueError(f"invalid expression ID/alias: {key!r}")
            if key in names:
                raise ValueError(f"duplicate expression ID/alias: {key}")
            names.add(key)
        raw_frames = scene.get("frames")
        if not isinstance(raw_frames, list) or not 1 <= len(raw_frames) <= 64:
            raise ValueError(f"{name}: require 1..64 frames")
        start, duration = len(frames), 0
        for index, frame in enumerate(raw_frames):
            where = f"{name}.frames[{index}]"
            if not isinstance(frame, dict):
                raise ValueError(f"{where}: frame must be an object")
            ms = integer(frame.get("ms"), 40, 30000, where + ".ms")
            duration += ms
            elements = frame.get("elements")
            if not isinstance(elements, dict) or set(elements) - set(LAYERS):
                raise ValueError(f"{where}: invalid elements/layers")
            # Standalone faces explicitly replace all visible layers; no stale decorations.
            required = LAYERS[1:] if kind == "emotion" else ("mouth",)
            if any(not elements.get(k) for k in required):
                raise ValueError(f"{where}: nose/mouth/eye_l/eye_r/extra must be nonempty (use black placeholders)")
            block = []
            for layer in LAYERS:
                ps = elements.get(layer, [])
                if not isinstance(ps, list) or len(ps) > 16:
                    raise ValueError(f"{where}.{layer}: at most 16 primitives")
                for p in ps:
                    if not isinstance(p, dict):
                        raise ValueError(f"{where}: primitive must be an object")
                    if p.get("shape") not in SHAPES[1:]:
                        raise ValueError(f"{where}: unsupported firmware shape {p.get('shape')!r}")
                    allowed = {"shape", "c", "text", "size", "rw", "rh", "radius", *FIELDS}
                    if set(p) - allowed:
                        raise ValueError(f"{where}: unsupported fields {sorted(set(p) - allowed)}")
                    color = integer(p.get("c", 65535), 0, 65535, where + ".c")
                    vals = []
                    for k in FIELDS:
                        fallback = p.get({"w": "rw", "h": "rh", "r": "radius"}.get(k, ""), 0)
                        vals.append(integer(p.get(k, fallback), -1024, 1024, where + "." + k))
                    size = integer(p.get("size", 1), 1, 3, where + ".size")
                    text = p.get("text", "")
                    if not isinstance(text, str) or "\0" in text or len(text.encode()) > 128:
                        raise ValueError(f"{where}: invalid UTF-8 text (max 128 bytes)")
                    block.append((layer, p["shape"], color, *vals, size, text))
            block = tuple(block)
            if block not in blocks:
                blocks[block] = len(primitives)
                primitives.extend(block)
            frames.append((blocks[block], len(block), ms))
        # v1 contains short, one-shot expressions; looping is controlled by callers.
        if duration > 10000:
            raise ValueError(f"{name}: standalone expression exceeds 10 seconds")
        if len(frames) > 65535:
            raise ValueError("expression library exceeds the uint16 frame index limit")
        target = assets if kind == "emotion" else mouth_assets
        target.append(
            dict(
                id=name,
                aliases=aliases,
                frame_offset=start,
                frame_count=len(raw_frames),
                duration_ms=duration,
                frame_ms=[f["ms"] for f in raw_frames],
            )
        )
    if len(assets) > 128 or len(mouth_assets) > 128 or len(frames) > 1024 or len(primitives) > 4096:
        raise ValueError("expression program exceeds device table limits")
    if any(len(a["aliases"]) > 32 for a in assets + mouth_assets):
        raise ValueError("expression has too many aliases")
    if not isinstance(doc.get("lipsync", {}), dict):
        raise ValueError("lipsync must be an object")
    from deskbot_server.model.lipsync_profile import normalize_lipsync
    lipsync = normalize_lipsync(doc.get("lipsync", {}), set(names_by_kind["phoneme"]))
    idle = doc.get("idle", {"after_ms": 15000, "repeat_ms": 12000, "clips": [{"anim_id": "idle", "servo": []}]})
    if not isinstance(idle, dict) or set(idle) != {"after_ms", "repeat_ms", "clips"}:
        raise ValueError("idle requires after_ms, repeat_ms and clips")
    integer(idle["after_ms"], 1000, 3600000, "idle.after_ms")
    integer(idle["repeat_ms"], 1000, 3600000, "idle.repeat_ms")
    if not isinstance(idle["clips"], list) or not 1 <= len(idle["clips"]) <= 16:
        raise ValueError("idle requires 1..16 clips")
    for clip in idle["clips"]:
        if (
            not isinstance(clip, dict)
            or set(clip) != {"anim_id", "servo"}
            or clip["anim_id"] not in names_by_kind["emotion"]
        ):
            raise ValueError("idle clip references an unknown animation")
        if not isinstance(clip["servo"], list) or len(clip["servo"]) > 16:
            raise ValueError("idle servo requires at most 16 steps")
        duration = 0
        for step in clip["servo"]:
            if not isinstance(step, dict) or set(step) != {"xm", "ym", "x", "y", "ms"}:
                raise ValueError("invalid idle servo step")
            for axis in ("x", "y"):
                mode = integer(step[axis + "m"], 0, 2, "idle axis mode")
                integer(step[axis], -30 if mode == 1 else 0, 30 if mode == 1 else 180, "idle angle")
            duration += integer(step["ms"], 100, 3000, "idle servo duration")
        if duration > 10000:
            raise ValueError("idle motion exceeds 10 seconds")
    normalized = dict(
        schema=2, assets=assets, phonemes=mouth_assets, primitives=primitives, frames=frames, lipsync=lipsync, idle=idle
    )
    version = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:16]
    return dict(**normalized, version=version)


MAX_PACKAGE_BYTES = 256 * 1024


def encode_package(bundle):
    """Declarative drawing operations, never downloaded native executable code."""
    payload = json.dumps(
        dict(format="deskbot-face-v1", library=bundle),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=True,
    ).encode()
    if len(payload) > MAX_PACKAGE_BYTES:
        raise ValueError("face package exceeds device storage budget")
    return payload
