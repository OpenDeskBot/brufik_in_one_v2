"""每设备「最近一次 VAD 说话人」声纹判定快照 + 注册样本槽（进程内）。

与人脸快照（face_snapshot_cache：持续推流帧）不同，声纹判定只在 VAD 出句时发生，
且同设备可能有两个 utterance 重叠在途——因此快照带 **seq 写守卫**：

- ``begin_identification`` 分配单调递增 seq 并立即把快照置为 identifying（清除旧名字，
  防止上一句结果残留错配到新说话人）；
- ``finish_identification`` 仅当传入 seq 仍是当前 seq 才写入——新一句一旦 begin，
  旧一句的识别结果永远写不进来。

快照终态：found（匹配到档案）/ unknown（成功出 embedding 但未匹配，或音频过短 422）/
degraded（引擎不可达/超时/模型未就绪）。每设备只保留最新一句。

注册样本槽：每次成功抽出 embedding（无论 found/unknown）都存入「最近声音样本」
（每设备单槽 + 时间戳），供 register_voiceprint / 后台注册使用（新声音首次注册依赖它）。
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_snapshots: dict[str, dict[str, Any]] = {}
_samples: dict[str, dict[str, Any]] = {}

STATE_IDLE = "idle"
STATE_IDENTIFYING = "identifying"
STATE_FOUND = "found"
STATE_UNKNOWN = "unknown"
STATE_DEGRADED = "degraded"


# ────────────────── 快照 ─────────────────────────


def begin_identification(device_id: str, request_id: str | None = None) -> int:
    """开始一次识别：置快照为 identifying 并清除旧名字，返回本次 seq。"""
    device_id = str(device_id or "").strip()
    with _lock:
        prev = _snapshots.get(device_id) or {}
        try:
            seq = int(prev.get("seq") or 0) + 1
        except (TypeError, ValueError):
            seq = 1
        _snapshots[device_id] = {
            "state": STATE_IDENTIFYING,
            "seq": seq,
            "name": None,
            "score": None,
            "ts": time.time(),
            "request_id": str(request_id or ""),
        }
    return seq


def finish_identification(
    device_id: str,
    seq: int,
    *,
    state: str,
    name: str | None = None,
    score: float | None = None,
) -> bool:
    """写识别终态；仅当 ``seq`` 仍是该设备当前 seq（无更新 utterance 抢跑）才生效。"""
    device_id = str(device_id or "").strip()
    with _lock:
        cur = _snapshots.get(device_id)
        if cur is None or int(cur.get("seq") or -1) != int(seq):
            return False  # 已过期：更新 utterance 已 begin，本结果丢弃（防串句错名）
        cur["state"] = state
        cur["name"] = str(name) if name else None
        try:
            cur["score"] = round(float(score), 3) if score is not None else None
        except (TypeError, ValueError):
            cur["score"] = None
        cur["ts"] = time.time()
        return True


def get_voice_snapshot(device_id: str) -> dict[str, Any] | None:
    """返回快照副本；无记录返回 None（与 idle/identifying 等同视作「暂无判定」）。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return None
    with _lock:
        mem = _snapshots.get(device_id)
    return dict(mem) if mem else None


def clear_device(device_id: str) -> None:
    """删除设备快照与注册样本（configure mode=none / 设备解绑时调用）。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return
    with _lock:
        _snapshots.pop(device_id, None)
        _samples.pop(device_id, None)


def clear_all_devices() -> None:
    with _lock:
        _snapshots.clear()
        _samples.clear()


# ────────────────── 注册样本槽 ─────────────────────────


def store_voice_sample(device_id: str, request_id: str, embedding: list[float]) -> None:
    """保存最近一次成功抽出 embedding 的语音样本（每设备单槽，供注册）。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return
    with _lock:
        _samples[device_id] = {
            "ts": time.time(),
            "request_id": str(request_id or ""),
            "embedding": [float(x) for x in embedding],
        }


def take_voice_sample(device_id: str, max_age_s: float = 60.0) -> list[float] | None:
    """取最近声音样本的 embedding；过期/缺失返回 None（不删除，留给下次取）。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        return None
    try:
        cap = max(1.0, float(max_age_s))
    except (TypeError, ValueError):
        cap = 60.0
    with _lock:
        mem = _samples.get(device_id)
        if not mem:
            return None
        try:
            age = time.time() - float(mem.get("ts") or 0.0)
        except (TypeError, ValueError):
            age = float("inf")
        if age > cap:
            return None
        emb = mem.get("embedding")
    return list(emb) if isinstance(emb, list) else None
