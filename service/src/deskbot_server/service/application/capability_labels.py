"""ASR / LLM / TTS 生效能力的人读展示标签（事件字段用）。

与各 resolve 层同一套"设备级覆盖 > env > 默认"口径，只产出短展示串，
不构造 adapter、不做网络调用。供实验台「实时对话」事件携带
``asr_model`` / ``llm_model`` / ``tts_model`` 字段。

解析失败（设备不存在、DB 异常等）一律返回 None，事件侧按缺省展示。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("deskbot-server")


def asr_model_label(device_id: str | None) -> str | None:
    """ASR 生效 provider + 识别模型（funasr=本地 SenseVoiceSmall / doubao=Seed-ASR）。"""
    try:
        from deskbot_server.dao.device_mapper import get_asr_provider
        from deskbot_server.infrastructure.asr.resolve import DEFAULT_ASR_PROVIDER, SUPPORTED_ASR_PROVIDERS

        provider = get_asr_provider(device_id) if device_id else None
        if provider not in SUPPORTED_ASR_PROVIDERS:
            provider = DEFAULT_ASR_PROVIDER
        if provider == "doubao":
            return "doubao · Seed-ASR(bigmodel)"
        return "funasr · SenseVoiceSmall"
    except Exception:
        logger.debug("[capability_labels] asr 标签解析失败 device_id=%s", device_id, exc_info=True)
        return None


def llm_model_label(device_id: str | None) -> str | None:
    """LLM 实际请求模型串（meta.model 口径）：qwen3.8-2b / minicpm5-1b / ep-…。"""
    try:
        from deskbot_server.infrastructure.llm.runtime import resolve_llm_config

        cfg = resolve_llm_config(device_id or "")
        model = str(getattr(cfg, "model", "") or "").strip()
        if model:
            return model
        name = str(getattr(cfg, "display_name", "") or "").strip()
        return name or None
    except Exception:
        logger.debug("[capability_labels] llm 标签解析失败 device_id=%s", device_id, exc_info=True)
        return None


def tts_model_label(device_id: str | None) -> str | None:
    """TTS 生效 provider + 模型/音色：moss-tts-nano · demo-1 / doubao · seed-tts-2.0…。"""
    try:
        from deskbot_server.dao.device_mapper import get_tts_param, get_tts_provider
        from deskbot_server.infrastructure.tts.doubao import DEFAULT_MODEL
        from deskbot_server.infrastructure.tts.factory import DEFAULT_TTS_PROVIDER, SUPPORTED_TTS_PROVIDERS
        from deskbot_server.infrastructure.tts.moss_adapter import DEFAULT_DEMO_ID

        provider = get_tts_provider(device_id) if device_id else None
        if provider not in SUPPORTED_TTS_PROVIDERS:
            provider = DEFAULT_TTS_PROVIDER
        params = get_tts_param(device_id) if device_id else {}

        if provider == "doubao":
            raw = params.get("doubao") or {}
            model = str(raw.get("model") or "").strip() or DEFAULT_MODEL
            short = str(model)
            if short.endswith("-expressive"):
                short = short[: -len("-expressive")]
            return f"doubao · {short or model}"
        raw = params.get("moss") or {}
        demo = str(raw.get("demo_id") or "").strip() or DEFAULT_DEMO_ID
        return f"moss-tts-nano · {demo}"
    except Exception:
        logger.debug("[capability_labels] tts 标签解析失败 device_id=%s", device_id, exc_info=True)
        return None
