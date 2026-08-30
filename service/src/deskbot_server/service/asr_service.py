"""语音识别服务（设备级动态路由）。

ASR provider 按设备解析（device 表 asr_provider，默认 funasr），每次调用
``resolve_asr_adapter`` 构造无状态 adapter——不再全局 bind 单一实现。
``bind`` 保留作显式注入兜底（如测试与 ChatService 直连引用）。
"""

from __future__ import annotations

from deskbot_server.infrastructure.asr.resolve import resolve_asr_adapter
from deskbot_server.ports.asr import AsrPort
from deskbot_server.utils.singleton import SingletonMeta


class AsrService(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self._asr: AsrPort | None = None

    def bind(self, asr: AsrPort) -> None:
        self._asr = asr

    @property
    def asr(self) -> AsrPort:
        if self._asr is None:
            raise RuntimeError("AsrService 尚未 bind，请先在 bootstrap 中装配")
        return self._asr

    def _resolve(self, device_id: str | None) -> AsrPort:
        """优先按设备动态解析；解析失败（如 DB 未就绪）回落到 bind 的兜底。"""
        try:
            return resolve_asr_adapter(device_id)
        except Exception:
            return self.asr

    async def transcribe(self, pcm_bytes: bytes, sample_rate: int, *, device_id: str | None = None) -> str:
        return await self._resolve(device_id).transcribe(pcm_bytes, sample_rate)

    def is_valid_text(self, text: str, *, device_id: str | None = None) -> bool:
        return self._resolve(device_id).is_valid_text(text)
