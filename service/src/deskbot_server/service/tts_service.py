"""语音合成服务（设备级动态路由）。

TTS provider 按设备解析（device 表 tts_provider，默认 moss-tts-nano），每次调用
``resolve_tts_adapter`` 构造轻量 adapter（豆包连接池按 config key 复用）——不再全局
rebind 单一实现。``bind`` 保留作显式注入兜底（如测试与 ChatService 直连引用）。
"""

from __future__ import annotations

from deskbot_server.infrastructure.tts.resolve import resolve_tts_adapter
from deskbot_server.ports.tts import TtsPort
from deskbot_server.utils.singleton import SingletonMeta


class TtsService(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self._tts: TtsPort | None = None

    def bind(self, tts: TtsPort) -> None:
        self._tts = tts

    @property
    def tts(self) -> TtsPort:
        if self._tts is None:
            raise RuntimeError("TtsService 尚未 bind，请先在 bootstrap 中装配")
        return self._tts

    @staticmethod
    def _seg_to_dict(s) -> dict:
        if isinstance(s, dict):
            return {
                "phoneme": s.get("phoneme"),
                "ms": s.get("ms"),
                "pcm": s.get("pcm"),
                "phoneme_id": s.get("phoneme_id"),
            }
        return {"phoneme": s.phoneme, "ms": s.ms, "pcm": s.pcm, "phoneme_id": s.phoneme_id}

    def _resolve(self, device_id: str | None) -> TtsPort:
        """优先按设备动态解析；解析失败（如 DB 未就绪）回落到 bind 的兜底。"""
        try:
            return resolve_tts_adapter(device_id)
        except Exception:
            return self.tts

    async def synthesize_phoneme_segments(
        self, text: str, *, device_id: str | None = None
    ) -> tuple[int, list[dict]]:
        sr, segs = await self._resolve(device_id).synthesize_phoneme_segments(text)
        return sr, [self._seg_to_dict(s) for s in segs]
