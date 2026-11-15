from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from deskbot_server.client.llm.runtime import chat_acompletion, is_local_llm_url, tool_acompletion
from deskbot_server.model.llm import LlmToolRoundResult
from deskbot_server.model.settings import AppSettings
from deskbot_server.service.llm_context import (
    build_llm_system_prompt,
    build_llm_user_message,
    estimate_text_tokens,
    strip_llm_preamble,
)
from deskbot_server.service.llm_routing import resolve_llm_config

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
    history: list[dict[str, str]], *, system_content: str, user_content: str, extra: list[dict[str, str]],
    budget_tokens: int = _LOCAL_CTX_BUDGET_TOKENS,
) -> list[dict[str, str]]:
    """本地引擎：按预估 token 预算从最旧开始裁剪历史，至少保留最近一条。"""
    if not history:
        return history
    fixed = estimate_text_tokens(system_content) + estimate_text_tokens(user_content) + 8
    fixed += sum(estimate_text_tokens(str(m.get("content") or "")) + 4 for m in extra)
    budget = budget_tokens - fixed
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
    """DeepSeek 等模型偶发纯文本回复；包装成约定 JSON，避免二次请求超时。

    原实现只挡 ``{``/``[`` 开头的输出，模型若写「一段中文推理 + 换行 + JSON
    envelope」就会被整段包成 ``tts`` 念出来。这里先按原护栏排除 JSON 意图，
    再用 ``strip_llm_preamble`` 剥掉推理前言。
    """
    raw = (text or "").strip()
    if not raw or raw.startswith("{") or raw.startswith("["):
        return None  # JSON 意图（哪怕解析失败）交给重试路径
    plain = strip_llm_preamble(raw)
    if not plain or len(plain) > 800:
        return None
    if plain.startswith("{") or plain.startswith("[") or '"need_reply"' in plain:
        return None
    return json.dumps(
        {"need_reply": True, "tts": plain, "gesture": [], "expression": []}, ensure_ascii=False
    )




