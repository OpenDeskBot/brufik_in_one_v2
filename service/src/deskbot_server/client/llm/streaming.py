"""Cancellable SSE transport for native tool rounds (including local llama-server)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from deskbot_server.client.llm.runtime import (
    ResolvedLlmConfig,
    _build_completion_payload,
    _completion_url,
    _content_from_response,
    _delta_content_from_sse_event,
    _tool_calls_from_response,
    _usage_from_response,
    _uses_ark_responses_api,
    _validate_api_key,
    build_chat_model,
    resolve_first_token_timeout,
)


async def stream_tool_completion(
    client: httpx.AsyncClient,
    messages: list[dict[str, Any]],
    cfg: ResolvedLlmConfig,
    *,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    temperature: float,
    on_delta: Callable[[str], Awaitable[None]],
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    _validate_api_key(cfg)
    payload = _build_completion_payload(
        messages, cfg, temperature=temperature, json_mode=False, stream=True, tools=tools, tool_choice=tool_choice
    )
    parts: list[str] = []
    calls: dict[Any, dict[str, str]] = {}
    usage = None
    final_response = None
    finished = False
    ark = _uses_ark_responses_api(cfg.protocol)
    first_timeout = resolve_first_token_timeout(cfg.protocol)
    # A real async response closes its socket on cancellation. No detached
    # urllib thread continues consuming a model slot after the user interrupts.
    headers = {"Accept": "text/event-stream"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    async with client.stream(
        "POST",
        _completion_url(cfg.api_base, cfg.protocol),
        json=payload,
        headers=headers,
    ) as response:
        if response.is_error:
            raw = (await response.aread()).decode("utf-8", "replace")[:1000]
            raise RuntimeError(f"LLM API 请求失败 HTTP {response.status_code}: {raw}")
        # Some local wrappers accept stream=true but return a complete JSON.
        # Preserve compatibility without retrying an already accepted request.
        if "application/json" in response.headers.get("content-type", ""):
            data = json.loads(await response.aread())
            content = _content_from_response(data, protocol=cfg.protocol)
            return (
                content,
                _tool_calls_from_response(data, protocol=cfg.protocol),
                {
                    "model": build_chat_model(cfg.protocol, cfg.model),
                    "source": cfg.source,
                    "display_name": cfg.display_name,
                    "usage": _usage_from_response(data, protocol=cfg.protocol),
                    "raw_response": data,
                    "streamed": False,
                },
            )
        first = True
        deadline = asyncio.get_running_loop().time() + first_timeout
        lines = response.aiter_lines().__aiter__()
        while True:
            try:
                if first and first_timeout > 0:
                    async with asyncio.timeout_at(deadline):
                        line = await anext(lines)
                else:
                    line = await anext(lines)
            except StopAsyncIteration:
                break
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            event = json.loads(raw)
            if event.get("error") or event.get("type") in {"error", "response.failed", "response.incomplete"}:
                raise RuntimeError("LLM 流式回复失败或未完整生成")
            piece = _delta_content_from_sse_event(event, protocol=cfg.protocol)
            if piece:
                first = False
                parts.append(piece)
                await on_delta(piece)
            if ark:
                kind = event.get("type")
                if kind == "response.completed":
                    final_response = event.get("response") or {}
                    usage = _usage_from_response(final_response, protocol=cfg.protocol)
                    finished = True
                elif kind in {"response.output_item.added", "response.function_call_arguments.delta"}:
                    if (
                        kind == "response.function_call_arguments.delta"
                        or (event.get("item") or {}).get("type") == "function_call"
                    ):
                        first = False
                continue
            choices = event.get("choices") or []
            for choice in choices[:1]:
                delta = choice.get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    first = False
                    state = calls.setdefault(call["index"], {"id": "", "name": "", "arguments": ""})
                    state["id"] += call.get("id") or ""
                    function = call.get("function") or {}
                    state["name"] += function.get("name") or ""
                    state["arguments"] += function.get("arguments") or ""
                reason = choice.get("finish_reason")
                if reason:
                    if reason not in {"stop", "tool_calls"}:
                        raise RuntimeError(f"LLM 流式回复未完整生成: {reason}")
                    finished = True
            if event.get("usage"):
                usage = event["usage"]
    if not finished:
        raise RuntimeError("LLM 流式连接提前结束，未收到完成事件")
    content = "".join(parts)
    if ark:
        final_response = final_response or {}
        content = _content_from_response(final_response, protocol=cfg.protocol) or content
        tool_calls = _tool_calls_from_response(final_response, protocol=cfg.protocol)
        raw_response = final_response
    else:
        tool_calls = [calls[key] for key in sorted(calls)]
        raw_response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]},
                            }
                            for c in tool_calls
                        ],
                    }
                }
            ],
            "usage": usage,
        }
    return (
        content.strip(),
        tool_calls,
        {
            "model": build_chat_model(cfg.protocol, cfg.model),
            "source": cfg.source,
            "display_name": cfg.display_name,
            "usage": usage,
            "raw_response": raw_response,
            "streamed": True,
        },
    )
