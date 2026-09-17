"""硅基流动（SiliconFlow）端到端集成测试（需网络与 SILICONFLOW_API_KEY）。

覆盖三条主链路的**真实**行为 —— 这三项是接入前实测确认过的、会让主对话整体不可用的风险点：

1. ``response_format: json_object`` 被 Qwen3-8B 接受（主对话链路默认 json_mode=True，
   被拒就是全挂而不是降级）；
2. ``enable_thinking: false`` 真的抑制思维链（首字延迟从 ~32s 降到 ~0.9s）；
3. 关思考后**原生 function calling 不退化**（提醒 / 记忆 / 搜索都依赖它）。

密钥来源：宿主 env 或 service/.env（``load_dotenv`` 只填空缺项）。未配置则整文件跳过。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from deskbot_server.utils.env import load_dotenv

load_dotenv()  # 独立导入 runtime 时 .env 不会自动加载（那条链只在 create_app 里）

pytestmark = pytest.mark.skipif(
    not os.environ.get("SILICONFLOW_API_KEY", "").strip(),
    reason="SILICONFLOW_API_KEY 未配置（.env 或环境变量）",
)


def _cfg(model: str = "Qwen/Qwen3-8B"):
    from deskbot_server.infrastructure.llm.runtime import build_siliconflow_config

    return build_siliconflow_config(model, source="test", display_name="SiliconFlow 集成测试")


def test_live_json_mode_accepted():
    """json_object 必须被接受，否则整条主对话链路 400。"""
    from deskbot_server.infrastructure.llm.runtime import chat_completion

    content, meta = chat_completion(
        [{"role": "user", "content": '只输出 JSON：{"tts":"集成测试通过"}'}],
        config=_cfg(),
        json_mode=True,
    )

    assert json.loads(content)["tts"] == "集成测试通过"
    assert meta["usage"]["total_tokens"] > 0
    assert meta["model"] == "Qwen/Qwen3-8B"  # org 前缀必须原样发出


def test_live_thinking_disabled_via_extra_body():
    """extra_body 里的 enable_thinking 要真的进请求体并生效（无思维链、token 明显更少）。"""
    from deskbot_server.infrastructure.llm.runtime import chat_completion

    _, with_off = chat_completion(
        [{"role": "user", "content": "用一句话说明你适合做什么。"}], config=_cfg(), json_mode=False
    )
    assert _cfg().extra_body == {"enable_thinking": False}

    from deskbot_server.infrastructure.llm.runtime import ResolvedLlmConfig

    no_flag = _cfg()
    _, baseline = chat_completion(
        [{"role": "user", "content": "用一句话说明你适合做什么。"}],
        config=ResolvedLlmConfig(
            model=no_flag.model, api_key=no_flag.api_key, api_base=no_flag.api_base,
            protocol=no_flag.protocol, source="test", display_name="开思考对照",
        ),
        json_mode=False,
    )

    # 开思考会产生大量 reasoning token；关掉后 completion_tokens 应显著更少
    assert with_off["usage"]["completion_tokens"] < baseline["usage"]["completion_tokens"]


def test_live_native_tool_calling_with_thinking_off():
    """提醒 / 记忆 / 搜索全靠原生 function calling，关思考后必须仍然可用。"""
    from deskbot_server.infrastructure.llm.runtime import tool_acompletion

    tools = [
        {
            "type": "function",
            "function": {
                "name": "set_reminder",
                "description": "给主人设置一个定时提醒",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "minutes": {"type": "integer"}},
                    "required": ["text", "minutes"],
                },
            },
        }
    ]

    content, calls, meta = asyncio.run(
        tool_acompletion(
            [{"role": "user", "content": "5 分钟后提醒我喝水"}], config=_cfg(), tools=tools
        )
    )

    assert calls, f"未返回原生 tool_calls（content={content!r}）"
    assert calls[0]["name"] == "set_reminder"
    assert "喝水" in calls[0]["arguments"]
    assert meta["usage"]["total_tokens"] > 0


def test_live_stream_tts_prefetch():
    """流式 TTS 预取路径：``tts`` 字段一闭合就回调（硅基流动是公网，不会被本地引擎的禁流式拦下）。"""
    from deskbot_server.infrastructure.llm.runtime import chat_acompletion

    ready: list[str] = []

    async def on_tts(text: str) -> None:
        ready.append(text)

    async def _run():
        return await chat_acompletion(
            [{"role": "user", "content": '只输出 JSON：{"tts":"流式通过"}'}],
            config=_cfg(),
            json_mode=True,
            on_tts_ready=on_tts,
        )

    content, meta = asyncio.run(_run())

    assert json.loads(content)["tts"] == "流式通过"
    assert ready == ["流式通过"]  # 预取回调恰好一次
    assert meta["usage"]["total_tokens"] > 0
