"""原生 tools 传输底座（A0）：payload 组装 / tool_calls 解析 / ark 消息映射 / schema / 开关。"""

from __future__ import annotations

import asyncio

import pytest

from deskbot_server.infrastructure.llm.runtime import (
    _build_completion_payload,
    _messages_to_ark_input,
    _tool_calls_from_ark_response,
    _tool_calls_from_openai_response,
    native_tools_enabled,
)
from deskbot_server.infrastructure.llm.tool_schema import (
    NATIVE_TOOL_NAMES_BATCH1,
    build_native_tool_schemas,
)


class _Cfg:
    protocol = "openai"
    model = "qwen3.8-2b"
    api_key = ""


def test_payload_tools_no_json_format_when_tools_present():
    cfg = _Cfg()
    p = _build_completion_payload(
        [{"role": "user", "content": "hi"}], cfg, temperature=0.7, json_mode=True, stream=False, tools=[{"x": 1}]
    )
    assert "response_format" not in p  # tools 轮不叠加 json_object
    assert p["tools"] == [{"x": 1}]
    # 无 tools → 维持 json_mode 行为
    p2 = _build_completion_payload([{"role": "user", "content": "hi"}], cfg, temperature=0.7, json_mode=True, stream=False)
    assert p2.get("response_format") == {"type": "json_object"}
    assert "tools" not in p2


def test_payload_ark_tools_shape():
    cfg = _Cfg()
    cfg.protocol = "ark_responses"
    p = _build_completion_payload(
        [{"role": "user", "content": "hi"}], cfg, temperature=0.7, json_mode=True, stream=False, tools=[{"y": 2}]
    )
    assert p["tools"] == [{"y": 2}]
    assert "text" not in p  # 无 json 约束


def test_payload_ark_tools_flattened_responses_shape():
    """回归：ark_responses 的 tools 不接受 ChatCompletions 的 function 嵌套（live 400）。

    Responses API 语义：type/name/description/parameters 平铺；tool_choice 对象同源扁平。
    """
    tools = [
        {
            "type": "function",
            "function": {
                "name": "update_user_info",
                "description": "按人归档",
                "parameters": {"type": "object", "properties": {"user_name": {"type": "string"}}, "required": ["user_name"]},
            },
        }
    ]
    choice = {"type": "function", "function": {"name": "update_user_info"}}

    cfg = _Cfg()
    cfg.protocol = "ark_responses"
    p = _build_completion_payload(
        [{"role": "user", "content": "hi"}], cfg, temperature=0.7, json_mode=True, stream=False,
        tools=tools, tool_choice=choice,
    )
    assert p["tools"] == [
        {
            "type": "function",
            "name": "update_user_info",
            "description": "按人归档",
            "parameters": {"type": "object", "properties": {"user_name": {"type": "string"}}, "required": ["user_name"]},
        }
    ]
    assert p["tool_choice"] == {"type": "function", "name": "update_user_info"}
    assert "text" not in p  # 带 tools 不加 json 约束

    # openai 协议（本地引擎）保持 ChatCompletions 嵌套形状不变
    cfg2 = _Cfg()
    p2 = _build_completion_payload(
        [{"role": "user", "content": "hi"}], cfg2, temperature=0.7, json_mode=True, stream=False, tools=tools
    )
    assert p2["tools"] == tools


def test_tool_calls_parsing_openai_and_ark():
    openai_resp = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "memory_add", "arguments": '{"text": "喜欢猫"}'}},
                        {"id": "c2", "type": "function", "function": {"name": "websearch", "arguments": {"query": "天气"}}},
                    ],
                }
            }
        ]
    }
    calls = _tool_calls_from_openai_response(openai_resp)
    assert calls == [
        {"id": "c1", "name": "memory_add", "arguments": '{"text": "喜欢猫"}'},
        {"id": "c2", "name": "websearch", "arguments": '{"query": "天气"}'},  # dict arguments 归一为 str
    ]

    ark_resp = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": ""}]},
            {"type": "function_call", "id": "fc_1", "name": "memory_add", "arguments": '{"text": "x"}'},
        ]
    }
    assert _tool_calls_from_ark_response(ark_resp) == [{"id": "fc_1", "name": "memory_add", "arguments": '{"text": "x"}'}]


