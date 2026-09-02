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

# 本地 llama-server 的 n_ctx 被模型 n_ctx_train 钉死在 8192（-c 再大也会被 clamp，只有 rope-scaling 能超过），
# 请求超出即 400 exceed_context_size_error。预算预留输出与 JSON 重试轮的 token 空间，超出按估算裁剪历史。
_LOCAL_CTX_BUDGET_TOKENS = 7000
_MAX_EXCEED_RETRY = 3
_EXCEED_CTX_MARKERS = ("exceeds the available context size", "exceed_context_size_error")


def _is_exceed_ctx_error(exc: BaseException) -> bool:
    """识别 llama-server 的上下文超限 400 错误。"""
    text = str(exc)
    return any(marker in text for marker in _EXCEED_CTX_MARKERS)


def _estimate_tokens(text: str) -> int:
    """本地引擎无 tokenizer，粗略估算（宁多勿少）：CJK 约 1.1 token/字，其余按 3.2 字符/token。"""
    if not text:
        return 0
    cjk = sum(
        1
        for ch in text
        if "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯"
    )
    return int(cjk * 1.1) + int((len(text) - cjk) / 3.2) + 1


def _trim_history_for_budget(
    history: list[dict[str, str]], *, system_content: str, user_content: str, extra: list[dict[str, str]]
) -> list[dict[str, str]]:
    """本地引擎：按预估 token 预算从最旧开始裁剪历史，至少保留最近一条。"""
    if not history:
        return history
    fixed = _estimate_tokens(system_content) + _estimate_tokens(user_content) + 8
    fixed += sum(_estimate_tokens(str(m.get("content") or "")) + 4 for m in extra)
    budget = _LOCAL_CTX_BUDGET_TOKENS - fixed
    keep: list[dict[str, str]] = []
    total = 0
    for msg in reversed(history):
        cost = _estimate_tokens(str(msg.get("content") or "")) + 4
        if keep and total + cost > budget:
            break
        keep.append(msg)
        total += cost
    keep.reverse()
    return keep


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
        local_engine = is_local_llm_url(llm_cfg.api_base)
        # 火山 ark_responses 流式在工具/联网阶段常长时间无 text delta，语音对话改非流式更稳。
        # 本地引擎（llm-minicpm / llm-qwen 等）不支持 stream，同样强制非流式（TTS 走完整响应后预取）。
        use_stream_tts = bool(on_tts_ready) and llm_cfg.protocol != "ark_responses" and not local_engine

        system_content = self._build_system_prompt(device_id=device_id)
        if on_system_prompt is not None:
            try:
                on_system_prompt(system_content)
            except Exception:
                logger.debug("[LLM] on_system_prompt 回调异常（忽略）", exc_info=True)
        user_content = build_llm_user_message(user_text, device_id=device_id, device_context=device_context)
        history = list(history_messages or [])
        extra = list(extra_messages or [])
        if local_engine and history:
            trimmed = _trim_history_for_budget(
                history, system_content=system_content, user_content=user_content, extra=extra
            )
            if len(trimmed) < len(history):
                logger.info(
                    "[LLM] 本地引擎上下文预算裁剪 device_id=%s history=%d→%d 条",
                    device_id,
                    len(history),
                    len(trimmed),
                )
            history = trimmed

        def _assemble(hist: list[dict[str, str]]) -> list[dict[str, str]]:
            msgs: list[dict[str, str]] = [{"role": "system", "content": system_content}]
            if hist:
                msgs.extend(hist)
            msgs.append({"role": "user", "content": user_content})
            if extra:
                msgs.extend(extra)
            return msgs

        messages = _assemble(history)

        async def _chat(
            msgs: list[dict[str, str]],
            *,
            json_mode: bool = True,
            stream_tts: bool = False,
            first_token_timeout: float | None = None,
            tail: list[dict[str, str]] | None = None,
        ) -> str:
            # 本地引擎：400 上下文超限时裁掉最旧一半历史重建请求重试（tail 为追加在消息尾部的固定内容）
            cur_history = list(history)
            attempts = 0
            while True:
                try:
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
                except RuntimeError as exc:
                    if (
                        not local_engine
                        or not cur_history
                        or attempts >= _MAX_EXCEED_RETRY
                        or not _is_exceed_ctx_error(exc)
                    ):
                        raise
                    attempts += 1
                    prev_len = len(cur_history)
                    cur_history = cur_history[prev_len // 2 :]
                    rebuilt = _assemble(cur_history)
                    if tail:
                        rebuilt.extend(tail)
                    logger.info(
                        "[LLM] 本地引擎上下文超限，裁剪历史重试 device_id=%s history=%d→%d attempt=%d",
                        device_id,
                        prev_len,
                        len(cur_history),
                        attempts,
                    )
                    msgs = rebuilt

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
                retry_tail = [
                    {"role": "assistant", "content": answer},
                    {
                        "role": "user",
                        "content": (
                            "上轮输出不是合法 JSON。请仅输出一个 JSON 对象（不要 markdown 代码围栏、不要解释），"
                            "格式含 need_reply、tts、moves、anims、tools 等字段。"
                        ),
                    },
                ]
                answer = await _chat(
                    list(messages) + list(retry_tail), stream_tts=False, first_token_timeout=0, tail=retry_tail
                )
                parsed = parse_llm_reply(answer)
        elif parsed.get("tools") and not (parsed.get("reply") or "").strip():
            logger.info("[LLM] tools 轮无 tts，跳过过渡语重试 device_id=%s tools=%s", device_id, parsed.get("tools"))
        if not use_stream_tts:
            await _prefetch_tts(parsed)
        return answer
