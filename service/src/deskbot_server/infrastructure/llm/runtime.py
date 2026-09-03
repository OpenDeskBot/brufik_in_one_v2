"""OpenAI-compatible LLM runtime.

The default direct provider for China deployments is Volcengine Ark:
``https://ark.cn-beijing.volces.com/api/v3``.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from deskbot_server.config import load_config

logger = logging.getLogger("deskbot-server")

ARK_OPENAI_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
OPENAI_BASE_URL = "https://api.openai.com/v1"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_TIMEOUT_SECONDS = 60
LLM_FIRST_TOKEN_TIMEOUT_SECONDS = 5.0

VOLCENGINE_PROTOCOLS = {"ark", "ark_responses", "volcengine", "doubao"}
OPENAI_COMPAT_PROTOCOLS = {"openai", "ark", "volcengine", "doubao", "dashscope", "qwen"}
ARK_RESPONSES_PROTOCOLS = {"ark_responses"}
LEGACY_MODEL_PREFIXES = OPENAI_COMPAT_PROTOCOLS | ARK_RESPONSES_PROTOCOLS | {"azure", "anthropic", "gemini", "ollama"}

# 本地 llama-server 引擎端点（OpenAI 兼容；服务端不校验 Authorization、不支持 stream）
MINICPM_LLM_BASE_URL = "http://127.0.0.1:9105/v1"
MINICPM_LLM_MODEL = "minicpm5-1b"
QWEN_LLM_BASE_URL = "http://127.0.0.1:9106/v1"
QWEN_LLM_MODEL = "qwen3.8-2b"
# 设备级 LLM provider 白名单：本地固定端点 / 云端 ark；空/非法回落系统默认（config.yaml llm 段）
DEVICE_LLM_PROVIDERS = frozenset({"minicpm", "qwen", "ark"})
LOCAL_LLM_PROVIDERS: dict[str, tuple[str, str]] = {
    "minicpm": (MINICPM_LLM_BASE_URL, MINICPM_LLM_MODEL),
    "qwen": (QWEN_LLM_BASE_URL, QWEN_LLM_MODEL),
}
# 设备级 ark 参数白名单（llm_param["ark"]），解析 / 保存 / 掩码共用
ARK_PARAM_FIELDS = ("api_key", "model_name", "base_url")
ARK_DEVICE_PROTOCOL = "ark_responses"


@dataclass(frozen=True)
class ResolvedLlmConfig:
    model: str
    api_key: str
    api_base: str | None
    protocol: str
    source: str  # "device" | "system" | "test"
    display_name: str
    # 模型上下文窗口 token 数；None = 未知（调用方回退默认预算）
    context_window: int | None = None


def _normalized_protocol(protocol: str | None) -> str:
    p = str(protocol or "openai").strip().lower() or "openai"
    if p == "byteark":
        return "ark"
    if p in {"ark-responses", "arkresponses"}:
        return "ark_responses"
    if p == "volcano":
        return "volcengine"
    return p


def _uses_ark_responses_api(protocol: str) -> bool:
    return _normalized_protocol(protocol) in ARK_RESPONSES_PROTOCOLS


def resolve_first_token_timeout(protocol: str) -> float:
    raw = str(os.environ.get("LLM_FIRST_TOKEN_TIMEOUT") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    # ark_responses 在联网/工具阶段可能长时间无 output_text.delta；首字超时误杀，交给总超时。
    if _uses_ark_responses_api(protocol):
        return 0.0
    return LLM_FIRST_TOKEN_TIMEOUT_SECONDS


def _default_base_url(protocol: str) -> str | None:
    protocol = _normalized_protocol(protocol)
    if protocol in VOLCENGINE_PROTOCOLS:
        return ARK_OPENAI_BASE_URL
    if protocol == "dashscope":
        return DASHSCOPE_BASE_URL
    if protocol == "openai":
        return OPENAI_BASE_URL
    return None


def _resolve_api_base(protocol: str, configured_base_url: str | None) -> str | None:
    base_url = str(configured_base_url or "").strip()
    if base_url:
        return base_url.rstrip("/")
    default_base = _default_base_url(protocol)
    return default_base.rstrip("/") if default_base else None


def resolve_system_llm_config(cfg: dict | None = None) -> ResolvedLlmConfig:
    """系统默认 LLM：只读 config.yaml ``llm`` 段（本地免费引擎或未带密钥的旧配置）。

    云端模型密钥一律为设备级（devices.llm_provider=ark + llm_param["ark"]）；系统默认
    不读取任何环境变量密钥，api_key 恒为空。
    """
    if cfg is None:
        cfg = load_config()
    llm_cfg = dict(cfg.get("llm") or {})
    protocol = _normalized_protocol(llm_cfg.get("protocol") or "openai")
    model_name = str(llm_cfg.get("model_name") or "").strip()
    base_url = str(llm_cfg.get("base_url") or "").strip()
    resolved_base = _resolve_api_base(protocol, base_url)
    return ResolvedLlmConfig(
        model=build_chat_model(protocol, model_name),
        api_key="",
        api_base=resolved_base,
        protocol=protocol,
        source="system",
        display_name=f"系统默认 ({model_name})",
    )


def _coerce_bool_flag(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    s = str(raw or "").strip().lower()
    return s in ("1", "true", "yes", "on")


def native_tools_enabled(device_id: str | None = None) -> bool:
    """原生 function calling 开关。

    设备级 ``devices.llm_param.native_tools`` 优先；缺省回退 config.yaml
    ``llm.native_tools``（默认 False——本地小模型 tool-call 未验证前保持关闭）。
    """
    did = str(device_id or "").strip()
    if did:
        try:
            from deskbot_server.dao.device_mapper import get_llm_param

            raw = get_llm_param(did).get("native_tools")
        except Exception:
            raw = None
        if raw is not None:
            return _coerce_bool_flag(raw)
    try:
        return _coerce_bool_flag((load_config().get("llm") or {}).get("native_tools"))
    except Exception:
        return False


def resolve_device_llm_provider(device_id: str | None) -> str | None:
    """设备级 LLM provider（devices.llm_provider，白名单校验）。

    空 / 非法值 / 无设备 → None（回落系统默认，见 resolve_system_llm_config）。
    """
    did = str(device_id or "").strip()
    if not did:
        return None
    from deskbot_server.dao.device_mapper import get_llm_provider

    provider = str(get_llm_provider(did) or "").strip()
    if provider not in DEVICE_LLM_PROVIDERS:
        if provider:
            logger.warning("[LLM] 设备 llm_provider 非法/未知: %r（回落系统默认）", provider)
        return None
    return provider


def _local_device_llm_config(provider: str) -> ResolvedLlmConfig:
    """本地固定端点（minicpm / qwen）：免 API Key，无参数可配。"""
    base_url, model_name = LOCAL_LLM_PROVIDERS[provider]
    return ResolvedLlmConfig(
        model=model_name,
        api_key="",
        api_base=base_url,
        protocol="openai",
        source="device",
        display_name=f"本地 {provider}（{model_name}）",
    )


def _ark_device_llm_config(device_id: str) -> ResolvedLlmConfig:
    """按设备 llm_param["ark"] 构造云端 ark 配置（密钥/模型仅设备级）。

    model_name 缺失抛 ValueError（绝不回落 config.yaml，避免本地模型 ID 串位到云端）；
    base_url 空 → 内置默认；api_key 空则留空，真正调用时由 _validate_api_key 报错。
    """
    from deskbot_server.dao.device_mapper import get_llm_param

    param = get_llm_param(device_id)
    ark = param.get("ark")
    ark = ark if isinstance(ark, dict) else {}
    model_name = str(ark.get("model_name") or "").strip()
    if not model_name:
        raise ValueError(
            "该设备 ark LLM 配置缺少模型 ID（火山方舟推理接入点 ep-…）："
            "请在机器人设置 → LLM → ark「配置」中为该设备填写"
        )
    base_url = str(ark.get("base_url") or "").strip().rstrip("/") or ARK_OPENAI_BASE_URL
    return ResolvedLlmConfig(
        model=build_chat_model(ARK_DEVICE_PROTOCOL, model_name),
        api_key=str(ark.get("api_key") or "").strip(),
        api_base=base_url,
        protocol=ARK_DEVICE_PROTOCOL,
        source="device",
        display_name=f"ark · {model_name}",
    )


def resolve_llm_config(device_id: str | None = None, cfg: dict | None = None) -> ResolvedLlmConfig:
    """解析设备生效 LLM 配置（设备级真源：devices.llm_provider / llm_param）。

    优先级：minicpm / qwen（本地固定端点）→ ark（llm_param["ark"]，含密钥/模型）→
    系统默认（config.yaml llm 段）。顶层键 context_window / native_tools 自
    devices.llm_param 合并（未填 → None，调用方回退默认预算）。
    ``cfg`` 仅供测试注入自定义 config 文件内容。
    """
    provider = resolve_device_llm_provider(device_id)
    if provider in LOCAL_LLM_PROVIDERS:
        resolved = _local_device_llm_config(provider)
    elif provider == "ark":
        resolved = _ark_device_llm_config(str(device_id or "").strip())
    else:
        resolved = resolve_system_llm_config(cfg)
    did = str(device_id or "").strip()
    if not did:
        return resolved
    from deskbot_server.dao.device_mapper import get_llm_param

    param = get_llm_param(did)
    raw_cw = param.get("context_window")
    try:
        cw = int(raw_cw)
    except (TypeError, ValueError):
        return resolved
    if cw > 0:
        return replace(resolved, context_window=cw)
    return resolved


def build_chat_model(protocol: str, model_name: str) -> str:
    """Return the raw model/endpoint ID used by OpenAI-compatible APIs.

    Older code prefixed models for the previous adapter (for example ``openai/foo``).  The
    direct HTTP API expects the provider model ID only, so known compatibility
    prefixes are stripped while real ``org/model`` IDs are preserved.
    """
    _ = _normalized_protocol(protocol)
    raw_model = str(model_name or "").strip()
    if not raw_model:
        raise ValueError("model_name required")
    if "/" not in raw_model:
        return raw_model
    prefix, rest = raw_model.split("/", 1)
    if prefix.strip().lower() in LEGACY_MODEL_PREFIXES and rest.strip():
        return rest.strip()
    return raw_model


def _completion_url(api_base: str | None, protocol: str) -> str:
    base = _resolve_api_base(protocol, api_base)
    if not base:
        raise ValueError("LLM Base URL 未配置。请填写 Base URL 或选择火山方舟/Ark 协议。")
    base = base.rstrip("/")
    if _uses_ark_responses_api(protocol):
        if base.endswith("/responses"):
            return base
        return f"{base}/responses"
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _ark_responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """ChatCompletions 嵌套 tools → 方舟 Responses API 扁平 tools。

    Responses API 的 function 工具**不带** ``function`` 嵌套：name/description/
    parameters 平铺在工具条目上（嵌套形状会被 ark 判 ``json: unknown field
    "function"`` 400）。已是扁平形状/未知形状原样透传。
    """
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else None
        if fn is None:
            out.append(t)
            continue
        row: dict[str, Any] = {"type": "function"}
        for key in ("name", "description", "parameters", "strict"):
            if key in fn and fn[key] is not None:
                row[key] = fn[key]
        if not row.get("name"):
            continue  # 无工具名视为无效条目
        out.append(row)
    return out


def _ark_responses_tool_choice(tool_choice: Any) -> Any:
    """Responses API 的 tool_choice：对象形状扁平化为 {type, name}；字符串原样。"""
    if not isinstance(tool_choice, dict):
        return tool_choice
    fn = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else None
    if fn is None:
        return tool_choice
    out: dict[str, Any] = {"type": str(tool_choice.get("type") or "function")}
    name = fn.get("name")
    if name:
        out["name"] = name
    return out


def _messages_to_ark_input(messages: list[dict[str, str] | dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI ``messages`` → 火山方舟 Responses API ``input``。

    原生 function calling 映射：assistant ``tool_calls`` → ``function_call`` item；
    role=tool → ``function_call_output`` item。
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip() or "user"
        # 原生工具调用：assistant 带 tool_calls
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                name = str(fn.get("name") or "").strip()
                if not name:
                    continue
                args = fn.get("arguments")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                out.append(
                    {
                        "type": "function_call",
                        "call_id": str(tc.get("id") or ""),
                        "name": name,
                        "arguments": str(args or "{}"),
                    }
                )
            continue
        # 原生工具结果
        if role == "tool":
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": str(msg.get("tool_call_id") or ""),
                    "output": _stringify_content(msg.get("content")),
                }
            )
            continue
        content = msg.get("content")
        if isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip()
                if item_type in {"input_text", "output_text", "text"}:
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text:
                        parts.append({"type": "input_text", "text": text})
                elif item_type == "input_image" and isinstance(item.get("image_url"), str):
                    parts.append({"type": "input_image", "image_url": item["image_url"]})
            if parts:
                out.append({"role": role, "content": parts})
            continue
        text = _stringify_content(content).strip()
        if not text:
            continue
        out.append({"role": role, "content": [{"type": "input_text", "text": text}]})
    if not out:
        raise ValueError("LLM 请求缺少有效 input")
    return out


LOCAL_LLM_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_local_llm_url(api_base: str | None) -> bool:
    """判断 base_url 是否为本地/内网地址。

    本地引擎（如 llm-minicpm）不需要 API Key、不支持流式，据此走豁免与降级路径。
    """
    raw = str(api_base or "").strip().rstrip("/")
    if not raw:
        return False
    try:
        netloc = raw.split("://", 1)[1].split("/", 1)[0]
    except IndexError:
        return False
    host = netloc.rsplit(":", 1)[0].strip("[]")
    if host in LOCAL_LLM_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _validate_api_key(cfg: ResolvedLlmConfig) -> None:
    if is_local_llm_url(cfg.api_base):
        return  # 本地引擎不校验 Authorization（服务端不鉴权）
    if not cfg.api_key or "请替换" in cfg.api_key:
        raise ValueError(
            "LLM API Key 未配置。系统默认仅支持本地免费引擎；"
            "云端模型密钥请在该设备 LLM 配置（机器人设置 → LLM → ark「配置」）中填写。"
        )


def _build_completion_payload(
    messages: list[dict[str, str]],
    cfg: ResolvedLlmConfig,
    *,
    temperature: float,
    json_mode: bool,
    stream: bool,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> dict[str, Any]:
    # 原生 function calling 轮：带 tools 时不叠加 json_object 约束（两者并存易冲突）
    want_json = json_mode and not tools
    if _uses_ark_responses_api(cfg.protocol):
        payload: dict[str, Any] = {
            "model": build_chat_model(cfg.protocol, cfg.model),
            "input": _messages_to_ark_input(messages),
            "stream": stream,
            "thinking": {"type": "disabled"},
        }
        if want_json:
            payload["text"] = {"format": {"type": "json_object"}}
        if tools:
            # Responses API 工具形状与 ChatCompletions 不同：嵌套 function 需扁平化
            payload["tools"] = _ark_responses_tools(tools)
            if tool_choice is not None:
                payload["tool_choice"] = _ark_responses_tool_choice(tool_choice)
        return payload

    payload = {
        "model": build_chat_model(cfg.protocol, cfg.model),
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if want_json:
        payload["response_format"] = {"type": "json_object"}
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    return payload


def _usage_from_response(response: Any, *, protocol: str = "openai") -> dict[str, Any] | None:
    if isinstance(response, dict) and _uses_ark_responses_api(protocol):
        return _usage_from_ark_response(response)

    if isinstance(response, dict):
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return None
        return {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }

    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
    except Exception:
        return None


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(item.get("content"), str):
                    parts.append(str(item["content"]))
        return "".join(parts)
    return "" if content is None else str(content)


def _content_from_response(response: Any, *, protocol: str = "openai") -> str:
    if isinstance(response, dict):
        if _uses_ark_responses_api(protocol):
            return _content_from_ark_response(response)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if isinstance(message, dict):
            return _stringify_content(message.get("content")).strip()
        delta = first.get("delta")
        if isinstance(delta, dict):
            return _stringify_content(delta.get("content")).strip()
        return ""

    try:
        return (response.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError):
        return ""


def _content_from_ark_response(response: dict[str, Any]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") == "output_text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "".join(parts).strip()


def _usage_from_ark_response(response: dict[str, Any]) -> dict[str, Any] | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _tool_calls_from_openai_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 OpenAI Chat Completions 的 ``message.tool_calls`` → [{id, name, arguments(str)}]。"""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    first = choices[0]
    if not isinstance(first, dict):
        return []
    message = first.get("message")
    if not isinstance(message, dict):
        return []
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    out: list[dict[str, Any]] = []
    for tc in raw_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": str(tc.get("id") or ""), "name": name, "arguments": str(args or "")})
    return out


