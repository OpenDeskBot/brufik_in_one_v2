from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServerSettings:
    host: str = "0.0.0.0"
    port: int = 9000
    ws_path: str = "/asr_chat"
    ws_ping_interval: float | None = 20.0
    ws_ping_timeout: float = 20.0
    asr_chat_device_pb_only: bool = True
    asr_chat_minimal_device_downlink: bool = False
    send_face_info_to_asr_chat: bool = False
    web_public_host: str = ""


@dataclass(frozen=True)
class AudioSettings:
    input_codec: str = "opus"
    output_codec: str = "opus"
    sample_rate: int = 16000
    channels: int = 1


@dataclass(frozen=True)
class VadSettings:
    mode: int = 2
    frame_ms: int = 30
    min_speech_ms: int = 250
    max_silence_ms: int = 200
    pre_speech_ms: int = 300
    silero_model_path: str = ""
    silero_threshold: float = 0.5
    silero_threshold_low: float = 0.2


@dataclass(frozen=True)
class AsrTextFilterSettings:
    min_text_len: int = 2
    min_chinese_ratio: float = 0.0


@dataclass(frozen=True)
class AsrSettings:
    # 设备级 ASR provider 存 device 表（默认 funasr）；此处仅保留基础设施参数
    external_url: str = "http://127.0.0.1:9102"
    text_filter: AsrTextFilterSettings = field(default_factory=AsrTextFilterSettings)


@dataclass(frozen=True)
class LlmSettings:
    base_url: str = ""
    model_name: str = ""
    system_prompt: str = ""


@dataclass(frozen=True)
class TtsSettings:
    provider: str = "moss-tts-nano"
    ws_url: str = ""
    lang: str = "zh"
    spk_id: int = 0
    sample_rate: int = 16000
    pb_random_servo: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppSettings:
    """统一运行时配置：YAML + 环境变量覆盖。"""

    server: ServerSettings
    audio: AudioSettings
    vad: VadSettings
    asr: AsrSettings
    llm: LlmSettings
    tts: TtsSettings
    camera_face: dict[str, Any] = field(default_factory=dict)
    voiceprint: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> AppSettings:
        srv = dict(config.get("server") or {})
        audio = dict(config.get("audio") or {})
        vad = dict(config.get("vad") or {})
        asr = dict(config.get("asr") or {})
        llm = dict(config.get("llm") or {})
        tts = dict(config.get("tts") or {})
        tf = dict(asr.get("text_filter") or {})

        pb_only = bool(srv.get("asr_chat_device_pb_only", True))
        minimal = bool(srv.get("asr_chat_minimal_device_downlink", False))
        face_info = bool(srv.get("send_face_info_to_asr_chat", False))

        tts_extra = {
            k: v
            for k, v in tts.items()
            if k
            not in (
                "provider",
                "ws_url",
                "lang",
                "spk_id",
                "sample_rate",
                "pb_random_servo",
                "pb_face_bundle",  # Retired online face configuration; never forward to providers.
                "pb_face_bundle_json",
                "pb_face_bundle_file",
            )
        }

        return cls(
            server=ServerSettings(
                host=str(srv.get("host", "0.0.0.0")),
                port=int(srv.get("port", 9000)),
                ws_path=str(srv.get("ws_path", "/asr_chat")),
                ws_ping_interval=_parse_ping_interval(
                    None, srv.get("ws_ping_interval", 20)
                ),
                ws_ping_timeout=float(srv.get("ws_ping_timeout", 20)),
                asr_chat_device_pb_only=pb_only,
                asr_chat_minimal_device_downlink=minimal,
                send_face_info_to_asr_chat=face_info and not pb_only,
                web_public_host=str(srv.get("web_public_host") or ""),
            ),
            audio=AudioSettings(
                input_codec=str(audio.get("input_codec", "opus")),
                output_codec=str(audio.get("output_codec", "opus")),
                sample_rate=int(audio.get("sample_rate", 16000)),
                channels=int(audio.get("channels", 1)),
            ),
            vad=VadSettings(
                mode=int(vad.get("mode", 2)),
                frame_ms=int(vad.get("frame_ms", 30)),
                min_speech_ms=int(vad.get("min_speech_ms", 300)),
                max_silence_ms=int(vad.get("max_silence_ms", 200)),
                pre_speech_ms=int(vad.get("pre_speech_ms", 300)),
                silero_model_path=str(vad.get("silero_model_path", "")),
                silero_threshold=float(vad.get("silero_threshold", 0.5)),
                silero_threshold_low=float(vad.get("silero_threshold_low", 0.2)),
            ),
            asr=AsrSettings(
                external_url=str(asr.get("external_url") or "http://127.0.0.1:9102"),
                text_filter=AsrTextFilterSettings(
                    min_text_len=int(tf.get("min_text_len", 2)),
                    min_chinese_ratio=float(tf.get("min_chinese_ratio", 0.2)),
                ),
            ),
            llm=LlmSettings(
                base_url=str(llm.get("base_url", "")),
                model_name=str(llm.get("model_name", "")),
                system_prompt=str(llm.get("system_prompt", "")),
            ),
            tts=TtsSettings(
                provider=str(tts.get("provider") or "moss-tts-nano").strip().lower(),
                ws_url=str(tts.get("ws_url", "")),
                lang=str(tts.get("lang", "zh")),
                spk_id=int(tts.get("spk_id", 0)),
                sample_rate=int(tts.get("sample_rate", 16000)),
                pb_random_servo=dict(tts.get("pb_random_servo") or {}),
                extra=tts_extra,
            ),
            camera_face=dict(config.get("camera_face") or {}),
            voiceprint=dict(config.get("voiceprint") or {}),
            raw=config,
        )

    def pb_random_servo_cfg(self) -> dict[str, Any] | None:
        sub = self.tts.pb_random_servo
        enabled = bool(sub.get("enabled", False))
        if not enabled:
            return None
        out = dict(sub)
        out["enabled"] = True
        return out

    @property
    def tts_cfg(self) -> dict[str, Any]:
        """兼容旧代码对 dict 形式 tts 配置的访问。"""
        base = {
            "provider": self.tts.provider,
            "ws_url": self.tts.ws_url,
            "lang": self.tts.lang,
            "spk_id": self.tts.spk_id,
            "sample_rate": self.tts.sample_rate,
            "pb_random_servo": self.tts.pb_random_servo,
        }
        base["output_codec"] = self.audio.output_codec
        base.update(self.tts.extra)
        return base


def _parse_ping_interval(env_val: str | None, cfg_val: Any) -> float | None:
    raw = env_val if env_val is not None else str(cfg_val)
    raw = str(raw).strip().lower()
    if raw in ("0", "none", "off", "false"):
        return None
    return max(5.0, float(raw))
