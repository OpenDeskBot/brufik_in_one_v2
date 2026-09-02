"""实验台「实时对话」音频仓库：按 request_id 留存 ASR 原声 / TTS 合成音。

内存 LRU（进程内唯一实例，与流水事件窗口同生命周期），存的是已加 WAV 头的
``audio/wav`` 字节，供 ``GET /api/pipeline_audio`` 回放；不落盘、不跨重启。

- ``put(device_id, request_id, kind, pcm, sample_rate)``：s16le mono PCM → WAV 存入；
- ``get/has``：回放与事件字段（audio_asr / audio_tts）判断。

淘汰策略：条目数与总字节双上限 + TTL 惰性淘汰（取/写时清理过期项），超限丢最旧。
kind 限定 ``{"asr", "tts"}``。音频单条上限保护（超长截断到上限避免撑爆内存）。
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

from deskbot_server.utils.audio import pcm_to_wav_bytes
from deskbot_server.utils.singleton import SingletonMeta

logger = logging.getLogger("deskbot-server")

KIND_ASR = "asr"
KIND_TTS = "tts"
KIND_FACE = "face"  # 最近帧 jpeg 原样字节（非 wav）
KINDS = frozenset({KIND_ASR, KIND_TTS, KIND_FACE})

MAX_ITEMS = 256  # 最大条数（LRU 淘汰）
MAX_TOTAL_BYTES = 96 * 1024 * 1024  # WAV 字节总量上限
TTL_S = 30 * 60  # 惰性 TTL：超过后下一次访问清除
# 单条合成音频上限（wav 字节，约 50 分钟 16k 16bit mono，足够任何单轮口播）
MAX_ENTRY_BYTES = 96 * 1024 * 1024


class ConvoAudioStore(metaclass=SingletonMeta):
    """滚动内存音频仓库。key = ``f"{device_id}:{request_id}:{kind}"``。"""

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0

    # ── 写 ──────────────────────────────────────────────────────────

    def put_raw(self, device_id: str | None, request_id: str | None, kind: str, data: bytes) -> bool:
        """原样字节存入（仅 ``face`` 最近帧 jpeg）；其余参数/kind 校验与 put 一致。"""
        if kind != KIND_FACE or not device_id or not request_id or not data:
            return False
        return self._store(device_id, request_id, kind, bytes(data))

    def put(
        self,
        device_id: str | None,
        request_id: str | None,
        kind: str,
        pcm: bytes,
        sample_rate: int,
        channels: int = 1,
    ) -> bool:
        """PCM（s16le）→ WAV 存入（asr/tts）。返回是否成功；参数缺失/kind 非法/异常返回 False。"""
        if kind not in KINDS or not device_id or not request_id or not pcm:
            return False
        try:
            sample_rate = int(sample_rate) or 16000
        except (TypeError, ValueError):
            sample_rate = 16000
        try:
            wav = pcm_to_wav_bytes(pcm, sample_rate, channels=channels)
        except Exception:
            logger.warning("[convo_audio] WAV 封装失败 device_id=%s req=%s kind=%s", device_id, request_id, kind)
            return False
        if len(wav) > MAX_ENTRY_BYTES:
            wav = wav[:MAX_ENTRY_BYTES]
        return self._store(device_id, request_id, kind, wav)

    def _store(self, device_id: str | None, request_id: str | None, kind: str, payload: bytes) -> bool:
        key = self._key(device_id, request_id, kind)
        try:
            self._prune_expired()
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old[1])
            self._items[key] = (time.time(), payload)
            self._bytes += len(payload)
            self._evict_if_over()
            logger.info(
                "[convo_audio] 已存 kind=%s device_id=%s req=%s bytes=%d items=%d",
                kind, device_id, request_id, len(payload), len(self._items),
            )
            return True
        except Exception:
            logger.exception("[convo_audio] 存储异常 device_id=%s req=%s kind=%s", device_id, request_id, kind)
            return False

    # ── 读 ──────────────────────────────────────────────────────────

    def get(self, device_id: str | None, request_id: str | None, kind: str) -> bytes | None:
        key = self._key(device_id, request_id, kind)
        if not key:
            return None
        try:
            self._prune_expired()
            item = self._items.pop(key, None)
            if item is None:
                return None
            self._items[key] = item  # 命中即刷新为最新（LRU）
            return item[1]
        except Exception:
            logger.debug("[convo_audio] 读取异常 key=%s", key, exc_info=True)
            return None

    def has(self, device_id: str | None, request_id: str | None, kind: str) -> bool:
        return self.get(device_id, request_id, kind) is not None

    def clear(self, device_id: str | None = None) -> None:
        """清空全部或某设备条目（切换设备/测试用）。"""
        if device_id is None:
            self._items.clear()
            self._bytes = 0
            return
        for key in list(self._items.keys()):
            if key.startswith(f"{device_id}:"):
                self._bytes -= len(self._items.pop(key)[1])

    # ── 内部 ────────────────────────────────────────────────────────

    @staticmethod
    def _key(device_id: str | None, request_id: str | None, kind: str) -> str:
        if not device_id or not request_id or kind not in KINDS:
            return ""
        return f"{device_id}:{request_id}:{kind}"

    def _evict_if_over(self) -> None:
        while (len(self._items) > MAX_ITEMS or self._bytes > MAX_TOTAL_BYTES) and self._items:
            _k, item = self._items.popitem(last=False)
            self._bytes -= len(item[1])

    def _prune_expired(self) -> None:
        now = time.time()
        expired = [k for k, (ts, _w) in self._items.items() if now - ts > TTL_S]
        for k in expired:
            self._bytes -= len(self._items.pop(k)[1])
        if expired:
            logger.debug("[convo_audio] 清理过期 %d 条，剩余 %d", len(expired), len(self._items))
