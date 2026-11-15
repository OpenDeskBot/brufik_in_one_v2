"""Firmware-local expression keyframes; speech mouths are derived from PCM."""
from __future__ import annotations

from typing import Any

from deskbot_server.dao.face_asset_catalog import find_face_asset
from deskbot_server.pb.llm_plan import _scale_ms_values


def expand_local_anims(anims: list[Any] | None) -> tuple[list[dict[str, Any]], str]:
    out: list[dict[str, Any]] = []
    version = ""
    # None preserves the legacy default; [] explicitly requests audio-only output.
    for item in (["idle"] if anims is None else anims):
        name = item if isinstance(item, str) else item.get("anim", "")
        found = find_face_asset(name)
        if found is None:
            raise ValueError(f"animation {name!r} is not in the firmware catalog; save a valid face package first")
        entry, version = found
        # Saved source and HTTP bootstrap use the same content version.
        durations = list(entry["frame_ms"])
        if isinstance(item, dict) and item.get("ms"):
            durations = _scale_ms_values(durations, int(item["ms"]))
        for index, ms in enumerate(durations):
            out.append({"anim_id": entry["id"], "frame": index, "ms": ms})
    return out, version


def pcm_anim_rows(segs: list[dict[str, Any]], parallel: list[dict[str, Any] | None] | None):
    rows = []
    for i, seg in enumerate(segs):
        ms = max(1, int(seg.get("ms") or 1))
        pose = parallel[i] if parallel and i < len(parallel) else None
        rows.append({"idx": i, "chunk_ms": ms,
                     "anim": [{**pose, "ms": ms}] if pose else []})
    return rows
