#!/usr/bin/env python3
"""llm-engine：本地 LLM 网关（Cactus Needle 2）独立进程服务。

- GET  /health                  健康检查
- POST /v1/chat/completions     OpenAI 兼容端点（response_format=json_schema → 结构化提取；
                                tools 参数 → needle 内置工具调用）
- POST /chat                    外部服务契约 llm：{"messages": [...]} → {"text": "..."}

设计：核心能力为结构化提取（needle.extract）、补全（needle.Needle().complete）与
内置演示工具调用（@needle.tool 纯本地工具：时间/日期/星期；请求带 OpenAI tools 参数时启用）。
模型为 45M 微型模型（~28MB 内存，CPU 推理）。

运行：.venv/bin/python server.py [--host 127.0.0.1] [--port 9104]（独立 venv）
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import time
import uuid
from typing import Any

import needle  # noqa: F401  # 顶层 import：@needle.tool 装饰器需要

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("llm-engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="llm-engine")

_agent: Any = None
_engine_ok = False
_request_lock = asyncio.Lock()  # 小模型推理快，串行化即可

_DEFAULT_MODEL = "needle-2"


# ────────────────── 内置演示工具（纯本地、无副作用）──────────────────


@needle.tool
def get_current_time():
    """Get the current local time, e.g. \"14:30\". Args: none."""
    return {"time": datetime.datetime.now().strftime("%H:%M")}


@needle.tool
def get_current_date():
    """Get the current local date, e.g. \"2026-08-29\". Args: none."""
    return {"date": datetime.datetime.now().strftime("%Y-%m-%d")}


@needle.tool
def get_weekday():
    """Get the current weekday name in English, e.g. \"Saturday\". Args: none."""
    return {"weekday": datetime.datetime.now().strftime("%A")}


_BUILTIN_TOOLS: dict[str, Any] = {
    "get_current_time": get_current_time,
    "get_current_date": get_current_date,
    "get_weekday": get_weekday,
}


def _init_engine() -> Any:
    """加载 needle 引擎并 warmup（模型加载含权重读取，放线程池避免阻塞事件循环）。"""
    agent = needle.Needle()
    agent.complete("ping")  # 触发完整加载与推理路径
    return agent


@app.on_event("startup")
async def _load_engine() -> None:
    global _agent, _engine_ok
    try:
        _agent = await asyncio.to_thread(_init_engine)
        _engine_ok = True
        logger.info("llm-engine Needle 2 就绪")
    except Exception:
        logger.exception("Needle 初始化失败")
        _engine_ok = False


def _extract_user_text(messages: Any) -> str:
    """取最后一条 role=user 的 content（OpenAI 格式 content 可为 str 或分段 list）。"""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # 多模态分段：拼 text 段
            parts = []
            for seg in content:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    parts.append(seg["text"])
                elif isinstance(seg, str):
                    parts.append(seg)
            if parts:
                return "".join(parts)
        break  # 最后一个 user 消息无有效文本 → 无输入
    return ""