def _tool_calls_from_ark_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    """解析火山 Responses API ``output[]`` 的 ``function_call`` 条目。"""
    output = response.get("output")
    if not isinstance(output, list):
        return []
    out: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "function_call":
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        args = item.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": str(item.get("id") or item.get("call_id") or ""), "name": name, "arguments": str(args or "")})
    return out


def _tool_calls_from_response(response: Any, *, protocol: str = "openai") -> list[dict[str, Any]]:
    """按协议解析响应的原生 tool_calls；非 dict（mock/对象形态）→ []。"""
    if not isinstance(response, dict):
        return []
    if _uses_ark_responses_api(protocol):
        return _tool_calls_from_ark_response(response)
    return _tool_calls_from_openai_response(response)


def _delta_content_from_sse_event(data: dict[str, Any], *, protocol: str = "openai") -> str:
    if _uses_ark_responses_api(protocol):
        event_type = str(data.get("type") or "")
        if event_type == "response.output_text.delta":
            delta = data.get("delta")
            return delta if isinstance(delta, str) else ""
        return ""

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if isinstance(delta, dict):
        return _stringify_content(delta.get("content"))
    message = first.get("message")
    if isinstance(message, dict):
        return _stringify_content(message.get("content"))
    return ""


def _iter_sse_json_events(resp) -> Any:
    """从 OpenAI-compatible SSE 响应中逐条解析 ``data: {...}``。"""
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line or not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                return
            try:
                data = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                yield data


