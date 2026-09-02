"""原生 tools 传输底座（A0）：payload 组装 / tool_calls 解析 / ark 消息映射 / schema / 开关。"""

from __future__ import annotations

import json

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


def test_native_tools_flag_device_param_wins(monkeypatch):
    monkeypatch.setattr("deskbot_server.infrastructure.llm.runtime.load_config", lambda: {"llm": {"native_tools": True}})

    class _FakeDev:
        pass

    def _fake_get_llm_param(did):
        assert did == "dev_a"
        return {"native_tools": True}

    def _fake_get_llm_param_off(did):
        assert did == "dev_b"
        return {"native_tools": False}

    import deskbot_server.infrastructure.llm.runtime as rt

    monkeypatch.setattr(rt, "get_llm_param", _fake_get_llm_param) if hasattr(rt, "get_llm_param") else None
    # 直接在 dao 层打桩（runtime 内联 import）
    import types

    fake_dao = types.ModuleType("deskbot_server.dao.device_mapper")
    fake_dao.get_llm_param = _fake_get_llm_param
    monkeypatch.setitem(__import__("sys").modules, "deskbot_server.dao.device_mapper", fake_dao)
    assert native_tools_enabled("dev_a") is True
    fake_dao.get_llm_param = _fake_get_llm_param_off
    assert native_tools_enabled("dev_b") is False
    # 设备未配 → 回退 config
    assert native_tools_enabled() is True


def test_schema_batch1_keys_match_runner():
    schemas = build_native_tool_schemas()
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
