"""机器人能力设置服务：ASR / LLM / TTS 能力选择与热切换。

- ASR：设备级配置（device 表 asr_provider，默认 funasr），写表即生效——
  ``resolve_asr_adapter`` 每次调用动态解析（见 infrastructure/asr/resolve.py）
- LLM：设备级覆盖（llm_models.json active 模型）优先于系统默认（config.yaml llm 段），
  系统级受 .env 覆盖（env 优先），apply_llm 会清掉协议类覆盖
- TTS：config.yaml 真源（默认 moss-tts-nano 独立进程），写 config → 后台重建
  adapter（走 to_thread）→ 成功才 rebind 到 TtsService 单例；构造失败回滚
  config，单例保持旧能力
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deskbot_server.config import load_config, save_config
from deskbot_server.controller.runtime import get_runtime
from deskbot_server.dao.device_mapper import get_asr_param, set_asr_provider, update_asr_param
from deskbot_server.dao.llm_config_store import get_active_llm_model, set_active_llm_model
from deskbot_server.infrastructure.asr.audio_norm import DEFAULT_PCM_SAMPLE_RATE, normalize_test_audio
from deskbot_server.infrastructure.asr.doubao import (
    DOUBAO_ASR_FIELDS,
    STATUS_OK,
    STATUS_SILENCE,
    DoubaoAsrConfig,
    transcribe_doubao_detailed,
)
from deskbot_server.infrastructure.asr.env_store import (
    _is_masked_secret,
    _mask_secret,
    load_doubao_asr_env,
    save_doubao_asr_env,
)
from deskbot_server.infrastructure.asr.funasr_adapter import TRANSCRIBE_TIMEOUT_S
from deskbot_server.infrastructure.asr.protocol import AsrProtocolError, extract_error, parse_transcribe_response
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
from deskbot_server.infrastructure.tts.env_store import read_env_file
from deskbot_server.infrastructure.tts.factory import build_tts_adapter
from deskbot_server.infrastructure.tts.speakers import list_doubao_tts_consumer_speaker_presets
from deskbot_server.model.settings import AppSettings
from deskbot_server.service.camera_face_service import CameraFaceService, build_camera_face_runtime
from deskbot_server.service.tts_service import TtsService
from deskbot_server.utils.audio import pcm_to_wav_bytes

logger = logging.getLogger("deskbot-server")

# 服务根目录（service/），用于定位 externals 下各服务的音色/样本文件
SERVICE_ROOT = Path(__file__).resolve().parents[3]
MOSS_VOICES_FILE = SERVICE_ROOT / "externals" / "moss-tts-nano" / "checkout" / "assets" / "demo.jsonl"
TTS_TEST_DEFAULT_TEXT = "你好，这是语音合成测试。"

# 本地 llm-engine（Cactus Needle 2）端点；服务端不校验 Authorization、不支持 stream
LLM_ENGINE_BASE_URL = "http://127.0.0.1:9104/v1"
LLM_ENGINE_MODEL = "cactus-needle-2"

# 页面提示用环境变量清单（存在即表示 config.yaml 被覆盖；ASR 已设备级化，不在其中）
ENV_OVERRIDE_KEYS = ("TTS_PROVIDER", "LLM_PROTOCOL", "LLM_MODEL", "LLM_BASE_URL")

# ASR 测试默认音频样本（与 external/manager.DEFAULT_ASR_TEST_AUDIO 同路径）
DEFAULT_ASR_TEST_AUDIO = SERVICE_ROOT / "data" / "test" / "asr.wav"
# DOUBAO_ASR_FIELDS 常量见 infrastructure/asr/doubao.py（resolve 与蓝图共用，避免循环依赖）


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
    CapabilityCandidate(
        "doubao", "豆包云端 ASR 2.0", "新版控制台 Seed-ASR（x-api-key 鉴权）；可在「配置」中为该设备填写凭证（未填写时回落环境变量）"
    ),
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
TTS_CANDIDATES = [
    CapabilityCandidate(
        "moss-tts-nano",
        "本地 moss-tts-nano",
        "独立 MOSS-TTS-Nano 进程（HTTP 9101，默认），需先安装并启动该服务",
        requires_service="moss-tts-nano",
    ),
    CapabilityCandidate("doubao", "豆包语音合成", "火山云端 TTS；需要 DOUBAO_TTS_* 环境变量凭证"),
]
FACE_CANDIDATES = [
    CapabilityCandidate("none", "不识别", "关闭人脸识别：不检测人脸、不跟踪、不注册档案（推流画面照常）"),
    CapabilityCandidate(
        "insightface",
        "独立服务 insightface",
        "InsightFace + MediaPipe 独立进程（HTTP 9103，多 worker 并行），需先安装并启动该服务",
        requires_service="insightface-engine",
    ),
]

_CANDIDATES = {"asr": ASR_CANDIDATES, "llm": LLM_CANDIDATES, "tts": TTS_CANDIDATES, "face": FACE_CANDIDATES}


def _candidate(cap: str, provider: str) -> CapabilityCandidate:
    for item in _CANDIDATES[cap]:
        if item.id == provider:
            return item
    raise CapabilityError(f"未知的 {cap.upper()} 能力: {provider}")


class RobotCapabilityService:
    """能力状态读取与切换（ASR/LLM 设备级，TTS 仍为 config.yaml 真源）。"""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = config_path
        # TTS/人脸切换仍需锁（config 落盘 + 重建）；ASR 为设备表写入，无需锁
        self._tts_lock = asyncio.Lock()
        self._face_lock = threading.Lock()  # apply_face 为同步方法，用线程锁

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
                "face": self._face_status(cfg),
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
        return {
            "current": current,
            "candidates": [c.to_dict() for c in ASR_CANDIDATES],
            "warning": warning,
            "device_params": {"configured": bool(get_asr_param(device_id)) if device_id else False},
        }

    def _tts_status(self, cfg: dict[str, Any]) -> dict[str, Any]:
        current = str((cfg.get("tts") or {}).get("provider") or "moss-tts-nano")
        warning = None
        if current == "moss-tts-nano" and not self._service_running("moss-tts-nano"):
            warning = "moss-tts-nano 未在运行，语音合成将失败；请先在「独立服务管理」中启动"
        return {"current": current, "candidates": [c.to_dict() for c in TTS_CANDIDATES], "warning": warning}

    def _face_status(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """人脸识别为 config.yaml 真源（camera_face.mode: none | insightface）。"""
        current = str((cfg.get("camera_face") or {}).get("mode") or "insightface")
        warning = None
        if current == "insightface" and not self._service_running("insightface-engine"):
            warning = "insightface-engine 未在运行，人脸识别将失败；请先在「独立服务管理」中启动"
        return {"current": current, "candidates": [c.to_dict() for c in FACE_CANDIDATES], "warning": warning}

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
            for name in ("funasr", "llm-engine", "moss-tts-nano"):
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
        """重置设备 ASR 为默认 funasr，并清空该设备 asr_param（回到全局配置）。"""
        if not device_id:
            raise CapabilityError("未选择当前设备")
        set_asr_provider(device_id, "funasr")
        update_asr_param(device_id, None)
        logger.info("[robot-settings] ASR 设备覆盖已清除 device_id=%s（provider=funasr，asr_param 清空）", device_id)
        return self.get_status(device_id)

    # ---------- ASR 配置 / 测试（对话框） ----------

    def asr_config_info(self, device_id: str | None = None) -> dict[str, Any]:
        """ASR 配置对话框元信息：默认音频样本、funasr 端点、豆包当前值（api_key 掩码）。

        读链：设备 asr_param > 全局 env > 默认（url/uid/resource_id 有兜底）。
        """
        audio: dict[str, Any] = {"path": "data/test/asr.wav", "exists": False}
        if DEFAULT_ASR_TEST_AUDIO.is_file():
            audio = {
                "path": "data/test/asr.wav",
                "exists": True,
                "size": DEFAULT_ASR_TEST_AUDIO.stat().st_size,
            }
            try:
                import wave

                with wave.open(str(DEFAULT_ASR_TEST_AUDIO), "rb") as wav:
                    audio["sample_rate"] = wav.getframerate()
                    audio["channels"] = wav.getnchannels()
                    audio["duration_s"] = round(wav.getnframes() / max(1, wav.getframerate()), 2)
            except Exception:
                pass  # 元信息展示失败不阻塞测试
        env = load_doubao_asr_env()
        cfg = self._load_cfg()
        params = get_asr_param(device_id) if device_id else {}
        doubao_params = params.get("doubao") if isinstance(params.get("doubao"), dict) else {}
        funasr_params = params.get("funasr") if isinstance(params.get("funasr"), dict) else {}

        def _doubao_value(key: str) -> str:
            return str(doubao_params.get(key) or "").strip() or env["DOUBAO_ASR_" + key.upper()]

        api_key = _doubao_value("api_key")
        return {
            "default_audio": audio,
            "funasr_url": str(funasr_params.get("url") or "").strip()
            or str((cfg.get("asr") or {}).get("external_url") or "http://127.0.0.1:9102"),
            "doubao": {
                "api_key": _mask_secret(api_key),
                "api_key_set": bool(api_key.strip()),
                "resource_id": _doubao_value("resource_id"),
                "uid": _doubao_value("uid"),
                "url": _doubao_value("url"),
            },
        }

    def save_device_asr_config(self, device_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        """保存设备级 ASR 配置到 device 表 asr_param（JSON），不再写全局 .env。

        payload 嵌套格式 ``{"funasr": {"url"}, "doubao": {api_key/resource_id/uid/url}}``；
        旧平铺 doubao 字段（脚本兼容）也会归一。空值/掩码按回填链保留：
        payload > 设备已有 asr_param > 全局 env；解析后全空 → 清空 asr_param。
        """
        if not device_id:
            raise CapabilityError("未选择当前设备，无法保存 ASR 配置")
        if isinstance(payload.get("funasr"), dict) or isinstance(payload.get("doubao"), dict):
            nested = payload
        else:
            logger.warning("[robot-settings] 旧平铺 ASR 配置 payload，按 doubao 字段归一（Deprecated）")
            nested = {"doubao": payload}

        existing = get_asr_param(device_id)
        raw_env = read_env_file()  # .env 真实值（不经默认兜底，避免默认值污染落库）
        existing_doubao = existing.get("doubao") if isinstance(existing.get("doubao"), dict) else {}
        existing_funasr = existing.get("funasr") if isinstance(existing.get("funasr"), dict) else {}

        doubao_in = nested.get("doubao") if isinstance(nested.get("doubao"), dict) else {}
        doubao_out: dict[str, str] = {}
        for key in DOUBAO_ASR_FIELDS:
            raw = str(doubao_in.get(key) or "").strip()
            if key == "api_key" and _is_masked_secret(raw):
                raw = ""  # 掩码占位 → 回填链取已有值
            if not raw:
                raw = str(existing_doubao.get(key) or "").strip()
            if not raw:
                raw = (raw_env.get("DOUBAO_ASR_" + key.upper()) or "").strip()
            if raw:
                doubao_out[key] = raw

        funasr_in = nested.get("funasr") if isinstance(nested.get("funasr"), dict) else {}
        funasr_out: dict[str, str] = {}
        raw_url = str(funasr_in.get("url") or "").strip()
        if not raw_url:
            raw_url = str(existing_funasr.get("url") or "").strip()
        if raw_url:
            funasr_out["url"] = raw_url  # funasr.url 无 env 可回填：留空则不落键，运行时回落 config.yaml

        out: dict[str, dict[str, str]] = {}
        if funasr_out:
            out["funasr"] = funasr_out
        if doubao_out:
            out["doubao"] = doubao_out
        update_asr_param(device_id, json.dumps(out, ensure_ascii=False) if out else None)
        logger.info(
            "[robot-settings] 设备级 ASR 配置已保存 device_id=%s fields=%s",
            device_id, sorted({k for d in out.values() for k in d}),
        )
        return self.asr_config_info(device_id)

    def save_doubao_asr_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Deprecated: 前端已改为设备级 save_device_asr_config；本方法写全局 .env，仅脚本/运维兜底。"""
        save_doubao_asr_env({k: str(payload.get(k) or "") for k in DOUBAO_ASR_FIELDS})
        logger.warning("[robot-settings] Deprecated: save_doubao_asr_config 写全局 .env，请改用设备级保存")
        return self.asr_config_info()

    async def asr_test(
        self,
        provider: str,
        audio_bytes: bytes | None,
        *,
        use_default: bool = False,
        doubao_overrides: dict[str, str] | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """按指定 provider 测试转写（不改配置、不落盘）。

        音频源：上传 bytes > 默认样本（data/test/asr.wav）；都缺 → CapabilityError。
        归一化为 16k 单声道 PCM；funasr 直连 external_url 抓 HTTP 状态码，
        doubao 用覆盖字段（空/掩码回落 env）构造临时配置。
        返回 ``{ok:true, success, provider, http_code, elapsed_ms, text, error, business_code,
        used_default, sample_rate}``——失败也走 200 + success:false，便于前端渲染详情。
        """
        _candidate("asr", provider)
        raw = audio_bytes
        used_default = False
        if not raw:
            if not use_default or not DEFAULT_ASR_TEST_AUDIO.is_file():
                raise CapabilityError("未提供测试音频：请选择本地音频文件或使用默认音频")
            raw = DEFAULT_ASR_TEST_AUDIO.read_bytes()
            used_default = True
        try:
            pcm, sample_rate = normalize_test_audio(raw)
        except ValueError as exc:
            raise CapabilityError(str(exc)) from exc

        t0 = time.monotonic()
        if provider == "funasr":
            result = await self._funasr_test(pcm, sample_rate, device_id)
        else:
            result = await self._doubao_test(pcm, sample_rate, doubao_overrides or {}, device_id)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result.update(
            {
                "ok": True,
                "provider": provider,
                "elapsed_ms": elapsed_ms,
                "used_default": used_default,
                "sample_rate": DEFAULT_PCM_SAMPLE_RATE,
            }
        )
        logger.info(
            "[robot-settings] ASR 测试 provider=%s success=%s http_code=%s elapsed_ms=%d audio_bytes=%d",
            provider,
            result.get("success"),
            result.get("http_code"),
            elapsed_ms,
            len(raw),
        )
        return result

    async def _funasr_test(self, pcm: bytes, sample_rate: int, device_id: str | None = None) -> dict[str, Any]:
        """直连 funasr 引擎 /transcribe（url 优先设备 asr_param），捕获 HTTP 状态码与协议错误。"""
        import urllib.error
        import urllib.request

        settings = AppSettings.from_config(self._load_cfg())
        params = get_asr_param(device_id) if device_id else {}
        funasr_params = params.get("funasr") if isinstance(params.get("funasr"), dict) else {}
        base_url = str(funasr_params.get("url") or "").strip() or settings.asr.external_url
        url = f"{base_url.rstrip('/')}/transcribe"
        req = urllib.request.Request(
            url,
            data=pcm,
            method="POST",
            headers={"Content-Type": "application/octet-stream", "X-Sample-Rate": str(sample_rate)},
        )
        try:
            with urllib.request.urlopen(req, timeout=TRANSCRIBE_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                http_code = resp.getcode()
        except urllib.error.HTTPError as exc:  # HTTPError 是 URLError 子类，须在前
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            except Exception:
                payload = None
            http_code = exc.code
        except (urllib.error.URLError, OSError) as exc:
            return {
                "success": False,
                "http_code": 0,
                "text": "",
                "error": f"funasr 引擎不可达: {exc.reason if isinstance(exc, urllib.error.URLError) else exc}",
                "business_code": None,
            }
        err = extract_error(payload) if isinstance(payload, dict) else None
        if err:
            code, message = err
            return {"success": False, "http_code": http_code, "text": "", "error": message, "business_code": code}
        try:
            parsed = parse_transcribe_response(payload)
        except AsrProtocolError as exc:
            return {"success": False, "http_code": http_code, "text": "", "error": exc.message, "business_code": exc.code}
        return {"success": True, "http_code": http_code, "text": parsed["text"], "error": "", "business_code": 0}

    async def _doubao_test(
        self, pcm: bytes, sample_rate: int, overrides: dict[str, str], device_id: str | None = None
    ) -> dict[str, Any]:
        """按"设备实际生效配置"测试豆包 2.0：覆盖字段 > 设备 asr_param > env，错误进结果不抛。"""
        env = load_doubao_asr_env()
        params = get_asr_param(device_id) if device_id else {}
        dev_params = params.get("doubao") if isinstance(params.get("doubao"), dict) else {}

        def _pick(field: str) -> str:
            val = (overrides.get(field) or "").strip()
            if field == "api_key" and _is_masked_secret(val):
                val = ""
            if not val:
                val = str(dev_params.get(field) or "").strip()
            if not val:
                val = (env.get("DOUBAO_ASR_" + field.upper()) or "").strip()
            return val

        cfg = DoubaoAsrConfig(
            api_key=_pick("api_key"),
            resource_id=_pick("resource_id"),
            uid=_pick("uid"),
            url=_pick("url"),
        )
        try:
            cfg.validate()
        except RuntimeError as exc:
            return {"success": False, "http_code": 0, "text": "", "error": str(exc), "business_code": None}
        try:
            detail = await transcribe_doubao_detailed(pcm, sample_rate, cfg)
        except RuntimeError as exc:  # 配置缺失 / 时长超上限 / 响应异常
            return {"success": False, "http_code": 0, "text": "", "error": str(exc), "business_code": None}
        ok = detail["http_code"] == 200 and detail["business_code"] in (STATUS_OK, STATUS_SILENCE)
        return {
            "success": ok,
            "http_code": detail["http_code"],
            "text": detail["text"] if ok else "",
            "error": detail["message"] if not ok else "",
            "business_code": detail["business_code"],
        }

    # ---------- TTS 切换 ----------

    async def apply_tts(
        self, provider: str, device_id: str | None = None, voice_id: str | None = None
    ) -> dict[str, Any]:
        """切换 TTS provider，可选同时设置音色。

        voice_id 按 provider 落不同键：moss-tts-nano → ``tts.demo_id``（demo-N 行序）；
        doubao → ``tts.doubao_speaker``（火山音色 ID）。不带 voice_id 保留旧值。
        """
        _candidate("tts", provider)
        async with self._tts_lock:
            cfg = self._load_cfg()
            old = str((cfg.get("tts") or {}).get("provider") or "moss-tts-nano")
            voice_id = (voice_id or "").strip() or None
            if provider == old and voice_id is None:
                return self.get_status(device_id)  # 幂等
            if os.environ.get("TTS_PROVIDER"):
                raise CapabilityError(
                    "检测到环境变量 TTS_PROVIDER 覆盖 config.yaml，页面切换不会生效，请先清除该环境变量"
                )
            new_cfg = copy.deepcopy(cfg)
            tts = new_cfg.setdefault("tts", {})
            tts["provider"] = provider
            if voice_id:
                if provider == "moss-tts-nano":
                    tts["demo_id"] = voice_id
                else:  # doubao
                    tts["doubao_speaker"] = voice_id
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

    # ---------- 人脸识别 ----------

    def apply_face(self, mode: str) -> dict[str, Any]:
        """切换人脸识别能力：none=不识别；insightface=外部独立服务（config.yaml 真源）。

        写 config → 重建 CameraFaceRuntime 并 re-configure CameraFaceService 单例；
        重建失败回滚 config，单例保持旧能力。
        """
        _candidate("face", mode)
        with self._face_lock:
            cfg = self._load_cfg()
            old = str((cfg.get("camera_face") or {}).get("mode") or "insightface")
            if mode == old:
                return self.get_status(None)  # 幂等
            new_cfg = copy.deepcopy(cfg)
            new_cfg.setdefault("camera_face", {})["mode"] = mode
            save_config(new_cfg, self._config_path)
            try:
                runtime = build_camera_face_runtime(new_cfg)
                CameraFaceService().configure(runtime)
            except Exception as exc:
                try:
                    save_config(cfg, self._config_path)
                except Exception:
                    logger.exception("[robot-settings] 回滚 config 失败")
                raise CapabilityError(f"切换失败，已回滚（仍为 {old}）：{exc}") from exc
            logger.info("[robot-settings] 人脸识别切换生效 mode=%s", mode)
            return self.get_status(None)

    # ---------- TTS 测试（不落盘、不 rebind 单例） ----------

    def tts_test_info(self) -> dict[str, Any]:
        """测试对话框元信息：默认文本、moss 音色列表（demo.jsonl 行序 demo-N）、
        豆包音色预设列表（data/doubao_tts_speakers.json 消费级）与当前音色。"""
        voices: list[dict[str, str]] = []
        try:
            if MOSS_VOICES_FILE.is_file():
                for i, line in enumerate(MOSS_VOICES_FILE.read_text(encoding="utf-8").splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        name = str(json.loads(line).get("name") or f"demo-{i + 1}")
                    except Exception:
                        name = f"demo-{i + 1}"
                    voices.append({"id": f"demo-{i + 1}", "name": name})
        except Exception:
            logger.exception("[robot-settings] 读取 moss 音色文件失败")
        cfg = self._load_cfg()
        tts = cfg.get("tts") or {}
        doubao_speaker = str(tts.get("doubao_speaker") or os.environ.get("DOUBAO_TTS_SPEAKER") or "").strip()
        doubao_voices = [
            {"id": p["id"], "label": p["label"], "resource_id": p.get("resource_id") or ""}
            for p in list_doubao_tts_consumer_speaker_presets()
        ]
        return {
            "text": TTS_TEST_DEFAULT_TEXT,
            "voices": voices,
            "demo_id": str(tts.get("demo_id") or "demo-1"),
            "doubao_voices": doubao_voices,
            "doubao_speaker": doubao_speaker,
        }

    async def tts_test(
        self, provider: str, text: str, voice_id: str | None = None
    ) -> dict[str, Any]:
        """按指定 provider 合成测试（临时 adapter，不改 config、不 rebind 单例）。

        返回 WAV base64 + 采样率 + 音素分片概要；voice_id 语义同 apply_tts：
        moss → demo_id，doubao → doubao_speaker。
        """
        _candidate("tts", provider)
        text = (text or "").strip()
        if not text:
            raise CapabilityError("请输入测试文本")
        cfg = copy.deepcopy(self._load_cfg())
        tts = cfg.setdefault("tts", {})
        tts["provider"] = provider
        if (voice_id or "").strip():
            if provider == "moss-tts-nano":
                tts["demo_id"] = (voice_id or "").strip()
            else:  # doubao
                tts["doubao_speaker"] = (voice_id or "").strip()
        settings = AppSettings.from_config(cfg)
        adapter = build_tts_adapter(settings)
        t0 = time.monotonic()
        sr, segs = await adapter.synthesize_phoneme_segments(text)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        pcm = b"".join(bytes(s.pcm or b"") for s in segs)
        if not pcm:
            raise CapabilityError(f"{provider} 合成无 PCM 输出")
        wav = pcm_to_wav_bytes(pcm, sr)
        logger.info(
            "[robot-settings] TTS 测试 provider=%s voice_id=%s elapsed_ms=%d pcm=%d text=%r",
            provider,
            voice_id,
            elapsed_ms,
            len(pcm),
            text[:40],
        )
        return {
            "provider": provider,
            "sample_rate": int(sr),
            "wav_base64": base64.b64encode(wav).decode("ascii"),
            "pcm_total_bytes": len(pcm),
            "segments": [
                {"phoneme": s.phoneme, "ms": s.ms, "pcm_bytes": len(s.pcm or b"")} for s in segs
            ],
            "elapsed_ms": elapsed_ms,
        }

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