def _request_chat_completion_stream(
    messages: list[dict[str, str]],
    cfg: ResolvedLlmConfig,
    *,
    temperature: float,
    json_mode: bool,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    on_delta: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """SSE 流式 Chat Completions；``on_delta`` 收到 content 增量时回调。"""
    _validate_api_key(cfg)
    url = _completion_url(cfg.api_base, cfg.protocol)
    payload = _build_completion_payload(messages, cfg, temperature=temperature, json_mode=json_mode, stream=True)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "deskbot-server/0.1",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", "replace").strip()
        preview = err_body[:1000] if err_body else str(exc)
        raise RuntimeError(f"LLM API 请求失败 HTTP {exc.code}: {preview}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM API 请求失败: {exc.reason}") from exc

    parts: list[str] = []
    usage: dict[str, Any] | None = None
    try:
        with resp:
            for event in _iter_sse_json_events(resp):
                piece = _delta_content_from_sse_event(event, protocol=cfg.protocol)
                if piece:
                    parts.append(piece)
                    if on_delta is not None:
                        on_delta(piece)
                if _uses_ark_responses_api(cfg.protocol):
                    if str(event.get("type") or "") == "response.completed":
                        response_obj = event.get("response")
                        if isinstance(response_obj, dict):
                            usage = _usage_from_ark_response(response_obj)
                    continue
                event_usage = event.get("usage")
                if isinstance(event_usage, dict):
                    usage = {
                        "prompt_tokens": event_usage.get("prompt_tokens"),
                        "completion_tokens": event_usage.get("completion_tokens"),
                        "total_tokens": event_usage.get("total_tokens"),
                    }
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", "replace").strip()
        preview = err_body[:1000] if err_body else str(exc)
        raise RuntimeError(f"LLM SSE 读取失败 HTTP {exc.code}: {preview}") from exc

    return "".join(parts).strip(), usage


def _request_chat_completion(
    messages: list[dict[str, str]],
    cfg: ResolvedLlmConfig,
    *,
    temperature: float,
    json_mode: bool,
    stream: bool,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    _validate_api_key(cfg)
    url = _completion_url(cfg.api_base, cfg.protocol)
    payload = _build_completion_payload(
        messages, cfg, temperature=temperature, json_mode=json_mode, stream=stream, tools=tools, tool_choice=tool_choice
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "deskbot-server/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", "replace").strip()
        preview = err_body[:1000] if err_body else str(exc)
        raise RuntimeError(f"LLM API 请求失败 HTTP {exc.code}: {preview}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM API 请求失败: {exc.reason}") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = raw[:1000].decode("utf-8", "replace")
        raise RuntimeError(f"LLM API 返回不是合法 JSON: {preview}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("LLM API 返回格式异常：顶层不是 JSON object")
    return data


_TTS_KEY_RE = re.compile(r'"tts"\s*:\s*', re.IGNORECASE)


def try_extract_tts_from_partial_json(buf: str) -> tuple[str | None, bool]:
    """尝试从部分 JSON 文本中提取 ``tts`` 字符串值。

    返回 ``(value, complete)``：
    - 尚未出现 ``tts`` 键或未闭合字符串：``(None, False)``
    - 已闭合：``(value, True)``，空字符串表示 ``"tts":""``
    """
    text = (buf or "").lstrip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :].lstrip()

    m = _TTS_KEY_RE.search(text)
    if not m:
        return None, False

    i = m.end()
    if i >= len(text):
        return None, False

    rest = text[i:].lstrip()
    if rest.startswith("null"):
        return "", True

    if not rest.startswith('"'):
        return None, False

    raw, end = _read_json_string(rest, start=0)
    if end < 0:
        return None, False
    return raw, True


def _read_json_string(text: str, *, start: int) -> tuple[str, int]:
    """读取以 ``"`` 开头的 JSON 字符串，返回解码值与结束索引（不含）。"""
    if start >= len(text) or text[start] != '"':
        return "", -1

    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            try:
                return json.loads(text[start : i + 1]), i + 1
            except json.JSONDecodeError:
                return "", -1
        if ch == "\\":
            if i + 1 >= len(text):
                return "", -1
            i += 2
            continue
        i += 1
    return "", -1


class JsonTtsStreamExtractor:
    """累积流式 delta，在 ``tts`` 字符串闭合时回调一次。"""

    def __init__(self, on_tts_ready: Callable[[str], None] | None = None) -> None:
        self._buf = ""
        self._fired = False
        self._on_tts_ready = on_tts_ready

    @property
    def buffer(self) -> str:
        return self._buf

    def feed(self, chunk: str) -> str | None:
        if self._fired or not chunk:
            return None
        self._buf += chunk
        value, complete = try_extract_tts_from_partial_json(self._buf)
        if not complete:
            return None
        self._fired = True
        text = (value or "").strip()
        if text and self._on_tts_ready is not None:
            self._on_tts_ready(text)
        return text or None

    def reset(self) -> None:
        self._buf = ""
        self._fired = False


async def chat_acompletion(
    messages: list[dict[str, str]],
    *,
    device_id: str | None = None,
    temperature: float = 0.7,
    config: ResolvedLlmConfig | None = None,
    json_mode: bool = True,
    stream: bool = False,
    on_tts_ready: Callable[[str], Awaitable[None]] | None = None,
    first_token_timeout: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call an OpenAI-compatible Chat Completions endpoint."""
    cfg = config or resolve_llm_config(device_id)
    if first_token_timeout is None:
        first_token_timeout = resolve_first_token_timeout(cfg.protocol)
    # 本地引擎（llm-minicpm 等）不支持 stream；任何调用路径都强制走非流式
    use_stream = bool(stream or on_tts_ready) and not is_local_llm_url(cfg.api_base)
    usage_dict: dict[str, Any] | None = None
    tts_extractor: JsonTtsStreamExtractor | None = None

    async def _fire_tts_ready(text: str) -> None:
        if not on_tts_ready or not text:
            return
        result = on_tts_ready(text)
        if inspect.isawaitable(result):
            await result

    if use_stream:
        tts_extractor = JsonTtsStreamExtractor()
        pending_tts: dict[str, str | None] = {"text": None}
        loop = asyncio.get_running_loop()
        first_token_event = asyncio.Event()

        def _on_delta(piece: str) -> None:
            if tts_extractor is None:
                return
            if piece and not first_token_event.is_set():
                loop.call_soon_threadsafe(first_token_event.set)
            ready = tts_extractor.feed(piece)
            if ready:
                pending_tts["text"] = ready

        stream_task = asyncio.create_task(
            asyncio.to_thread(
                _request_chat_completion_stream,
                messages,
                cfg,
                temperature=temperature,
                json_mode=json_mode,
                on_delta=_on_delta,
            )
        )

        # 首字超时检测：first_token_timeout 秒内若无任何 delta 则放弃
        if first_token_timeout > 0:
            token_wait_task = asyncio.create_task(first_token_event.wait())
            done, _ = await asyncio.wait(
                {token_wait_task, stream_task}, timeout=first_token_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            # 清理 token 等待 task
            if not token_wait_task.done():
                token_wait_task.cancel()
                try:
                    await token_wait_task
                except asyncio.CancelledError:
                    pass
            # 若超时且 stream 仍未完成、且未收到首字 → 放弃
            if stream_task not in done and not first_token_event.is_set():
                stream_task.cancel()
                try:
                    await stream_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise TimeoutError(f"LLM 首字超时（{first_token_timeout:.0f}s 内无内容返回）")
            # stream 已完成（可能是错误），但未收到首字 → 让 await stream_task 正常抛出错误
            # stream 仍在运行但已收到首字 → 正常继续等待

        content, usage_dict = await stream_task

        if pending_tts["text"]:
            await _fire_tts_ready(pending_tts["text"])
        elif tts_extractor is not None and not tts_extractor._fired:
            ready = tts_extractor.feed("")
            if ready:
                await _fire_tts_ready(ready)
        logger.debug(
            "[LLM] SSE 流式完成 device_id=%s chars=%d tts_prefetch=%s",
            device_id,
            len(content),
            bool(tts_extractor and tts_extractor._fired),
        )
    else:
        response = await asyncio.to_thread(
            _request_chat_completion, messages, cfg, temperature=temperature, json_mode=json_mode, stream=False
        )
        content = _content_from_response(response, protocol=cfg.protocol)
        usage_dict = _usage_from_response(response, protocol=cfg.protocol)

    meta = {
        "model": build_chat_model(cfg.protocol, cfg.model),
        "source": cfg.source,
        "display_name": cfg.display_name,
        "usage": usage_dict,
    }
    return content, meta


async def tool_acompletion(
    messages: list[dict[str, str] | dict[str, Any]],
    *,
    device_id: str | None = None,
    temperature: float = 0.7,
    config: ResolvedLlmConfig | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """原生 function calling 回合（恒非流式）。

    返回 ``(content, tool_calls, meta)``：``content`` 为该轮模型文本输出（可空），
    ``tool_calls`` 为 ``[{id, name, arguments(str)}]``（无工具调用 → []）。
    ``tools`` 为空时等价于一次不带 json 约束的普通调用。
    """
    cfg = config or resolve_llm_config(device_id)
    response = await asyncio.to_thread(
        _request_chat_completion,
        messages,
        cfg,
        temperature=temperature,
        json_mode=False,
        stream=False,
        tools=tools,
        tool_choice=tool_choice,
    )
    content = _content_from_response(response, protocol=cfg.protocol)
    calls = _tool_calls_from_response(response, protocol=cfg.protocol)
    meta = {
        "model": build_chat_model(cfg.protocol, cfg.model),
        "source": cfg.source,
        "display_name": cfg.display_name,
        "usage": _usage_from_response(response, protocol=cfg.protocol),
    }
    return content, calls, meta


def chat_completion(
    messages: list[dict[str, str]],
    *,
    device_id: str | None = None,
    temperature: float = 0.7,
    config: ResolvedLlmConfig | None = None,
    json_mode: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Synchronous wrapper for Flask endpoints."""
    return asyncio.run(
        chat_acompletion(
            messages, device_id=device_id, temperature=temperature, config=config, json_mode=json_mode, stream=False
        )
    )
