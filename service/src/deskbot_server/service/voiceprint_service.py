"""声纹识别服务：每次 VAD 出句后识别「这句是谁说的」。

镜像 camera_face_service 的结构与降级哲学：
- 推理全走外部 wespeaker-resnet34（/voiceprint → 256 维 embedding），主服务只做档案余弦匹配；
- mode=none 时 ``identify`` 直接返回（零 /voiceprint 流量）；
- 引擎不可达/过短音频等一律不抛到对话链路，快照写终态（unknown/degraded）后静默降级；
- 每次成功抽出 embedding（无论是否匹配）都存入注册样本槽，供 register_voiceprint 建档。

竞态防护（防重叠 utterance 串句错名）：
- 每设备 asyncio.Lock 单飞行——识别顺序 = 出句顺序；
- voice_snapshot_cache 的 seq 写守卫——新句 begin 后旧句结果写不进；
- ``_inflight`` 持有在途任务强引用（防 asyncio「Task was destroyed」）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from deskbot_server.infrastructure.voice.vpr_http_client import VprHttpClient, VprHttpError
from deskbot_server.service.application import voice_snapshot_cache as snap
from deskbot_server.service.voice_profile_service import (
    VOICE_DESCRIPTOR_KIND,
    find_voice_by_similarity,
    load_voice_profiles,
    upsert_voice_profile,
)
from deskbot_server.utils.singleton import SingletonMeta

logger = logging.getLogger("deskbot-server")

# _run_asr_turn 等待单句识别的预算：超时本轮不带名字（快照稍后补写，供下轮/展示）
VOICEPRINT_WAIT_BUDGET_S = 3.0
# 引擎降级日志节流：同一设备 60s 内至多打一条 info（防引擎长时间停机刷屏）
_DEGRADED_LOG_INTERVAL_S = 60.0
_KNOWN_SOFT_ERROR_CODES = ("AUDIO_TOO_SHORT", "INVALID_AUDIO")

DEFAULT_VPR_URL = "http://127.0.0.1:9104"


@dataclass(frozen=True)
class VoiceprintRuntime:
    """全局声纹识别运行时参数（所有设备同一套；推理参数在外部服务侧）。"""

    # 声纹识别能力开关：vpr=外部独立服务；none=不识别（VAD 出句零 /voiceprint 流量）
    mode: str = "none"
    # 外部 wespeaker-resnet34 地址
    external_url: str = DEFAULT_VPR_URL
    external_timeout_s: float = 5.0
    # 说话人判定阈值（与档案的余弦相似度 ≥ 此值判同人）；可调
    identity_similarity_threshold: float = 0.5
    # 注册样本有效期：最近一次声音样本超过该秒数后 register 需重新说话
    sample_max_age_s: float = 60.0


def build_voiceprint_runtime(config: dict[str, Any]) -> VoiceprintRuntime:
    """从 yaml 构建声纹运行时（不按 device 区分；无全局调参覆盖文件）。"""
    raw = dict(config.get("voiceprint") or {})

    external_url = str(raw.get("external_url") or DEFAULT_VPR_URL).strip() or DEFAULT_VPR_URL
    external_timeout_s = max(1.0, float(raw.get("external_timeout_s") or 5.0))
    sample_max_age_s = max(1.0, float(raw.get("sample_max_age_s") or 60.0))

    ist = float(raw.get("identity_similarity_threshold", 0.5))
    ist = max(0.10, min(0.99, ist))

    mode = str(raw.get("mode") or "none").strip().lower()
    if mode not in ("none", "vpr"):
        logger.warning("[vpr] 未知 mode=%s，回落 none（声纹识别关闭）", mode)
        mode = "none"

    logger.info(
        "[vpr] mode=%s external_url=%s timeout_s=%.1f identity_threshold=%.2f sample_max_age_s=%.0f",
        mode, external_url, external_timeout_s, ist, sample_max_age_s,
    )
    return VoiceprintRuntime(
        mode=mode,
        external_url=external_url,
        external_timeout_s=external_timeout_s,
        identity_similarity_threshold=ist,
        sample_max_age_s=sample_max_age_s,
    )


class VoiceprintService(metaclass=SingletonMeta):
    """VAD utterance 声纹识别入口（全局一套 runtime）。

    - ``identify``：单句识别 → 快照终态（found/unknown/degraded），永不抛异常
    - ``register_voice_embedding``：档案写入
    - ``enabled``：调用方据此决定是否起识别任务（避免每次 import 探测）
    """

    def __init__(self) -> None:
        self._runtime: VoiceprintRuntime | None = None
        self._client: VprHttpClient | None = None
        # 每设备串行识别锁 + 在途任务强引用（防并发乱序/任务被 GC）
        self._locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._last_degraded_log: dict[str, float] = {}

    # ----- 配置 -----

    def configure(self, runtime: VoiceprintRuntime) -> None:
        self._runtime = runtime
        if runtime.mode == "none":
            self._client = None
            snap.clear_all_devices()
            logger.info("[vpr] 声纹识别已关闭（mode=none）")
            return
        self._client = VprHttpClient(runtime.external_url, timeout_s=runtime.external_timeout_s)

    def is_configured(self) -> bool:
        return self._runtime is not None

    def enabled(self) -> bool:
        return self._runtime is not None and self._runtime.mode == "vpr" and self._client is not None

    @property
    def runtime(self) -> VoiceprintRuntime:
        if self._runtime is None:
            raise RuntimeError("VoiceprintService 尚未 configure")
        return self._runtime

    def _lock_for(self, device_id: str) -> asyncio.Lock:
        lock = self._locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[device_id] = lock
        return lock

    # ----- 识别 -----

    async def identify(
        self,
        *,
        device_id: str | None = None,
        pcm_bytes: bytes,
        sample_rate: int = 16000,
        request_id: str | None = None,
    ) -> None:
        """VAD 出句后识别说话人并写快照终态。永不抛异常（不进对话链路）。

        单飞行：同设备并发调用时按到达顺序串行（后到的等先到的完成后才开始），
        配合快照 seq 守卫保证快照始终对应当前最新 utterance。
        """
        if not self.enabled():
            return
        dev = str(device_id or "").strip()
        if not dev:
            return
        lock = self._lock_for(dev)
        current = asyncio.current_task()
        if current is not None:
            self._inflight[dev] = current  # 强引用，防 GC
        try:
            async with lock:
                await self._identify_locked(dev, pcm_bytes=pcm_bytes, sample_rate=sample_rate, request_id=request_id)
        finally:
            if current is not None and self._inflight.get(dev) is current:
                self._inflight.pop(dev, None)

    async def _identify_locked(
        self,
        device_id: str,
        *,
        pcm_bytes: bytes,
        sample_rate: int,
        request_id: str | None,
    ) -> None:
        client = self._client
        runtime = self._runtime
        if client is None or runtime is None:
            return  # 等待锁期间被 configure(none) 关闭
        req = str(request_id or "").strip()
        seq = snap.begin_identification(device_id, req)
        try:
            embedding = await client.embedding(pcm_bytes, int(sample_rate or 16000))
        except VprHttpError as exc:
            if exc.code in _KNOWN_SOFT_ERROR_CODES:
                snap.finish_identification(device_id, seq, state=snap.STATE_UNKNOWN)
                logger.debug("[vpr] 音频过短/非法 device_id=%s req=%s code=%s msg=%s", device_id, req, exc.code, exc)
            else:
                snap.finish_identification(device_id, seq, state=snap.STATE_DEGRADED)
                self._log_degraded(device_id, f"vpr-engine 不可用: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都不进对话链路
            snap.finish_identification(device_id, seq, state=snap.STATE_DEGRADED)
            self._log_degraded(device_id, f"vpr-engine 识别异常: {exc}")
            return

        # embedding 抽出成功（无论是否匹配）都入样本槽，供 register_voiceprint 建档
        snap.store_voice_sample(device_id, req, embedding)
        try:
            profiles = load_voice_profiles(device_id=device_id)
        except Exception:  # noqa: BLE001 - 档案读取失败按未匹配处理
            profiles = []
        best, sim = find_voice_by_similarity(
            profiles, embedding, threshold=runtime.identity_similarity_threshold
        )
        if best is None:
            snap.finish_identification(device_id, seq, state=snap.STATE_UNKNOWN)
            logger.debug("[vpr] 未匹配已知声纹 device_id=%s req=%s best_sim=%.3f", device_id, req, sim)
            return
        name = str(best.get("name") or "").strip() or "未知"
        snap.finish_identification(device_id, seq, state=snap.STATE_FOUND, name=name, score=sim)
        logger.info(
            "[vpr] 说话人识别 device_id=%s req=%s name=%s sim=%.3f",
            device_id, req, name, sim,
        )

    def _log_degraded(self, device_id: str, message: str) -> None:
        """引擎降级日志节流：同设备 60s 内只打一条（防长时间停机刷屏）。"""
        now = time.monotonic()
        last = self._last_degraded_log.get(device_id, 0.0)
        if now - last >= _DEGRADED_LOG_INTERVAL_S:
            self._last_degraded_log[device_id] = now
            logger.warning("[vpr] device_id=%s %s", device_id, message)
        else:
            logger.debug("[vpr] device_id=%s %s", device_id, message)

    # ----- 档案 -----

    def register_voice_embedding(
        self, name: str, embedding: list[float], *, device_id: str | None = None
    ) -> dict[str, Any]:
        """将说话人 embedding 写入声纹档案（device 维度），返回 profile dict。"""
        dev = str(device_id or "").strip()
        if not dev:
            raise ValueError("device_id required")
        name = str(name or "").strip()
        if not name:
            raise ValueError("name required")
        if not isinstance(embedding, list) or len(embedding) < 64:
            raise ValueError("embedding required (256 维声纹向量)")
        rt = self._runtime
        merge_threshold = 0.85 if rt is None else max(0.85, rt.identity_similarity_threshold)
        profile = upsert_voice_profile(dev, name=name, descriptor=list(embedding), merge_threshold=merge_threshold)
        logger.info("[vpr] 声纹档案写入 device_id=%s name=%s id=%s kind=%s", dev, name, profile.get("id"), VOICE_DESCRIPTOR_KIND)
        return profile
