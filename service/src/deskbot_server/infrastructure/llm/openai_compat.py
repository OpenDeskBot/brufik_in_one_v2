from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from deskbot_server.infrastructure.llm.runtime import (
    chat_acompletion,
    is_local_llm_url,
    resolve_llm_config,
    tool_acompletion,
)
from deskbot_server.infrastructure.llm.utils import (
    build_llm_system_prompt,
    build_llm_user_message,
    estimate_text_tokens,
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


def _trim_history_for_budget(
    history: list[dict[str, str]], *, system_content: str, user_content: str, extra: list[dict[str, str]]
) -> list[dict[str, str]]:
    """本地引擎：按预估 token 预算从最旧开始裁剪历史，至少保留最近一条。"""
    if not history:
        return history
    fixed = estimate_text_tokens(system_content) + estimate_text_tokens(user_content) + 8
    fixed += sum(estimate_text_tokens(str(m.get("content") or "")) + 4 for m in extra)
    budget = _LOCAL_CTX_BUDGET_TOKENS - fixed
    keep: list[dict[str, str]] = []
    total = 0
    for msg in reversed(history):
        cost = estimate_text_tokens(str(msg.get("content") or "")) + 4
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
    return json.dumps(
        {"need_reply": True, "tts": plain, "gesture": [], "expression": [], "tools": []}, ensure_ascii=False
    )


@dataclass(frozen=True)
class LlmToolRoundResult:
    """原生 function calling 单回合结果。

    ``content`` 为模型文本输出（工具轮通常为空）；``tool_calls`` 为
    ``[{id, name, arguments(str)}]``（无工具调用 → []）；``meta`` 与文本路径同构。
    """

    content: str
    tool_calls: list[dict[str, Any]]
    meta: dict[str, Any]


class OpenAiLlmAdapter:
    """OpenAI-compatible 适配器：支持设备级模型配置，未设置时回退系统默认。"""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._default_system_prompt = settings.llm.system_prompt or (
            "你是中文语音助手。回复会转成语音（TTS）播报：只输出适合朗读的简短口语，"
            "不要 markdown/列表/emoji 等富文本。每轮只输出 JSON：{\"tts\":\"…\"}。"
        )

    def _resolve_system_prompt(self, *, device_id: str | None = None) -> str:
        from deskbot_server.utils.device_data import load_llm_system_prompt

        return load_llm_system_prompt(device_id) or self._default_system_prompt

    def _build_system_prompt(self, *, device_id: str | None = None, native_tool_names: list[str] | None = None) -> str:
        """组装本轮 system prompt。

        ``native_tool_names``：None → build 侧按设备解析名单（实验台预览语义）；
        空列表 → 本轮 API 未提供 tools（文本收口轮），prompt 不提任何工具；
        非空 → 原生 function-call 名单 directive（契约在 API tools 参数）。
        """
        base = self._resolve_system_prompt(device_id=device_id)
        return build_llm_system_prompt(base, device_id=device_id, native_tool_names=native_tool_names)

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
        user_message_override: str | None = None,
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
                user_message_override=user_message_override,
            )

    async def llm_tool_round(
        self,
        user_text: str,
        *,
        device_context: str | None = None,
        device_id: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        extra_messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "auto",
        on_system_prompt: Callable[[str], None] | None = None,
        user_message_override: str | None = None,
    ) -> LlmToolRoundResult:
        """原生 function calling 单回合（工具轮恒非流式、不带 json 约束）。

        - ``extra_messages`` 一旦含 assistant(tool_calls)/role=tool 消息，
          本轮不再重复注入 user（round1 的 user 已在历史中）；
        - 本地引擎 400 上下文超限：裁掉最旧一半历史重建重试（≤3 次）；
        - JSON 包装/收尾轮重试不属本层：最终轮语义由 chat_flow 决策
          （可用 ``complete()`` 文本路径收口 JSON envelope）。
        """
        async with _device_llm_lock(device_id):
            return await self._tool_round_locked(
                user_text,
                device_context=device_context,
                device_id=device_id,
                history_messages=history_messages,
                extra_messages=extra_messages,
                tools=tools,
                tool_choice=tool_choice,
                on_system_prompt=on_system_prompt,
                user_message_override=user_message_override,
            )

    async def _tool_round_locked(
        self,
        user_text: str,
        *,
        device_context: str | None = None,
        device_id: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        extra_messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = "auto",
        on_system_prompt: Callable[[str], None] | None = None,
        user_message_override: str | None = None,
    ) -> LlmToolRoundResult:
        llm_cfg = resolve_llm_config(device_id)
        local_engine = is_local_llm_url(llm_cfg.api_base)

        from deskbot_server.infrastructure.llm.tool_schema import build_native_tool_schemas

        native_names = [s["function"]["name"] for s in build_native_tool_schemas(device_id=device_id)]
        system_content = self._build_system_prompt(device_id=device_id, native_tool_names=native_names)
        if on_system_prompt is not None:
            try:
                on_system_prompt(system_content)
            except Exception:
                logger.debug("[LLM] on_system_prompt 回调异常（忽略）", exc_info=True)
        user_content = user_message_override or build_llm_user_message(
            user_text, device_id=device_id, device_context=device_context
        )
        history = list(history_messages or [])
        extra = list(extra_messages or [])
        # 出现工具 trail（assistant.tool_calls / role=tool）后不再重复注入本轮 user
        has_tool_trail = any(
            isinstance(m, dict) and (m.get("role") == "tool" or m.get("tool_calls")) for m in extra
        )
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

        def _assemble(hist: list[dict[str, Any]]) -> list[dict[str, Any]]:
            msgs: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
            msgs.extend(hist)
            if not has_tool_trail:
                msgs.append({"role": "user", "content": user_content})
            msgs.extend(extra)
            return msgs

        messages = _assemble(history)
        cur_history = list(history)
        attempts = 0
        while True:
            try:
                content, calls, meta = await tool_acompletion(
                    messages,
                    device_id=device_id,
                    temperature=0.7,
                    tools=tools,
                    tool_choice=tool_choice,
                )
                return LlmToolRoundResult(content=str(content or ""), tool_calls=calls, meta=meta)
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
                messages = _assemble(cur_history)
                logger.info(
                    "[LLM] 本地引擎上下文超限，裁剪历史重试 device_id=%s history=%d→%d attempt=%d",
                    device_id,
                    prev_len,
                    len(cur_history),
                    attempts,
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
        user_message_override: str | None = None,
    ) -> str:
        llm_cfg = resolve_llm_config(device_id)
        local_engine = is_local_llm_url(llm_cfg.api_base)
        # 火山 ark_responses 流式在工具/联网阶段常长时间无 text delta，语音对话改非流式更稳。
        # 本地引擎（llm-minicpm / llm-qwen 等）不支持 stream，同样强制非流式（TTS 走完整响应后预取）。
        use_stream_tts = bool(on_tts_ready) and llm_cfg.protocol != "ark_responses" and not local_engine

        # 文本通道（收口/纯对话）不提供 API tools → prompt 完全不提工具，
        # 避免模型在本轮重复输出函数调用文本（工具轮语义在原生 function-call 通道）
        system_content = self._build_system_prompt(device_id=device_id, native_tool_names=[])
        if on_system_prompt is not None:
            try:
                on_system_prompt(system_content)
            except Exception:
                logger.debug("[LLM] on_system_prompt 回调异常（忽略）", exc_info=True)
        # 语音轮由应用层单次装配（与实验台展示同一份识别），此处优先用 override；
        # 其余轮次沿用现拼（每轮现读人脸快照）
        user_content = user_message_override or build_llm_user_message(
            user_text, device_id=device_id, device_context=device_context
        )
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
                            "格式含 need_reply、tts、gesture、expression、tools 等字段。"
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
