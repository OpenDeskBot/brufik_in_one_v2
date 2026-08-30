"""机器人能力设置服务：ASR / LLM / TTS 能力选择与热切换。

- ASR：设备级配置（device 表 asr_provider，默认 funasr），写表即生效——
  ``resolve_asr_adapter`` 每次调用动态解析（见 infrastructure/asr/resolve.py）
- LLM：设备级覆盖（llm_models.json active 模型）优先于系统默认（config.yaml llm 段），
  系统级受 .env 覆盖（env 优先），apply_llm 会清掉协议类覆盖
- TTS：config.yaml 真源，写 config → 后台重建 adapter（走 to_thread）→ 成功才
  rebind 到 TtsService 单例；构造失败回滚 config，单例保持旧能力
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deskbot_server.config import load_config, save_config
from deskbot_server.controller.runtime import get_runtime
from deskbot_server.dao.device_mapper import set_asr_provider
from deskbot_server.dao.llm_config_store import get_active_llm_model, set_active_llm_model
from deskbot_server.infrastructure.asr.resolve import resolve_asr_provider
from deskbot_server.infrastructure.llm.env_store import clear_llm_env
from deskbot_server.infrastructure.llm.runtime import (
    ARK_OPENAI_BASE_URL,
    VOLCENGINE_PROTOCOLS,
    _entry_to_config,
    _normalized_protocol,
    is_local_llm_url,
    resolve_system_llm_config,
)
from deskbot_server.infrastructure.tts.factory import build_tts_adapter
from deskbot_server.model.settings import AppSettings
from deskbot_server.service.tts_service import TtsService

logger = logging.getLogger("deskbot-server")

# 本地 llm-engine（Cactus Needle 2）端点；服务端不校验 Authorization、不支持 stream
LLM_ENGINE_BASE_URL = "http://127.0.0.1:9104/v1"
LLM_ENGINE_MODEL = "cactus-needle-2"

# 页面提示用环境变量清单（存在即表示 config.yaml 被覆盖；ASR 已设备级化，不在其中）
ENV_OVERRIDE_KEYS = ("TTS_PROVIDER", "LLM_PROTOCOL", "LLM_MODEL", "LLM_BASE_URL")


class CapabilityError(RuntimeError):
    """能力切换失败（参数非法 / env 覆盖 / 构建失败已回滚）。"""


@dataclass(frozen=True)
class CapabilityCandidate:
    id: str
    name: str
    description: str
    requires_service: str | None = None  # 依赖的外部服务名（externals/*/service.yaml 的目录名）
    experimental: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "requires_service": self.requires_service,
            "experimental": self.experimental,
        }


ASR_CANDIDATES = [
    CapabilityCandidate(
        "funasr", "FunASR 独立进程", "独立 funasr 进程（HTTP 9102，默认），需先安装并启动该服务", requires_service="funasr"
    ),
    CapabilityCandidate("doubao", "豆包云端 ASR", "火山一句话识别；需要 DOUBAO_ASR_* 环境变量凭证"),
]
LLM_CANDIDATES = [
    CapabilityCandidate("ark", "火山方舟 Ark（云端）", "默认 LLM，支持工具调用（提醒 / 记忆 / 实验台）"),
    CapabilityCandidate(
        "local",
        "本地 llm-engine（Cactus Needle 2）",
        "实验性：模型较小，不支持工具调用，定时提醒 / 长期记忆会失效；需 llm-engine 服务运行（9104）",
        requires_service="llm-engine",
        experimental=True,
    ),
]
# 本期仅豆包；本地 tts-engine（MOSS-TTS-Nano）接入后在此注册候选即可
TTS_CANDIDATES = [
    CapabilityCandidate("doubao", "豆包语音合成", "火山云端 TTS；需要 DOUBAO_TTS_* 环境变量凭证"),
]

_CANDIDATES = {"asr": ASR_CANDIDATES, "llm": LLM_CANDIDATES, "tts": TTS_CANDIDATES}


def _candidate(cap: str, provider: str) -> CapabilityCandidate:
    for item in _CANDIDATES[cap]:
        if item.id == provider:
            return item
    raise CapabilityError(f"未知的 {cap.upper()} 能力: {provider}")


class RobotCapabilityService:
    """能力状态读取与切换（ASR/LLM 设备级，TTS 仍为 config.yaml 真源）。"""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = config_path
        # TTS 切换仍需锁（config 落盘 + adapter 重建）；ASR 为设备表写入，无需锁
        self._tts_lock = asyncio.Lock()

    # ---------- 状态读取 ----------

    def _load_cfg(self) -> dict[str, Any]:
        return load_config(self._config_path)

    def get_status(self, device_id: str | None = None) -> dict[str, Any]:
        cfg = self._load_cfg()
        return {
            "device_id": device_id,
            "capabilities": {
                "asr": self._asr_status(device_id),
                "llm": self._llm_status(cfg, device_id),
                "tts": self._tts_status(cfg),
            },
            "services": self._service_snapshots(),
            "env_overrides": {key: bool(os.environ.get(key)) for key in ENV_OVERRIDE_KEYS},
        }

    def _asr_status(self, device_id: str | None) -> dict[str, Any]:
        """ASR 为设备级配置：current 来自 device 表（缺省 funasr）。"""
        current = resolve_asr_provider(device_id)
        warning = None
        if current == "funasr" and not self._service_running("funasr"):
            warning = "funasr 未在运行，语音识别将失败；请先在「独立服务管理」中启动"
        return {"current": current, "candidates": [c.to_dict() for c in ASR_CANDIDATES], "warning": warning}

    def _tts_status(self, cfg: dict[str, Any]) -> dict[str, Any]:
        current = str((cfg.get("tts") or {}).get("provider") or "doubao")
        return {"current": current, "candidates": [c.to_dict() for c in TTS_CANDIDATES], "warning": None}

    def _llm_status(self, cfg: dict[str, Any], device_id: str | None) -> dict[str, Any]:
        override = None
        if device_id:
            entry = get_active_llm_model(device_id)
            if entry is not None:
                override = {"active": True, "model": entry.to_dict(mask_key=True)}
        if override is None:
            override = {"active": False, "hint": None if device_id else "未选择当前设备"}

        effective: dict[str, Any] = {}
        try:
            # 与 resolve_llm_config 相同的优先级：设备覆盖 → 系统默认；系统默认读同一份
            # config（self._config_path），保证页面展示与 apply_llm 落盘的内容一致
            if override["active"]:
                resolved = _entry_to_config(get_active_llm_model(device_id))
            else:
                resolved = resolve_system_llm_config(self._load_cfg())
            effective = {
                "protocol": resolved.protocol,
                "base_url": resolved.api_base,
                "model_name": resolved.model,
                "source": resolved.source,
                "display_name": resolved.display_name,
                "api_key_set": bool(str(resolved.api_key or "").strip()),
            }
        except Exception as exc:  # 配置缺 model_name 等；页面展示错误而不是整页失败
            logger.warning("[robot-settings] resolve_llm_config 失败: %s", exc)
            effective = {"error": str(exc)}

        if override["active"]:
            current = "device"
        elif effective.get("error"):
            current = "custom"
        else:
            protocol = _normalized_protocol(effective.get("protocol"))
            if protocol in VOLCENGINE_PROTOCOLS:
                current = "ark"
            elif protocol == "openai" and is_local_llm_url(effective.get("base_url")):
                current = "local"
            else:
                current = "custom"

        warning = None
        if current == "local" and not self._service_running("llm-engine"):
            warning = "llm-engine 未在运行，切换后对话将失败；请先在「独立服务管理」中启动"
        elif current == "ark" and not effective.get("api_key_set"):
            warning = "未配置火山方舟 API Key，云端 LLM 不可用；请在「高级 → 大模型」中设置"

        return {
            "candidates": [c.to_dict() for c in LLM_CANDIDATES],
            "current": current,
            "effective": effective,
            "device_override": override,
            "warning": warning,
        }

    def _service_snapshots(self) -> dict[str, dict[str, Any]]:
        try:
            manager = get_runtime().external_manager
        except Exception:
            manager = None
        out: dict[str, dict[str, Any]] = {}
        if manager is not None:
            for name in ("funasr", "llm-engine", "tts-engine"):
                snap = manager.snapshot(name)
                if snap is not None:
                    out[name] = snap.to_dict()
        return out

    @staticmethod
    def _service_running(name: str) -> bool:
        try:
            snap = get_runtime().external_manager.snapshot(name)
        except Exception:
            return False
        return bool(snap is not None and snap.state.value == "running")

    # ---------- ASR 切换（设备级：写 device 表，动态解析即时生效） ----------

    def apply_asr(self, provider: str, device_id: str | None = None) -> dict[str, Any]:
        _candidate("asr", provider)
        if not device_id:
            raise CapabilityError("未选择当前设备")
        set_asr_provider(device_id, provider)
        logger.info("[robot-settings] ASR 设备级切换生效 device_id=%s provider=%s", device_id, provider)
        return self.get_status(device_id)

    def clear_device_asr_override(self, device_id: str | None) -> dict[str, Any]:
        """重置设备 ASR 为默认 funasr。"""
        if not device_id:
            raise CapabilityError("未选择当前设备")
        set_asr_provider(device_id, "funasr")
        return self.get_status(device_id)

    # ---------- TTS 切换 ----------

    async def apply_tts(self, provider: str, device_id: str | None = None) -> dict[str, Any]:
        _candidate("tts", provider)
        async with self._tts_lock:
            cfg = self._load_cfg()
            old = str((cfg.get("tts") or {}).get("provider") or "doubao")
            if provider == old:
                return self.get_status(device_id)  # 幂等
            if os.environ.get("TTS_PROVIDER"):
                raise CapabilityError(
                    "检测到环境变量 TTS_PROVIDER 覆盖 config.yaml，页面切换不会生效，请先清除该环境变量"
                )
            new_cfg = copy.deepcopy(cfg)
            new_cfg.setdefault("tts", {})["provider"] = provider
            save_config(new_cfg, self._config_path)
            try:
                settings = AppSettings.from_config(new_cfg)
                adapter = await asyncio.to_thread(build_tts_adapter, settings)
            except Exception as exc:
                try:
                    save_config(cfg, self._config_path)
                except Exception:
                    logger.exception("[robot-settings] 回滚 config 失败")
                raise CapabilityError(f"切换失败，已回滚（仍为 {old}）：{exc}") from exc
            TtsService().bind(adapter)
            logger.info("[robot-settings] TTS 切换生效 provider=%s", provider)
            return self.get_status(device_id)

    # ---------- LLM 切换 ----------

    def apply_llm(self, provider: str, device_id: str | None = None) -> dict[str, Any]:
        _candidate("llm", provider)
        cfg = self._load_cfg()
        llm = cfg.setdefault("llm", {})
        if provider == "local":
            # 快照当前 ark 配置，切回时恢复（随 config 持久化，重启后仍可切回）
            protocol = _normalized_protocol(llm.get("protocol"))
            if protocol in VOLCENGINE_PROTOCOLS and not llm.get("ark_base_url"):
                llm["ark_base_url"] = str(llm.get("base_url") or "").strip() or ARK_OPENAI_BASE_URL
                llm["ark_model_name"] = str(llm.get("model_name") or "").strip()
            llm["protocol"] = "openai"
            llm["base_url"] = LLM_ENGINE_BASE_URL
            llm["model_name"] = LLM_ENGINE_MODEL
        else:  # ark
            llm["protocol"] = "ark_responses"
            llm["base_url"] = str(llm.pop("ark_base_url", "") or "").strip() or ARK_OPENAI_BASE_URL
            llm["model_name"] = str(llm.pop("ark_model_name", "") or "").strip() or str(llm.get("model_name") or "")
        save_config(cfg, self._config_path)
        # 清掉 .env 的协议/模型/地址覆盖（否则遮蔽 config.yaml），保留 ARK_API_KEY 密钥
        clear_llm_env(keep_api_key=True)
        logger.info("[robot-settings] LLM 切换生效 provider=%s", provider)
        return self.get_status(device_id)

    # ---------- 设备级 LLM 覆盖 ----------

    def clear_device_llm_override(self, device_id: str | None) -> dict[str, Any]:
        if not device_id:
            raise CapabilityError("未选择当前设备")
        set_active_llm_model(device_id, None)
        return self.get_status(device_id)
