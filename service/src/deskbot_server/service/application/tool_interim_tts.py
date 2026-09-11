"""LLM tool 轮过渡语：模型自述优先，词表兜底只覆盖「确实要等」的工具。

过渡语的产生有三层降级（见 ``chat_flow.complete_llm_with_tool_loop``）：

1. 模型调 ``say`` 工具说的一句（首选，模型自己知道要干什么）；
2. 模型在工具轮 envelope 的 ``tts`` 里写的一句；
3. 本模块的词表兜底——**仅当本轮工具属于「慢工具」**时才启用。

快工具（记忆落库、会话查询、任务记账等本地毫秒级调用）一律静默：它们在被感知为
「刚刚发生」的窗口里完成，此时插一句「稍等」只会让回复显得啰嗦。
"""

from __future__ import annotations

from typing import Any

# 会说「稍等一下」的工具：真实存在可感知等待（网络往返、注册流程、相机采集）
_SLOW_TOOL_PHRASES: dict[str, str] = {
    "websearch": "我帮你搜一下",
    "webfetch": "我打开看看",
    "read": "我读一下文件",
    "write": "我写进文件里",
    "register_face": "我记住你的样子了",
    "register_voiceprint": "我记住你的声音了",
    "capture_camera": "我看一下画面",
    "camera_capture": "我看一下画面",
    "miot": "我帮你控一下设备",
    "mihome": "我帮你控一下设备",
    "mijia": "我帮你控一下设备",
}

# 快工具：本地毫秒级完成，兜底语一律静默（显式列出便于新增工具时对照）
_FAST_TOOLS = frozenset({
    "memory_add", "memory_delete", "schedule_task", "scheduled_task", "session",
    "update_user_info", "update_daily_task", "complete_task", "say",
})

# 未知工具的兜底句（不含「稍等」前缀，避免叠成「稍等，稍等一下」）
_DEFAULT_PHRASE = "我想一下啊"

# 过渡语文本门禁：出现这些词说明模型在预报还没发生的结果
_FORBIDDEN_MARKERS = (
    "已经", "好了", "查到了", "搜到了", "记下了", "记住了", "完成了", "成功了", "结果",
)

_MAX_INTERIM_CHARS = 20


def _tool_name(raw: dict[str, Any]) -> str:
    return str(raw.get("tool") or raw.get("name") or "").strip().lower()


def phrase_for_tool(tool: str) -> str:
    """单工具兜底短句；快工具返回空串（静默）。"""
    key = (tool or "").strip().lower()
    if not key or key in _FAST_TOOLS:
        return ""
    return _SLOW_TOOL_PHRASES.get(key, _DEFAULT_PHRASE)


def interim_text_ok(text: str) -> bool:
    """过渡语质检：空/超长/markdown/JSON 片段/预报结果 → 不合格（宁可不说不说错话）。"""
    t = (text or "").strip()
    if not t or len(t) > _MAX_INTERIM_CHARS:
        return False
    if any(ch in t for ch in "{}[]`*#|<>_"):
        return False
    return not any(m in t for m in _FORBIDDEN_MARKERS)


def build_tool_interim_tts(tools: list[dict[str, Any]]) -> str:
    """词表兜底：多个慢工具合并为一句口语过渡语；无慢工具 → 空串（静默）。"""
    if not tools:
        return ""
    seen: set[str] = set()
    parts: list[str] = []
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        name = _tool_name(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        phrase = phrase_for_tool(name)
        if phrase:
            parts.append(phrase)
    if not parts:
        return ""
    if len(parts) == 1:
        return f"{parts[0]}。"
    return "，".join(parts[:-1]) + "，" + parts[-1] + "。"


def resolve_interim_tts(
    *, model_text: str = "", say_text: str = "", tools: list[dict[str, Any]] | None = None
) -> str:
    """三层降级合成过渡语：``say`` 工具 → envelope ``tts`` → 词表兜底。

    模型给的两条路径都过 ``interim_text_ok`` 质检，不合格则继续降级；
    词表兜底只在有慢工具时启用。全程无合适文本 → 返回空串（静默）。
    """
    for candidate in (say_text, model_text):
        if interim_text_ok(candidate):
            return str(candidate).strip()
    return build_tool_interim_tts(list(tools or []))
