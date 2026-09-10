"""各设备最近一帧人脸快照（进程内，供 LLM 识别上下文）。

注册人名优先用快照里的 ``embedding``；调试页也可附带 ``jpeg_base64`` + ``landmarks`` 重算。

快照另记录该帧人脸识别耗时（``detect_ms``，外部引擎 /detect 往返毫秒数），
供实验台「视觉」气泡展示本轮图像识别耗时——与快照同刻读取即与该轮 prompt 同源。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from deskbot_server.vision.face_identity import (
    compute_face_descriptor,
    is_embedding_vector,
)

_lock = threading.Lock()
_snapshots: dict[str, dict[int, dict[str, Any]]] = {}
# 最近一次人脸检测**完成**时刻（wall-clock）：含「无人脸帧」——检测过即打点，
# 供按时间窗判断「相机画面里现在是否有人」（无时间戳会把几分钟前的旧脸算成在场）
_detect_ts: dict[str, float] = {}
# 最近一次检测的识别耗时（外部引擎 /detect 往返，整数 ms，含无人脸帧）
_detect_ms: dict[str, int] = {}


def update_device_faces(
    device_id: str, faces: list[dict[str, Any]], *, detect_ms: int | None = None
) -> None:
    device_id = str(device_id or "").strip()
    if not device_id:
        return
    by_id: dict[int, dict[str, Any]] = {}
    for face in faces or []:
        if not isinstance(face, dict):
            continue
        fid = face.get("face_id")
        if fid is None:
            continue
        by_id[int(fid)] = dict(face)
    try:
        ms = int(detect_ms) if detect_ms is not None else None
    except (TypeError, ValueError):
        ms = None
    with _lock:
        _snapshots[device_id] = by_id
        _detect_ts[device_id] = time.time()
        if ms is not None:
            _detect_ms[device_id] = max(0, ms)


def face_snapshot_ts(device_id: str) -> float | None:
    """最近一次人脸检测完成时刻（wall-clock）；从未检测过返回 None。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return None
    with _lock:
        ts = _detect_ts.get(device_id)
    return ts if ts is not None else None


def face_snapshot_detect_ms(device_id: str) -> int | None:
    """最近一次人脸检测耗时（ms）；从未检测过返回 None。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return None
    with _lock:
        ms = _detect_ms.get(device_id)
    return ms if ms is not None else None


def list_device_faces(device_id: str) -> dict[int, dict[str, Any]]:
    """返回设备最近一帧各 ``face_id`` 的快照（进程内缓存）。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return {}
    with _lock:
        mem = _snapshots.get(device_id)
    if not mem:
        return {}
    return {int(k): dict(v) for k, v in mem.items()}


def clear_device(device_id: str) -> None:
    """删除设备快照与检测时间戳（设备清除数据时调用）。

    ``_detect_ts`` 是「画面里现在是否有人」的判据，必须一并清除——否则会拿着
    刚被删掉的档案去判断在场，让机器人对陌生人打招呼。
    """
    device_id = str(device_id or "").strip()
    if not device_id:
        return
    with _lock:
        _snapshots.pop(device_id, None)
        _detect_ts.pop(device_id, None)
        _detect_ms.pop(device_id, None)


def list_recognized_faces(device_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """已匹配到姓名的人脸，按置信度降序，去重后最多 ``limit`` 条。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return []
    cap = max(1, min(int(limit), 20))
    faces = list_device_faces(device_id)
    best_by_key: dict[str, dict[str, Any]] = {}
    for fid, face in faces.items():
        if not isinstance(face, dict):
            continue
        name = str(face.get("person_name") or "").strip()
        if not name:
            continue
        profile_id = face.get("id")
        dedupe_key = f"p:{int(profile_id)}" if profile_id is not None else f"n:{name}"
        score_raw = face.get("identity_score")
        try:
            score = round(float(score_raw), 3) if score_raw is not None else None
        except (TypeError, ValueError):
            score = None
        row = {"person_name": name, "identity_score": score, "face_id": int(fid)}
        if profile_id is not None:
            try:
                row["id"] = int(profile_id)
            except (TypeError, ValueError):
                pass
        prev = best_by_key.get(dedupe_key)
        prev_score = prev.get("identity_score") if prev else None
        if prev is None or (score or 0.0) > (prev_score or 0.0):
            best_by_key[dedupe_key] = row
    ranked = sorted(
        best_by_key.values(), key=lambda r: (-(r.get("identity_score") or 0.0), str(r.get("person_name") or ""))
    )
    return ranked[:cap]


def resolve_descriptor_from_payload(payload: dict[str, Any]) -> list[float] | None:
    """从注册请求提取 descriptor：优先 payload 向量（embedding 或几何），否则 landmarks 几何特征。

    主服务不推理（外部服务 /detect 已算好 embedding），jpeg 现场算分支已移除——
    注册 payload 来自快照（自带 embedding）；无向量时用 landmarks 几何特征兜底。
    """
    landmarks = payload.get("landmarks") if isinstance(payload.get("landmarks"), list) else []
    for key in ("embedding", "face_descriptor", "descriptor"):
        raw_desc = payload.get(key)
        if isinstance(raw_desc, list) and len(raw_desc) >= 4:
            try:
                vec = [float(x) for x in raw_desc]
                if is_embedding_vector(vec) or len(vec) >= 4:
                    return vec
            except (TypeError, ValueError):
                pass
    if landmarks:
        return compute_face_descriptor(landmarks)
    return None
