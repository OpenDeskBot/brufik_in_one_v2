from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from deskbot_server.infrastructure.llm.runtime import chat_acompletion, is_local_llm_url, resolve_llm_config
from deskbot_server.infrastructure.llm.utils import (
    build_llm_system_prompt,
    build_llm_user_message,
    parse_llm_reply,
)
from deskbot_server.model.settings import AppSettings

logger = logging.getLogger("deskbot-server")

_DEVICE_LLM_LOCKS: dict[str, asyncio.Lock] = {}


def _device_llm_lock(device_id: str | None) -> asyncio.Lock:
    key = str(device_id or "__system__")
    lock = _DEVICE_LLM_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DEVICE_LLM_LOCKS[key] = lock
    return lock


def _wrap_plain_text_llm_answer(text: str) -> str | None:
    """DeepSeek 等模型偶发纯文本回复；包装成约定 JSON，避免二次请求超时。"""
    plain = (text or "").strip()
    if not plain or len(plain) > 800:
        return None
    if plain.startswith("{") or plain.startswith("["):
        return None
    return json.dumps({"need_reply": True, "tts": plain, "moves": [], "anims": [], "tools": []}, ensure_ascii=False)


class OpenAiLlmAdapter:
    """OpenAI-compatible 适配器：支持设备级模型配置，未设置时回退系统默认。"""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._default_system_prompt = settings.llm.system_prompt or (
            '你是中文语音助手，请简洁回答。每次只输出 JSON：{"tts":"…","servo":[]}。'
        )

    def _resolve_system_prompt(self, *, device_id: str | None = None) -> str:
        from deskbot_server.utils.device_data import load_llm_system_prompt

        return load_llm_system_prompt(device_id) or self._default_system_prompt

    def _build_system_prompt(self, *, device_id: str | None = None) -> str:
        base = self._resolve_system_prompt(device_id=device_id)
        return build_llm_system_prompt(base, device_id=device_id)

    async def complete(
        self,
        user_text: str,
        *,
        device_context: str | None = None,
        device_id: str | None = None,
        history_messages: list[dict[str, str]] | None = None,
        extra_messages: list[dict[str, str]] | None = None,
        on_tts_ready: Callable[[str], Awaitable[None]] | None = None,
        on_system_prompt: Callable[[str], None] | None = None,
    ) -> str:
        async with _device_llm_lock(device_id):
            return await self._complete_locked(
                user_text,
                device_context=device_context,
                device_id=device_id,
                history_messages=history_messages,
                extra_messages=extra_messages,
                on_tts_ready=on_tts_ready,
                on_system_prompt=on_system_prompt,
            )

    async def _complete_locked(
        self,
        user_text: str,
        *,
        device_context: str | None = None,
        device_id: str | None = None,
        history_messages: list[dict[str, str]] | None = None,
        extra_messages: list[dict[str, str]] | None = None,
        on_tts_ready: Callable[[str], Awaitable[None]] | None = None,
        on_system_prompt: Callable[[str], None] | None = None,
    ) -> str:
        llm_cfg = resolve_llm_config(device_id)
        # 火山 ark_responses 流式在工具/联网阶段常长时间无 text delta，语音对话改非流式更稳。
        # 本地引擎（llm-engine）不支持 stream，同样强制非流式（TTS 走完整响应后预取）。
        use_stream_tts = (
            bool(on_tts_ready) and llm_cfg.protocol != "ark_responses" and not is_local_llm_url(llm_cfg.api_base)
        )

        system_content = self._build_system_prompt(device_id=device_id)
        if on_system_prompt is not None:
            try:
                on_system_prompt(system_content)
            except Exception:
                logger.debug("[LLM] on_system_prompt 回调异常（忽略）", exc_info=True)
        user_content = build_llm_user_message(user_text, device_id=device_id, device_context=device_context)
        messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
        if history_messages:
            messages.extend(history_messages)
        messages.append({"role": "user", "content": user_content})
        if extra_messages:
            messages.extend(extra_messages)

        async def _chat(
            msgs: list[dict[str, str]],
            *,
            json_mode: bool = True,
            stream_tts: bool = False,
            first_token_timeout: float | None = None,
        ) -> str:
            content, _meta = await chat_acompletion(
                msgs,
                device_id=device_id,
                temperature=0.7,
                json_mode=json_mode,
                stream=stream_tts,
                on_tts_ready=on_tts_ready if stream_tts else None,
                first_token_timeout=first_token_timeout,
            )
            return content

        async def _prefetch_tts(parsed: dict) -> None:
            if not on_tts_ready:
                return
            tts = str(parsed.get("reply") or "").strip()
            if tts:
                await on_tts_ready(tts)

        answer = await _chat(messages, stream_tts=use_stream_tts)
        parsed = parse_llm_reply(answer)
        if not parsed.get("json_ok"):
            wrapped = _wrap_plain_text_llm_answer(answer)
            if wrapped:
                logger.info(
                    "[LLM] 首轮输出为纯文本，已包装为 JSON device_id=%s preview=%r", device_id, (answer or "")[:120]
                )
                answer = wrapped
                parsed = parse_llm_reply(answer)
            else:
                logger.warning("[LLM] 首轮输出非 JSON，重试 device_id=%s preview=%r", device_id, (answer or "")[:120])
                retry_messages = list(messages)
                retry_messages.append({"role": "assistant", "content": answer})
                retry_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上轮输出不是合法 JSON。请仅输出一个 JSON 对象（不要 markdown 代码围栏、不要解释），"
                            "格式含 need_reply、tts、moves、anims、tools 等字段。"
                        ),
                    }
                )
                answer = await _chat(retry_messages, stream_tts=False, first_token_timeout=0)
                parsed = parse_llm_reply(answer)
        elif parsed.get("tools") and not (parsed.get("reply") or "").strip():
            logger.info("[LLM] tools 轮无 tts，跳过过渡语重试 device_id=%s tools=%s", device_id, parsed.get("tools"))
        if not use_stream_tts:
            await _prefetch_tts(parsed)
        return answer
