"""原生 function calling 探针：对真实引擎（本地 llama / ark_responses）跑工具调用矩阵。

用法（在 service/ 目录）：
    .venv/bin/python scripts/probe_native_tools.py --base http://127.0.0.1:9106/v1 --model qwen3.8-2b
    .venv/bin/python scripts/probe_native_tools.py            # 用 config.yaml 系统默认（ark_responses）

产出：每轮 tool_calls 是否产出 / arguments 是否合法 JSON / content 是否为空。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from deskbot_server.infrastructure.llm.runtime import tool_acompletion
from deskbot_server.infrastructure.llm.tool_schema import build_native_tool_schemas


def _round(content: str) -> str:
    return {"role": "user", "content": content}


CASES = [
    ("memory_add（最简 schema）", "记住我喜欢猫，周三开例会", ["memory_add"]),
    ("schedule_task（复杂 schema）", "两分钟后提醒我喝水", ["schedule_task"]),
    ("websearch", "帮我搜一下今天的新闻", ["websearch"]),
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="OpenAI 兼容 base url（缺省用系统 config）")
    ap.add_argument("--model", default=None, help="模型名（本地引擎用 --alias 暴露名）")
    args = ap.parse_args()

    schemas = build_native_tool_schemas()
    config = None
    if args.base:
        from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig

        config = ResolvedLlmConfig(
            model=args.model or "model",
            api_key="",
            api_base=args.base,
            protocol="openai",
            source="test",
            display_name=f"probe({args.base})",
        )
    print(f"工具 schema: {[s['function']['name'] for s in schemas]}")
    print("=" * 70)

    async def _run_case(title: str, text: str) -> dict:
        content, calls, meta = await tool_acompletion(
            [_round(text)], tools=schemas, tool_choice="auto", config=config
        )
        return {"title": title, "content": content, "calls": calls}

    # 三案例各自独立多轮上下文：首轮后把结果以 role=tool 回灌再问一轮，验证多轮走通
    for title, text, _ in CASES:
        try:
            content, calls, meta = await tool_acompletion(
                [_round(text)], tools=schemas, tool_choice="auto", config=config
            )
            ok_args = True
            for c in calls:
                try:
                    json.loads(c.get("arguments") or "")
                except ValueError:
                    ok_args = False
            print(f"[{title}]")
            print(f"  tool_calls: {len(calls)}   arguments_json_ok: {ok_args}   content: {content[:60]!r}")
            for c in calls:
                print(f"    -> {c.get('name')}({c.get('arguments')})")
        except Exception as exc:
            print(f"[{title}] 调用失败: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