def _extract_content(out: Any) -> str:
    """容错提取 needle complete 返回中的文本（无工具时返回推理痕迹，结构随版本可能变化）。"""
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        for key in ("content", "text", "message", "reasoning", "response"):
            v = out.get(key)
            if isinstance(v, str) and v:
                return v
        # 无工具模式下不应出现 function_calls；出现则原样序列化
        if out.get("type") == "call" or out.get("function_calls") is not None:
            return json.dumps(out, ensure_ascii=False)
        try:
            return json.dumps(out, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(out)
    return str(out)


_JSON_TYPE_TO_PY = {"string": str, "number": float, "integer": int, "boolean": bool}

# ────────────────── 工具 agent（按工具组合缓存，请求间 reset）──────────────────

_agent_cache: dict[frozenset, Any] = {}


def _get_agent(tool_names: frozenset) -> Any:
    """按工具组合取 needle agent；未知工具名一律忽略（保持无工具行为）。"""
    if tool_names in _agent_cache:
        return _agent_cache[tool_names]
    tools = [_BUILTIN_TOOLS[n] for n in tool_names if n in _BUILTIN_TOOLS]
    agent = needle.Needle(tools=tools)
    agent.complete("ping")  # warmup 工具解析路径
    _agent_cache[tool_names] = agent
    logger.info("llm-engine agent 就绪 tools=%s", sorted(tool_names))
    return agent


def _parse_tools(tools: Any) -> frozenset:
    """解析 OpenAI tools 参数（[{"type": "function", "function": {"name": ...}}]）→ 内置工具名集合。"""
    if not isinstance(tools, list):
        return frozenset()
    names = set()
    for t in tools:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if name in _BUILTIN_TOOLS:
                names.add(name)
    return frozenset(names)


def _format_tool_result(out: Any) -> str:
    """组装 agent.run 的返回：reasoning + 工具执行结果（results）。"""
    if isinstance(out, dict):
        parts: list[str] = []
        reasoning = out.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            parts.append(reasoning)
        results = out.get("results")
        if results:
            parts.append(json.dumps(results, ensure_ascii=False))
        if parts:
            return "\n".join(parts)
    return _extract_content(out)


def _schema_to_model(schema: dict):
    """JSON Schema dict → Pydantic 模型（needle.extract 的 dict schema 不生效，需 Pydantic 模型）。"""
    from typing import Optional

    from pydantic import create_model

    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    fields: dict[str, tuple] = {}
    for name, p in props.items():
        if not isinstance(p, dict):
            continue
        t = _JSON_TYPE_TO_PY.get(str(p.get("type") or ""), str)
        if name in required:
            fields[name] = (t, ...)
        else:
            fields[name] = (Optional[t], None)
    if not fields:
        raise ValueError("json_schema.properties 为空，无法构造提取模型")
    return create_model("DynamicExtract", **fields)


async def _complete_locked(messages: list, response_format: dict | None, tools: Any = None) -> str:
    """统一处理：json_schema → 结构化提取；tools → 工具调用循环；否则 → 补全。"""
    text = _extract_user_text(messages)
    if not text.strip():
        raise ValueError("缺少有效的 user 消息")
    if not _engine_ok:
        raise RuntimeError("模型未就绪")
    if isinstance(response_format, dict) and str(response_format.get("type") or "").lower() == "json_schema":
        schema = response_format.get("json_schema")
        if not isinstance(schema, dict):
            raise ValueError("response_format.json_schema 必须是 JSON Schema dict")
        model = _schema_to_model(schema)
        async with _request_lock:
            result = await asyncio.to_thread(needle.extract, text, model)
        if result is None:
            return ""
        dump = result.model_dump() if hasattr(result, "model_dump") else result
        return json.dumps(dump, ensure_ascii=False)
    tool_names = _parse_tools(tools)
    if tool_names:
        async with _request_lock:
            agent = _get_agent(tool_names)
            agent.reset()  # 请求间回卷会话，保持工具
            out = await asyncio.to_thread(agent.run, text, max_steps=4)
        return _format_tool_result(out)
    async with _request_lock:
        out = await asyncio.to_thread(_agent.complete, text)
    return _extract_content(out)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "llm-engine", "model_ready": _engine_ok})


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    """外部服务契约 llm：{"messages": [...]} → {"text": str}。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    try:
        text = await _complete_locked(messages, None, tools=payload.get("tools"))
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        logger.exception("chat failed")
        return JSONResponse({"error": "chat failed"}, status_code=500)
    return JSONResponse({"text": text})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    """OpenAI 兼容端点；stream=true 暂不支持。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid json"}}, status_code=400)
    if payload.get("stream"):
        return JSONResponse(
            {"error": {"message": "stream not supported", "type": "invalid_request_error"}}, status_code=400
        )
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    model = str(payload.get("model") or _DEFAULT_MODEL)
    response_format = payload.get("response_format")
    tools = payload.get("tools")
    try:
        text = await _complete_locked(messages, response_format, tools=tools)
    except RuntimeError as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "server_error"}}, status_code=503)
    except ValueError as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "invalid_request_error"}}, status_code=400)
    except Exception:
        logger.exception("chat completion failed")
        return JSONResponse({"error": {"message": "chat completion failed", "type": "server_error"}}, status_code=500)
    prompt_chars = sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
    resp = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": max(1, prompt_chars // 4),
            "completion_tokens": max(1, len(text) // 4),
            "total_tokens": max(2, (prompt_chars + len(text)) // 4),
        },
    }
    return JSONResponse(resp)


def main() -> None:
    parser = argparse.ArgumentParser(description="llm-engine: 本地 LLM 网关（Cactus Needle 2）独立进程服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9104)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
