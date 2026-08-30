"""人脸相似性特征纯函数：descriptor 判定 / 几何特征 / 相似度 / EMA / 阈值。

v1.2.0 起主服务不再做人脸推理（只调外部 insightface-engine 的 /detect，
进程内推理已移除），本模块只保留对「已有 descriptor」的纯函数运算
（跟踪 / 注册 / 缓存链路用），不依赖任何推理模块：
- ``is_embedding_vector`` / ``is_legacy_geometric_vector``：descriptor 类型判定
  （原 face_embedding.py 的函数，随推理移除搬入本模块）
- ``compute_face_descriptor``：landmarks 几何特征向量（纯数学）
- ``descriptor_cosine_similarity`` / ``ema_update_descriptor`` / ``match_threshold_for_descriptor``

推理相关（attach / dedup / 现场算 embedding）已随独立化移除——
外部服务 /detect 返回的 faces 自带 embedding（或几何 descriptor）。
"""

from __future__ import annotations

import math
from typing import Any

from deskbot_server.vision.geometry import compute_eye_iris_offsets, kps5_from_landmarks

FACE_EMBEDDING_DIM = 512


def is_embedding_vector(desc: list[float] | None) -> bool:
    return isinstance(desc, list) and len(desc) >= 64


def is_legacy_geometric_vector(desc: list[float] | None) -> bool:
    return isinstance(desc, list) and 4 <= len(desc) < 64


def compute_face_descriptor(landmarks: list) -> list[float] | None:
    """提取尺度不变的人脸几何特征向量（L2 归一化，适合余弦相似度）。

    特征仅依赖五官相对比例，与脸在画面中的位置无关；对小幅 yaw/pitch 有一定鲁棒性，
    侧脸过大或遮挡时相似度会下降。
    """
    kps = kps5_from_landmarks(landmarks) or []
    by = {p["name"]: p for p in kps if isinstance(p, dict) and p.get("name")}
    le = by.get("left_eye")
    re_ = by.get("right_eye")
    ns = by.get("nose")
    ml = by.get("mouth_left")
    mr = by.get("mouth_right")
    if not (le and re_ and ns and ml and mr):
        return None
    try:
        lex, ley = float(le["x"]), float(le["y"])
        rex, rey = float(re_["x"]), float(re_["y"])
        nsx, nsy = float(ns["x"]), float(ns["y"])
        mlx, mly = float(ml["x"]), float(ml["y"])
        mrx, mry = float(mr["x"]), float(mr["y"])
    except (TypeError, ValueError):
        return None

    eye_dist = math.hypot(rex - lex, rey - ley)
    if eye_dist < 1e-3:
        return None

    eye_cx = (lex + rex) * 0.5
    eye_cy = (ley + rey) * 0.5
    mouth_cx = (mlx + mrx) * 0.5
    mouth_cy = (mly + mry) * 0.5
    mouth_w = math.hypot(mrx - mlx, mry - mly)

    feats: list[float] = [
        (nsx - eye_cx) / eye_dist,
        (nsy - eye_cy) / eye_dist,
        mouth_w / eye_dist,
        (mouth_cx - eye_cx) / eye_dist,
        (mouth_cy - eye_cy) / eye_dist,
        (nsy - eye_cy) / eye_dist,
        abs(rey - ley) / eye_dist,
    ]

    iris = compute_eye_iris_offsets(landmarks or [])
    for key in ("left_eye", "right_eye"):
        v = iris.get(key)
        feats.append((float(v) - 0.5) if v is not None else 0.0)

    norm = math.sqrt(sum(x * x for x in feats))
    if norm < 1e-6:
        return None
    return [round(x / norm, 6) for x in feats]


def descriptor_cosine_similarity(a: list[float], b: list[float]) -> float:
    """两特征向量余弦相似度 [-1, 1]；输入须等长。"""
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot))


def ema_update_descriptor(prev: list[float] | None, sample: list[float], *, alpha: float = 0.2) -> list[float]:
    """对 profile / track 特征做指数滑动平均并重新归一化。"""
    if prev is None or len(prev) != len(sample):
        return list(sample)
    a = max(0.05, min(0.5, float(alpha)))
    merged = [(1.0 - a) * p + a * s for p, s in zip(prev, sample)]
    norm = math.sqrt(sum(x * x for x in merged))
    if norm < 1e-6:
        return list(sample)
    return [round(x / norm, 6) for x in merged]


def match_threshold_for_descriptor(
    desc: list[float], *, embedding_threshold: float, geometry_threshold: float
) -> float:
    """按向量类型选用阈值：embedding 约 0.40，几何约 0.88。"""
    if is_embedding_vector(desc):
        return max(0.25, min(0.99, float(embedding_threshold)))
    return max(0.75, min(0.99, float(geometry_threshold)))