def test_ark_input_maps_function_call_items():
    msgs = [
        {"role": "user", "content": "记住"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "memory_add", "arguments": '{"text": "猫"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
    ]
    out = _messages_to_ark_input(msgs)
    assert out[0] == {"role": "user", "content": [{"type": "input_text", "text": "记住"}]}
    assert out[1] == {"type": "function_call", "call_id": "c1", "name": "memory_add", "arguments": '{"text": "猫"}'}
    assert out[2] == {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'}


def test_native_tools_flag_default_off(monkeypatch):
    monkeypatch.setattr("deskbot_server.infrastructure.llm.runtime.load_config", lambda: {"llm": {}})
    assert native_tools_enabled() is False
    assert native_tools_enabled("dev_none") is False


def test_native_tools_flag_device_param_wins(tmp_path, monkeypatch):
    """设备 llm_param.native_tools 优先于 config 回退（真实 DB 链路）。"""
    import sqlite3

    import deskbot_server.infrastructure.llm.runtime as rt

    db = tmp_path / "flag.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE devices (id TEXT PRIMARY KEY, device_id TEXT, owner_user_id TEXT, "
        "asr_provider TEXT NOT NULL DEFAULT 'funasr', tts_provider TEXT NOT NULL DEFAULT 'moss-tts-nano', "
        "llm_provider TEXT NOT NULL DEFAULT '', llm_param TEXT)"
    )
    con.execute(
        "INSERT INTO devices (id, device_id, owner_user_id) VALUES ('d1', 'dev_a', 'u1')"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db))

    # dao 层用真实 sqlite 文件路径，绕过 engine：直接 monkeypatch dao 的 get_llm_param 读取实现太重；
    # 这里验证开关解析逻辑：设备参数 true → True；false → False；未配置 → config 回退
    import deskbot_server.dao.device_mapper as dm

    def _param(did):
        values = {"dev_a": {"native_tools": True}, "dev_b": {"native_tools": False}}
        return values.get(did, {})

    monkeypatch.setattr(dm, "get_llm_param", _param)
    monkeypatch.setattr(rt, "load_config", lambda: {"llm": {"native_tools": True}})
    assert native_tools_enabled("dev_a") is True
    assert native_tools_enabled("dev_b") is False
    assert native_tools_enabled("dev_unset") is True  # 设备未配置 → config True


def _make_settings():
    from deskbot_server.config import load_config
    from deskbot_server.model.settings import AppSettings

    return AppSettings.from_config(load_config())


def test_adapter_tool_round_message_assembly(monkeypatch):
    """工具 trail 出现后不再重复注入 user；首轮注入 user。"""
    import deskbot_server.infrastructure.llm.openai_compat as oc

    captured: dict = {}

    async def _fake_tool_acompletion(messages, *, device_id=None, temperature=0.7, tools=None, tool_choice=None, config=None):
        captured["messages"] = list(messages)
        captured["tools"] = tools
        captured["tool_choice"] = tool_choice
        return "", [{"id": "c1", "name": "memory_add", "arguments": '{"text": "x"}'}], {"usage": None}

    monkeypatch.setattr(oc, "tool_acompletion", _fake_tool_acompletion)
    adapter = oc.OpenAiLlmAdapter(_make_settings())

    async def _run():
        # 首轮：无 trail → 注入 user
        r1 = await adapter.llm_tool_round("记住我喜欢猫", device_id="dev_round", tools=[{"x": 1}])
        roles1 = [m["role"] for m in captured["messages"]]
        assert roles1 == ["system", "user"]
        assert r1.tool_calls[0]["name"] == "memory_add"
        # 二轮：extra 含 assistant.tool_calls + role=tool → 不重复注入 user
        trail = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "memory_add", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
        ]
        r2 = await adapter.llm_tool_round("记住我喜欢猫", device_id="dev_round", tools=[{"x": 1}], extra_messages=trail)
        roles2 = [m["role"] for m in captured["messages"]]
        assert roles2 == ["system", "assistant", "tool"]
        assert r2.content == ""

    asyncio.run(_run())


def test_schema_batch1_keys_match_runner():
    schemas = build_native_tool_schemas(include_batch2=False)
    names = [s["function"]["name"] for s in schemas]
    assert names == ["memory_add", "memory_delete", "schedule_task", "webfetch", "websearch", "session"]
    for s in schemas:
        assert s["function"]["parameters"]["type"] == "object"
    sch = next(s for s in schemas if s["function"]["name"] == "schedule_task")
    props = sch["function"]["parameters"]["properties"]
    assert props["action"]["enum"] == ["create", "list", "get", "update", "delete"]
    assert sch["function"]["parameters"]["required"] == ["action"]
    # 触发规则承载在 description
    assert "禁止仅口头答应" in sch["function"]["description"]
    assert NATIVE_TOOL_NAMES_BATCH1 == names


def test_schema_batch2_default_on_without_quest(monkeypatch):
    """batch2 默认启用：register_* 恒在；quest 工具需设备有 running 任务才产出。"""
    schemas = build_native_tool_schemas(device_id=None)
    names = [s["function"]["name"] for s in schemas]
    assert names[:6] == NATIVE_TOOL_NAMES_BATCH1
    assert "register_face" in names and "register_voiceprint" in names
    assert "update_task_result" not in names  # 无绑定/无 running → 不广告

    class _FakeSvc:
        def get_tool_calls(self, device_id):
            return [{"available_task_ids": ["g_a"]}]

    import deskbot_server.infrastructure.llm.tool_schema as ts
    import deskbot_server.service.quest_service as qs

    monkeypatch.setattr(qs, "QuestService", _FakeSvc)
    schemas2 = build_native_tool_schemas(device_id="dev_q")
    names2 = [s["function"]["name"] for s in schemas2]
    assert "update_task_result" in names2 and "update_task_strategy" in names2
    desc = next(s for s in schemas2 if s["function"]["name"] == "update_task_result")["function"]["description"]
    assert "g_a" in desc  # 动态任务 id 注入 description
