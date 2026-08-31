"""ASR provider 设备级解析（仿 infrastructure/llm/runtime.py::resolve_llm_config）。

真源：device 表 ``asr_provider`` 列（funasr / doubao），查不到（设备不存在/未设置/匿名连接）
一律回落默认 ``funasr``。config.yaml 不再持有 provider，仅保留基础设施参数
（external_url / text_filter）。

设备级参数（device 表 ``asr_param``，JSON）按 provider 注入 adapter：
- funasr：``funasr.url`` 覆盖 config.yaml 的 external_url
- doubao：``doubao.{api_key,resource_id,uid,url}`` 覆盖全局 env（未填字段回落 env）
两个 adapter 均无状态，每次调用解析构造即可。
"""

from __future__ import annotations

import logging

from deskbot_server.config import load_config
from deskbot_server.dao.device_mapper import get_asr_param, get_asr_provider
from deskbot_server.infrastructure.asr.doubao import DOUBAO_ASR_FIELDS
from deskbot_server.infrastructure.asr.doubao_adapter import DoubaoAsrAdapter
from deskbot_server.infrastructure.asr.env_store import _is_masked_secret
from deskbot_server.infrastructure.asr.funasr_adapter import FunAsrAdapter
from deskbot_server.model.settings import AppSettings
from deskbot_server.ports.asr import AsrPort

logger = logging.getLogger("deskbot-server")

DEFAULT_ASR_PROVIDER = "funasr"
SUPPORTED_ASR_PROVIDERS = ("funasr", "doubao")


def resolve_asr_provider(device_id: str | None) -> str:
    """设备级 ASR provider；匿名连接或查不到 → 默认 funasr。"""
    if not device_id:
        return DEFAULT_ASR_PROVIDER
    provider = get_asr_provider(device_id)
    return provider if provider in SUPPORTED_ASR_PROVIDERS else DEFAULT_ASR_PROVIDER


def _doubao_overrides(params: dict) -> dict[str, str]:
    """从 asr_param["doubao"] 提取干净覆盖字段（白名单 + 非空 + 非掩码）。"""
    raw = params.get("doubao") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in DOUBAO_ASR_FIELDS:
        val = str(raw.get(key) or "").strip()
        if val and not _is_masked_secret(val):
            out[key] = val
    return out


def resolve_asr_adapter(device_id: str | None = None, settings: AppSettings | None = None) -> AsrPort:
    """按设备解析 ASR adapter（无状态，可每次调用构造）。

    参数优先级：设备 asr_param > 全局 env > config.yaml/默认值。
    """
    provider = resolve_asr_provider(device_id)
    if settings is None:
        settings = AppSettings.from_config(load_config())
    params = get_asr_param(device_id) if device_id else {}
    if provider == "doubao":
        return DoubaoAsrAdapter(settings, overrides=_doubao_overrides(params))
    if provider != DEFAULT_ASR_PROVIDER:
        logger.warning("[ASR] 未知 provider=%r，回落 funasr", provider)
    funasr_raw = params.get("funasr") or {}
    funasr_url = str((funasr_raw.get("url") if isinstance(funasr_raw, dict) else None) or "").strip()
    return FunAsrAdapter(funasr_url or settings.asr.external_url, settings.asr.text_filter)
