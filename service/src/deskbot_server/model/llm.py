"""大模型工具调用结果，供 Service 和 Client 共享。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LlmToolRoundResult:
    """原生 function calling 单回合结果。

    ``content`` 为模型文本输出（工具轮通常为空）；``tool_calls`` 为
    ``[{id, name, arguments(str)}]``（无工具调用 → []）；``meta`` 与文本路径同构。
    """

    content: str
    tool_calls: list[dict[str, Any]]
    meta: dict[str, Any]
