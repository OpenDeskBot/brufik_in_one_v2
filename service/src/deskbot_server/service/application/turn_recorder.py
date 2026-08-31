"""调试模式对话轮次落库（历史对话 tab 数据源）。

开关：设备级 ``devices.record_history``（默认关）；关则整段跳过。
音频/图像文件由各 hook（device_ws_service / chat_flow / camera_face_service）
先落盘到 ``data/device/{device_id}/``，本模块只组装元数据写 ``device_turn`` 表。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from deskbot_server.model.chat import ChatTurnResult
from deskbot_server.utils.async_helpers import run_blocking
from deskbot_server.utils.util import _ms_between

logger = logging.getLogger("deskbot-server")

_ASR_MODEL_BY_PROVIDER = {"doubao": "doubao:bigmodel", "funasr": "funasr"}


def _ms(value: float | None) -> int | None:
    """_ms_between 结果（float 1 位小数）转整型毫秒。"""
    return int(value) if value is not None else None


def _snapshot_llm_model(device_id: str) -> str:
    from deskbot_server.dao.llm_config_store import get_active_llm_model

    try:
        entry = get_active_llm_model(device_id)
        if entry is not None:
            return f"{entry.protocol}:{entry.model_name}"
    except Exception:
        logger.debug("[record] LLM 模型快照失败（忽略）", exc_info=True)
    try:
        cfg = _load_config()
        llm = cfg.get("llm") or {}
        proto = str(llm.get("protocol") or "openai")
        model = str(llm.get("model_name") or "")
        return f"{proto}:{model}" if model else proto
    except Exception:
        return ""


def _snapshot_tts_model(tts_cfg: dict[str, Any] | None) -> str:
    try:
        cfg = tts_cfg if tts_cfg is not None else (_load_config().get("tts") or {})
        provider = str(cfg.get("provider") or "").strip()
        voice = str(cfg.get("doubao_speaker") or cfg.get("demo_id") or "").strip()
        return f"{provider}:{voice}" if voice else provider
    except Exception:
        return ""


def _snapshot_fr_model() -> str:
    try:
        cfg = _load_config().get("camera_face") or {}
        mode = str(cfg.get("mode") or "").strip() or "none"
        url = str(cfg.get("external_url") or "").strip()
        return f"{mode}:{url}" if url else mode
    except Exception:
        return ""


def _load_config() -> dict[str, Any]:
    from deskbot_server.config import load_config

    return load_config()


def _snapshot_models(device_id: str, tts_cfg: dict[str, Any] | None) -> dict[str, str]:
    """asr / llm / tts / fr / vpr 模型名快照（vpr 预留）。"""
    from deskbot_server.infrastructure.asr.resolve import resolve_asr_provider

    provider = ""
    try:
        provider = resolve_asr_provider(device_id)
    except Exception:
        logger.debug("[record] ASR provider 解析失败（忽略）", exc_info=True)
    return {
        "asr_model": _ASR_MODEL_BY_PROVIDER.get(provider, provider or ""),
        "llm_model": _snapshot_llm_model(device_id),
        "tts_model": _snapshot_tts_model(tts_cfg),
        "fr_model": _snapshot_fr_model(),
        "vpr_model": "",
    }


def _snapshot_fr_capture(device_id: str) -> dict[str, Any] | None:
    from deskbot_server.service.camera_face_service import get_recent_fr_capture

    try:
        return get_recent_fr_capture(device_id)
    except Exception:
        logger.debug("[record] 人脸识别快照失败（忽略）", exc_info=True)
        return None


async def persist_turn(
    *,
    device_id: str,
    session_id: str | None,
    source: str,
    user_text: str,
    result: ChatTurnResult,
    t_asr_start: float | None = None,
    t_asr_text: float | None = None,
    tts_cfg: dict[str, Any] | None = None,
) -> None:
    """将一轮对话写入 device_turn（调试模式开关门控，失败仅告警不阻断对话）。"""
    if not device_id or not session_id:
        return
    from deskbot_server.dao.device_mapper import get_record_history

    try:
        if not get_record_history(device_id):
            return
    except Exception:
        logger.debug("[record] 开关查询失败（忽略）", exc_info=True)
        return

    try:
        from deskbot_server.db.models import _new_id
        from deskbot_server.dao.device_turn_mapper import count_turns, insert_turn

        models = _snapshot_models(device_id, tts_cfg)
        fr = _snapshot_fr_capture(device_id)

        tools_json = None
        if result.tools:
            tools_slim = []
            for t in result.tools:
                if not isinstance(t, dict):
                    continue
                tools_slim.append(
                    {
                        "tool": t.get("tool") or t.get("name") or "",
                        "args": t.get("args") or t.get("params") or {},
                    }
                )
            tools_json = json.dumps(tools_slim, ensure_ascii=False)

        seq = await run_blocking(count_turns, session_id)
        await run_blocking(
            insert_turn,
            _new_id(),
            session_id,
            device_id,
            seq + 1,
            source,
            user_text=str(user_text or "").strip() or None,
            user_audio=result.user_audio,
            user_audio_ms=result.user_audio_ms,
            asr_ms=_ms(_ms_between(t_asr_start, t_asr_text)),
            asr_model=models["asr_model"] or None,
            fr_image=fr.get("image") if fr else None,
            fr_ms=fr.get("fr_ms") if fr else None,
            fr_result=json.dumps(fr["faces"], ensure_ascii=False) if fr and fr.get("faces") else None,
            fr_model=models["fr_model"] or None,
            vpr_model=models["vpr_model"] or None,
            bot_text=(result.llm_text or "").strip() or None,
            bot_audio=result.bot_audio,
            bot_audio_ms=result.bot_audio_ms,
            llm_ms=_ms(_ms_between(t_asr_text, result.t_llm_end)),
            llm_model=models["llm_model"] or None,
            tools=tools_json,
            tts_ms=_ms(_ms_between(result.t_llm_end, result.t_tts_synth_end)),
            tts_model=models["tts_model"] or None,
            system_prompt=(result.system_prompt or "").strip() or None,
            status=result.status or "ok",
            error=result.error,
        )
        logger.info(
            "[record] 轮次落库 device_id=%s session=%s seq=%d source=%s status=%s",
            device_id, session_id, seq + 1, source, result.status,
        )
    except Exception:
        logger.exception("[record] 轮次落库失败 device_id=%s session=%s", device_id, session_id)
