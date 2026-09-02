"""声纹档案业务层：匹配、注册、版本管理（镜像 face_profile_service，descriptor=WeSpeaker 256 维）。

档案独立于人脸档案（device_profile_voice 表）：同一设备可分别记住人脸与人声，
名字不互绑；比对只在本表内余弦（vpr-engine 的 /compare 不在主服务使用）。
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any

from deskbot_server.dao import device_profile_voice_mapper as mapper
from deskbot_server.vision.face_identity import ema_update_descriptor, is_embedding_vector

# 声纹 embedding 均为 256 维 WeSpeaker 向量；不同名档案相似度 ≥ 此值才并入新名
# （防跨人误并，与人脸侧 0.95 同值）
RENAME_MERGE_THRESHOLD = 0.95
# 档案向量类型标记（与 face 的 "embedding"/"geometry" 区分开，跨模态永不比较）
VOICE_DESCRIPTOR_KIND = "voice"

# ────────────────── 版本计数（预留 tracker 热加载，与 face 对齐）──────────────────

_version: int = 0
_version_lock = threading.Lock()


def _bump_version() -> None:
    global _version
    with _version_lock:
        _version += 1


def get_version() -> int:
    with _version_lock:
        return _version


# ────────────────── ORM → dict ─────────────────────────


def _row_to_dict(row) -> dict[str, Any]:
    descriptor = json.loads(row.descriptor) if isinstance(row.descriptor, str) else row.descriptor
    return {
        "id": int(row.id),
        "person_id": int(row.id),
        "name": str(row.name),
        "descriptor": descriptor,
        "descriptor_kind": str(row.descriptor_kind or VOICE_DESCRIPTOR_KIND),
    }


# ────────────────── 查询 ─────────────────────────


def load_voice_profiles(*, device_id: str | None = None) -> list[dict[str, Any]]:
    if not device_id:
        return []
    rows = mapper.list_by_device(str(device_id))
    return [_row_to_dict(r) for r in rows]


def list_voice_profiles_summary(*, device_id: str | None = None) -> list[dict[str, Any]]:
    """列表展示用：不含 descriptor 向量。"""
    return [
        {
            "id": int(p["id"]),
            "person_id": int(p["id"]),
            "name": str(p["name"]),
            "descriptor_kind": str(p.get("descriptor_kind") or VOICE_DESCRIPTOR_KIND),
        }
        for p in load_voice_profiles(device_id=device_id)
    ]


# ────────────────── 向量匹配 ─────────────────────────


def _l2_norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def voice_cosine_similarity(a: list[float], b: list[float]) -> float:
    """真实余弦相似度（声纹向量未归一化，点积必须除以双模长）。

    ⚠ 勿复用 vision/face_identity.descriptor_cosine_similarity：它只算点积
    后钳制 [-1,1]（人脸向量 L2 归一化所以点积≈余弦）；wespeaker 输出 norm≈1~6，
    直接点积会轻易爆表被钳到 1.0，导致任何声音都判同人。
    """
    if not a or not b or len(a) != len(b):
        return -1.0
    na, nb = _l2_norm(a), _l2_norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot / (na * nb)))


def _unit_vector(v: list[float]) -> list[float]:
    """L2 归一化（建档/合并存储用；0 向量原样返回防除零）。"""
    n = _l2_norm(v)
    if n == 0.0:
        return list(v)
    return [x / n for x in v]


def best_voice_similarity(
    profiles: list[dict[str, Any]], descriptor: list[float]
) -> tuple[dict[str, Any] | None, float]:
    """返回最相似档案（不设阈值）；仅比较 256 维声纹向量（真实余弦）。"""
    best: dict[str, Any] | None = None
    best_sim = -1.0
    for p in profiles:
        pd = p.get("descriptor")
        if not isinstance(pd, list) or not is_embedding_vector(pd):
            continue
        sim = voice_cosine_similarity(descriptor, pd)
        if sim > best_sim:
            best_sim = sim
            best = p
    return best, best_sim


def find_voice_by_similarity(
    profiles: list[dict[str, Any]], descriptor: list[float], *, threshold: float
) -> tuple[dict[str, Any] | None, float]:
    best, best_sim = best_voice_similarity(profiles, descriptor)
    if best is not None and best_sim >= threshold:
        return best, best_sim
    return None, best_sim


# ────────────────── 写操作 ─────────────────────────


def upsert_voice_profile(
    device_id: str,
    *,
    name: str,
    descriptor: list[float],
    merge_threshold: float = 0.85,
) -> dict[str, Any]:
    """注册或合并同名/相似声纹档案，写入 DB 并返回最终 profile dict。

    入库前统一 L2 归一化（wespeaker 输出未归一化，归档一律存单位向量）；
    相似度用真实余弦（``voice_cosine_similarity``）：
    - 命中同名档案且相似度 ≥ merge_threshold（默认 0.85，从严）→ EMA 合并
      （0.5 级相似度就合并会把不同人同名档案互相污染，说话人逐句漂移幅度远小于此）；
    - 不同名档案仅相似度 ≥ 0.95 才并入新名（防跨人误并）；
    - 否则插入新档案。

    注意：0.85 是「建档/合并」阈值；「每句说话人判定」用 VoiceprintRuntime 的
    identity_similarity_threshold（默认 0.5），二者用途不同。
    """
    name = str(name).strip()
    if not name:
        raise ValueError("name required")
    if not isinstance(descriptor, list) or not is_embedding_vector(descriptor):
        raise ValueError("descriptor must be an embedding vector")
    descriptor = _unit_vector([float(x) for x in descriptor])

    profiles = load_voice_profiles(device_id=device_id)
    matched, sim = find_voice_by_similarity(profiles, descriptor, threshold=merge_threshold)

    if matched is not None and (matched.get("name") == name or sim >= RENAME_MERGE_THRESHOLD):
        merged = ema_update_descriptor(matched["descriptor"], descriptor, alpha=0.35)
        merged_json = json.dumps(list(merged), ensure_ascii=False)
        mapper.update(int(matched["id"]), name, merged_json, VOICE_DESCRIPTOR_KIND)
        _bump_version()
        return {"id": int(matched["id"]), "person_id": int(matched["id"]), "name": name, "descriptor": merged, "descriptor_kind": VOICE_DESCRIPTOR_KIND}

    new_id = mapper.insert(str(device_id), name, json.dumps(list(descriptor), ensure_ascii=False), VOICE_DESCRIPTOR_KIND)
    _bump_version()
    return {"id": new_id, "person_id": new_id, "name": name, "descriptor": list(descriptor), "descriptor_kind": VOICE_DESCRIPTOR_KIND}


def delete_voice_profile(profile_id: int, *, device_id: str | None = None) -> bool:
    """按 ``id`` 删除已注册声纹档案。"""
    try:
        pid = int(profile_id)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    row = mapper.get_by_id(pid)
    if row is None:
        return False
    if device_id and str(row.device_id) != str(device_id):
        return False
    mapper.delete_by_id(pid)
    _bump_version()
    return True


def update_voice_profile_name(
    profile_id: int, name: str, *, device_id: str | None = None
) -> dict[str, Any] | None:
    """更新已注册声纹档案名称，返回摘要；档案不存在时返回 ``None``。"""
    try:
        pid = int(profile_id)
    except (TypeError, ValueError):
        return None
    clean_name = str(name or "").strip()
    if pid <= 0 or not clean_name:
        return None
    row = mapper.get_by_id(pid)
    if row is None:
        return None
    if device_id and str(row.device_id) != str(device_id):
        return None
    mapper.update_name(pid, clean_name)
    _bump_version()
    return {
        "id": int(row.id),
        "person_id": int(row.id),
        "name": clean_name,
        "descriptor_kind": str(row.descriptor_kind or VOICE_DESCRIPTOR_KIND),
    }