class OpenAiLlmClient:
    """OpenAI-compatible 适配器：支持设备级模型配置，未设置时回退系统默认。"""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._http: httpx.AsyncClient | None = None
        self._default_system_prompt = settings.llm.system_prompt or (
            "你是中文语音助手。回复会转成语音（TTS）播报：只输出适合朗读的简短口语纯文本，"
            "不要 markdown/列表/emoji 等富文本，也不要 JSON 或动作/表情字段。"
        )

    def _resolve_system_prompt(self, *, device_id: str | None = None) -> str:
        from deskbot_server.service.device_data_paths import load_llm_system_prompt

        return load_llm_system_prompt(device_id) or self._default_system_prompt

    def _build_system_prompt(self, *, device_id: str | None = None, native_tool_names: list[str] | None = None, cacheable: bool = False) -> str:
        """组装本轮 system prompt。

        ``native_tool_names``：None → build 侧按设备解析名单（实验台预览语义）；
        空列表 → 本轮 API 未提供 tools（文本收口轮），prompt 不提任何工具；
        非空 → 原生 function-call 名单 directive（契约在 API tools 参数）。
        """
        base = self._resolve_system_prompt(device_id=device_id)
        options = {"include_clock": False} if cacheable else {}
        return build_llm_system_prompt(base, device_id=device_id, native_tool_names=native_tool_names, **options)

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
        on_raw_response: Callable[[dict[str, Any]], None] | None = None,
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
                on_raw_response=on_raw_response,
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
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LlmToolRoundResult:
        """原生 function calling 单回合，支持增量口语与完整工具结果。

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
                on_delta=on_delta,
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
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LlmToolRoundResult:
        llm_cfg = resolve_llm_config(device_id)
        local_engine = is_local_llm_url(llm_cfg.api_base)

        from deskbot_server.service.llm_tools_schema import build_native_tool_schemas

        native_names = [s["function"]["name"] for s in build_native_tool_schemas(device_id=device_id)]
        cache_options = {"cacheable": True} if llm_cfg.model == "realtime-local" else {}
        system_content = self._build_system_prompt(device_id=device_id, native_tool_names=native_names, **cache_options)
        if on_system_prompt is not None:
            try:
                on_system_prompt(system_content)
            except Exception:
                logger.debug("[LLM] on_system_prompt 回调异常（忽略）", exc_info=True)
        user_content = user_message_override or build_llm_user_message(
            user_text, device_id=device_id, device_context=device_context
        )
        if cache_options:
            from deskbot_server.service.llm_context import beijing_time_str, llm_user_last_talk_prompt_appendix
            # Tools are rendered after the system message by Qwen's template.
            # A changing clock there invalidates the entire tools KV prefix.
            user_content = (f"[服务端上下文] 当前时间是: {beijing_time_str()}（北京时间，东八区）\n"
                            + llm_user_last_talk_prompt_appendix(device_id=device_id) + "\n" + user_content)
        history = list(history_messages or [])
        extra = list(extra_messages or [])
        # 出现工具 trail（assistant.tool_calls / role=tool）后不再重复注入本轮 user
        has_tool_trail = any(
            isinstance(m, dict) and (m.get("role") == "tool" or m.get("tool_calls")) for m in extra
        )
        if local_engine and history:
            tool_budget = {}
            if cache_options:
                tool_tokens = estimate_text_tokens(json.dumps(tools or [], ensure_ascii=False))
                tool_budget["budget_tokens"] = max(512, (llm_cfg.context_window or 8192) - tool_tokens - 512)
            trimmed = _trim_history_for_budget(
                history, system_content=system_content, user_content=user_content, extra=extra, **tool_budget
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
                stream_kwargs: dict[str, Any] = {}
                if on_delta is not None:
                    if self._http is None:
                        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))
                    stream_kwargs = {"on_delta": on_delta, "client": self._http}
                content, calls, meta = await tool_acompletion(
                    messages,
                    config=llm_cfg,
                    device_id=device_id,
                    temperature=0.7,
                    tools=tools,
                    tool_choice=tool_choice,
                    **stream_kwargs,
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

    async def warmup_realtime(self, device_id: str) -> None:
        from dataclasses import replace

        from deskbot_server.service.llm_tools_schema import build_native_tool_schemas
        cfg = resolve_llm_config(device_id)
        if cfg.model != "realtime-local":
            return
        tools = build_native_tool_schemas(device_id=device_id, is_tool_round=True)
        native_names = [t["function"]["name"] for t in build_native_tool_schemas(device_id=device_id)]
        system = self._build_system_prompt(device_id=device_id,
                                          native_tool_names=native_names, cacheable=True)
        cfg = replace(cfg, extra_body={**(cfg.extra_body or {}), "max_tokens": 1})
        # Populate the reusable system/tools prefix only. No tool is executed,
        # no history is saved and no generated audio reaches the robot.
        await tool_acompletion([{"role": "system", "content": system}, {"role": "user", "content": "你好"}],
                               config=cfg, tools=tools, tool_choice="auto")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

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
        on_raw_response: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """纯文本对话通道：模型直接输出要朗读的口语，不再要求 JSON envelope。

        动作/表情由独立的决策模型负责（见 ``service/application/action_decision.py``），
        本通道不再注入 gesture/expression 附录，也不做 JSON 包装/重试。
        """
        llm_cfg = resolve_llm_config(device_id)
        local_engine = is_local_llm_url(llm_cfg.api_base)

        # 文本通道不提供 API tools → prompt 完全不提工具，
        # 避免模型在本轮重复输出函数调用文本（工具轮语义在原生 function-call 通道）
        cache_options = {"cacheable": True} if llm_cfg.model == "realtime-local" else {}
        system_content = self._build_system_prompt(device_id=device_id, native_tool_names=[], **cache_options)
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
        if cache_options:
            from deskbot_server.service.llm_context import beijing_time_str, llm_user_last_talk_prompt_appendix
            # Tools are rendered after the system message by Qwen's template.
            # A changing clock there invalidates the entire tools KV prefix.
            user_content = (f"[服务端上下文] 当前时间是: {beijing_time_str()}（北京时间，东八区）\n"
                            + llm_user_last_talk_prompt_appendix(device_id=device_id) + "\n" + user_content)
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

        async def _chat(msgs: list[dict[str, str]]) -> str:
            # 本地引擎：400 上下文超限时裁掉最旧一半历史重建请求重试
            cur_history = list(history)
            attempts = 0
            while True:
                try:
                    content, meta = await chat_acompletion(
                        msgs,
                        config=llm_cfg,
                        device_id=device_id,
                        temperature=0.7,
                        json_mode=False,
                        stream=False,
                    )
                    raw = meta.get("raw_response") if isinstance(meta, dict) else None
                    if on_raw_response is not None and isinstance(raw, dict):
                        try:
                            on_raw_response(raw)
                        except Exception:
                            logger.debug("[LLM] on_raw_response 回调异常（忽略）", exc_info=True)
                    return str(content or "")
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
                    msgs = _assemble(cur_history)
                    logger.info(
                        "[LLM] 本地引擎上下文超限，裁剪历史重试 device_id=%s history=%d→%d attempt=%d",
                        device_id,
                        prev_len,
                        len(cur_history),
                        attempts,
                    )

        answer = await _chat(_assemble(history))
        # 纯文本无法在流式过程中可靠切出「tts 字段」；整段返回后再触发预取回调
        if on_tts_ready and answer.strip():
            try:
                await on_tts_ready(answer.strip())
            except Exception:
                logger.debug("[LLM] on_tts_ready 回调异常（忽略）", exc_info=True)
        return answer


OpenAiLlmAdapter = OpenAiLlmClient
