"""场景编排持久化（``data/scene_playbooks.json``，顶层数组）。

每条编排由 ``chunks[]`` 组成，与 PB 协议一致：每包（``pb_single`` / 一轮 ``pb_chunk``）
可独立携带口播、表情、舵机，按顺序串行下发。
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from typing import Any

from deskbot_server.constants import SCENE_PLAYBOOKS_FILE
from deskbot_server.utils.device_data import resolve_json_path

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$", re.I)
_TEMP_POSE_PRESET_RE = re.compile(r"^pose_x-?\d+_y-?\d+$", re.I)
_CLIP_MS_MIN = 40
_CLIP_MS_MAX = 120_000


def _new_clip_id() -> str:
    return uuid.uuid4().hex[:10]


def _normalize_ms(raw: object, *, default: int = 500) -> int:
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        ms = default
    return max(_CLIP_MS_MIN, min(_CLIP_MS_MAX, ms))


def _normalize_expr_part(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    scene = str(raw.get("scene") or raw.get("name") or "").strip()
    if not scene:
        return None
    return {"scene": scene, "ms": _normalize_ms(raw.get("ms"))}


def _normalize_servo_part(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    preset = str(raw.get("preset") or "").strip()
    ms = _normalize_ms(raw.get("ms"))
    if preset:
        # 早期录制器写出的 pose_x90_y88 只是临时 id，从未进入 servo.json。
        # 它们既不是可维护的预设，也不应被猜测还原成真机动作，加载时直接丢弃。
        if _TEMP_POSE_PRESET_RE.fullmatch(preset):
            return None
        return {"preset": preset, "ms": ms}
    if raw.get("x") is not None or raw.get("y") is not None:
        try:
            return {
                "x": int(raw.get("x", 90)),
                "y": int(raw.get("y", 90)),
                "xm": int(raw.get("xm", 0)),
                "ym": int(raw.get("ym", 0)),
                "ms": ms,
            }
        except (TypeError, ValueError) as exc:
            raise ValueError("servo needs preset or x/y") from exc
    return None


def _normalize_chunk(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        raise ValueError("chunk must be an object")
    cid = str(raw.get("id") or _new_clip_id()).strip() or _new_clip_id()
    text = str(raw.get("text") or "").strip()
    expr = _normalize_expr_part(raw.get("expr"))
    servo = _normalize_servo_part(raw.get("servo"))
    if not text and not expr and not servo:
        servo_raw = raw.get("servo")
        if isinstance(servo_raw, dict) and _TEMP_POSE_PRESET_RE.fullmatch(
            str(servo_raw.get("preset") or "").strip()
        ):
            return None
        raise ValueError("chunk needs text, expr or servo")
    out: dict[str, Any] = {"id": cid, "text": text}
    if expr:
        out["expr"] = expr
    if servo:
        out["servo"] = servo
    return out


def _legacy_to_chunks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """旧三轨格式 → ``chunks``。

    三条旧轨道表达的是并行时间线，不能按“全部舵机→全部表情→全部文本”串行
    展开。旧数据没有显式开始时间，只能按轨道内序号对齐；数量不等时较长轨道
    继续生成后续 chunk。这比旧实现保留了同序号动作/表情/口播的并发语义。
    """
    chunks: list[dict[str, Any]] = []
    servo_track = [x for x in (raw.get("servo_track") or []) if isinstance(x, dict)]
    expr_track = [x for x in (raw.get("expr_track") or []) if isinstance(x, dict)]
    text_track = [x for x in (raw.get("text_track") or []) if isinstance(x, dict)]
    legacy_text = str(raw.get("text") or "").strip()
    count = max(len(servo_track), len(expr_track), len(text_track), 1 if legacy_text else 0)
    for index in range(count):
        servo_clip = servo_track[index] if index < len(servo_track) else None
        expr_clip = expr_track[index] if index < len(expr_track) else None
        text_clip = text_track[index] if index < len(text_track) else None
        chunk: dict[str, Any] = {"id": _new_clip_id(), "text": ""}
        if servo_clip is not None:
            chunk["id"] = str(servo_clip.get("id") or chunk["id"])
            servo = _normalize_servo_part(servo_clip)
            if servo:
                chunk["servo"] = servo
        if expr_clip is not None:
            if servo_clip is None:
                chunk["id"] = str(expr_clip.get("id") or chunk["id"])
            expr = _normalize_expr_part(expr_clip)
            if expr:
                chunk["expr"] = expr
        if text_clip is not None:
            if servo_clip is None and expr_clip is None:
                chunk["id"] = str(text_clip.get("id") or chunk["id"])
            chunk["text"] = str(text_clip.get("text") or "").strip()
        elif index == 0 and legacy_text:
            chunk["text"] = legacy_text
        if chunk["text"] or chunk.get("servo") or chunk.get("expr"):
            chunks.append(chunk)
    return chunks


def normalize_playbook(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("playbook must be an object")
    name = str(raw.get("name") or raw.get("id") or "").strip()
    if not name:
        raise ValueError("name required")
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid name {name!r}")
    title = str(raw.get("title") or name).strip()

    chunks_raw = raw.get("chunks")
    if isinstance(chunks_raw, list) and chunks_raw:
        chunks = [chunk for c in chunks_raw if (chunk := _normalize_chunk(c)) is not None]
    else:
        chunks = _legacy_to_chunks(raw)
    if not chunks:
        raise ValueError("playbook needs at least one chunk")

    return {"name": name, "title": title, "chunks": chunks}


def normalize_scene_playbooks(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("body must be a JSON array")
    return [normalize_playbook(x) for x in raw]


def _seed_default_playbooks() -> list[dict[str, Any]]:
    return [
        {
            "name": "demo_greet",
            "title": "演示问候",
            "chunks": [
                {"id": "c1", "text": "", "servo": {"preset": "look_left", "ms": 500}},
                {"id": "c2", "text": "", "servo": {"preset": "center", "ms": 500}},
                {"id": "c3", "text": "你好，很高兴见到你", "expr": {"scene": "happy", "ms": 1500}},
            ],
        }
    ]


def load_scene_playbooks_file(
    *, seed_if_missing: bool = True, device_id: str | None = None
) -> list[dict[str, Any]] | None:
    path = resolve_json_path(SCENE_PLAYBOOKS_FILE, device_id)
    if not os.path.isfile(path):
        if not seed_if_missing:
            return None
        rows = _seed_default_playbooks()
        save_scene_playbooks_file(rows, device_id=device_id)
        return rows
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return normalize_scene_playbooks(raw)


def save_scene_playbooks_file(rows: list[dict[str, Any]], *, device_id: str | None = None) -> None:
    norm = normalize_scene_playbooks(rows)
    path = resolve_json_path(SCENE_PLAYBOOKS_FILE, device_id)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(norm, f, ensure_ascii=False, indent=2)
        f.write("\n")


def find_playbook_by_name(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    want = str(name or "").strip().lower()
    if not want:
        return None
    for row in rows:
        if str(row.get("name") or "").strip().lower() == want:
            return copy.deepcopy(row)
    return None


def collect_missing_servo_presets(
    playbooks: list[dict[str, Any]] | dict[str, Any], *, device_id: str | None = None
) -> list[str]:
    """编排引用的 ``servo.preset`` 在 ``servo.json`` 中不存在时返回 id 列表。"""
    from deskbot_server.pb.llm_plan import _resolve_servo_preset_steps

    rows = playbooks if isinstance(playbooks, list) else [playbooks]
    missing: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        try:
            playbook = normalize_playbook(raw)
        except ValueError:
            continue
        for chunk in playbook.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            servo = chunk.get("servo")
            if not isinstance(servo, dict):
                continue
            preset = str(servo.get("preset") or "").strip()
            if not preset:
                continue
            if not _resolve_servo_preset_steps(preset, device_id=device_id):
                missing.add(preset)
    return sorted(missing)


def collect_missing_expression_scenes(
    playbooks: list[dict[str, Any]] | dict[str, Any], *, device_id: str | None = None
) -> list[str]:
    """编排引用的 ``expr.scene`` 在表情设计中不存在时返回 id 列表。"""
    from deskbot_server.dao.face_expr_scenes_store import load_face_expr_scenes_file

    valid = {
        str(row.get("name") or "").strip().lower()
        for row in (load_face_expr_scenes_file(seed_if_missing=True, device_id=device_id) or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    }
    rows = playbooks if isinstance(playbooks, list) else [playbooks]
    missing: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        try:
            playbook = normalize_playbook(raw)
        except ValueError:
            continue
        for chunk in playbook.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            expr = chunk.get("expr")
            if not isinstance(expr, dict):
                continue
            scene = str(expr.get("scene") or "").strip()
            if scene and scene.lower() not in valid:
                missing.add(scene)
    return sorted(missing)


def validate_scene_playbooks(
    playbooks: list[dict[str, Any]] | dict[str, Any], *, device_id: str | None = None
) -> dict[str, list[str]]:
    """返回可直接用于 API、启动检查和测试的资源引用报告。"""
    return {
        "missing_servo_presets": collect_missing_servo_presets(playbooks, device_id=device_id),
        "missing_expression_scenes": collect_missing_expression_scenes(playbooks, device_id=device_id),
    }
