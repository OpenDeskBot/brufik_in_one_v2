"""TTS provider 设备级解析（仿 infrastructure/asr/resolve.py）。

真源：device 表 ``tts_provider`` 列（moss-tts-nano / doubao），查不到（设备不存在/未设置/匿名连接）
一律回落默认 ``moss-tts-nano``。config.yaml 不再持有 provider，仅保留播放行为参数
（sample_rate / pb_random_servo 等）。

设备级参数（device 表 ``tts_param``，JSON）按 provider 注入 adapter：
- moss：``moss.{demo_id,base_url}`` 覆盖 config.yaml tts.extra
- doubao：``doubao.{api_key,speaker,resource_id,model,ws_url,sample_rate,audio_format}``
  覆盖全局 env（未填字段回落 env）
两个 adapter 均为轻量构造（豆包连接池按 config key 复用），每次调用解析构造即可。
"""

from __future__ import annotations

import logging

from deskbot_server.config import load_config
from deskbot_server.dao.device_mapper import get_tts_param, get_tts_provider
from deskbot_server.infrastructure.tts.doubao import DOUBAO_TTS_FIELDS, _is_masked_secret
from deskbot_server.infrastructure.tts.doubao_phoneme import DoubaoPhonemeTtsAdapter
from deskbot_server.infrastructure.tts.factory import DEFAULT_TTS_PROVIDER, SUPPORTED_TTS_PROVIDERS
from deskbot_server.infrastructure.tts.moss_adapter import MOSS_TTS_FIELDS, MossTtsAdapter
from deskbot_server.model.settings import AppSettings
from deskbot_server.ports.tts import TtsPort

logger = logging.getLogger("deskbot-server")


def resolve_tts_provider(device_id: str | None) -> str:
    """设备级 TTS provider；匿名连接或查不到 → 默认 moss-tts-nano。"""
    if not device_id:
        return DEFAULT_TTS_PROVIDER
    provider = get_tts_provider(device_id)
    return provider if provider in SUPPORTED_TTS_PROVIDERS else DEFAULT_TTS_PROVIDER


def _clean_overrides(raw: object, fields: tuple[str, ...]) -> dict[str, str]:
    """白名单 + 非空 + 非掩码提取覆盖字段。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in fields:
        val = str(raw.get(key) or "").strip()
        if val and not _is_masked_secret(val):
            out[key] = val
    return out


def _moss_overrides(params: dict) -> dict[str, str]:
    return _clean_overrides(params.get("moss"), MOSS_TTS_FIELDS)


def _doubao_overrides(params: dict) -> dict[str, str]:
    return _clean_overrides(params.get("doubao"), DOUBAO_TTS_FIELDS)


def resolve_tts_adapter(device_id: str | None = None, settings: AppSettings | None = None) -> TtsPort:
    """按设备解析 TTS adapter（轻量构造，可每次调用）。

    参数优先级：设备 tts_param > 全局 env > config.yaml/默认值。
    """
    provider = resolve_tts_provider(device_id)
    if settings is None:
        settings = AppSettings.from_config(load_config())
    params = get_tts_param(device_id) if device_id else {}
    if provider == "doubao":
        return DoubaoPhonemeTtsAdapter(settings, overrides=_doubao_overrides(params))
    if provider != DEFAULT_TTS_PROVIDER:
        logger.warning("[TTS] 未知 provider=%r，回落 moss-tts-nano", provider)
    return MossTtsAdapter(settings, overrides=_moss_overrides(params))
