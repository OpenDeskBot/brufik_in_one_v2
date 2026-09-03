"""update_user_info / update_daily_task 工具：schema 注册与执行落盘。"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def data_dir(monkeypatch, tmp_path):
    from deskbot_server.utils import device_data as dd

    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(dd, "DATA_DIR", d)
    return d


def test_schemas_registered_without_touching_batch1():
    from deskbot_server.infrastructure.llm.tool_schema import (
        NATIVE_TOOL_NAMES_BATCH1,
        build_native_tool_schemas,
    )

    schemas = build_native_tool_schemas(device_id=None)
    names = [s["function"]["name"] for s in schemas]
    assert names[:6] == NATIVE_TOOL_NAMES_BATCH1  # batch1 顺序与集合不受影响
    assert "update_user_info" in names and "update_daily_task" in names
    # include_batch2=False（batch1 专用视图）不带社交工具
    names1 = [s["function"]["name"] for s in build_native_tool_schemas(include_batch2=False)]
    assert names1 == list(NATIVE_TOOL_NAMES_BATCH1)

    info = next(s for s in schemas if s["function"]["name"] == "update_user_info")
    assert info["function"]["parameters"]["required"] == ["user_name", "chat_message"]
    assert "user_name" in info["function"]["parameters"]["properties"]
    task = next(s for s in schemas if s["function"]["name"] == "update_daily_task")
    assert task["function"]["parameters"]["required"] == ["user_name", "message"]


def test_execute_update_user_info_and_daily_task(data_dir):
    from deskbot_server.service.application.llm_tool_runner import execute_llm_tools

    async def _go():
        return await execute_llm_tools(
            [
                {"tool": "update_user_info", "user_name": "小明", "chat_message": "我叫小明，今年10岁"},
                {"tool": "update_user_info", "name": "小明", "text": "我喜欢乐高"},
                {"tool": "update_daily_task", "user_name": "小红", "message": "我问了小红中午吃了什么"},
                {"tool": "update_daily_task", "user_name": "小红", "message": "我问了小红中午吃了什么"},
            ],
            device_id="dev1",
        )

    out = asyncio.run(_go())
    assert [r["ok"] for r in out] == [True, True, True, True]
    assert out[0]["created"] is True and out[0]["user_name"] == "小明"
    assert out[1]["created"] is False and out[1]["deduped"] is False  # 不同秒前缀不同 → 正常追加
    assert out[2]["created"] is True
    assert out[3]["deduped"] is True  # 同秒同内容 → 行级去重
    info_lines = (data_dir / "dev1" / "user_info_小明.txt").read_text(encoding="utf-8").splitlines()
    assert len(info_lines) == 2
    assert info_lines[0].endswith("我叫小明，今年10岁")
    import re as _re

    done_files = list((data_dir / "dev1").glob("done_list_小红_*.txt"))
    assert len(done_files) == 1 and _re.fullmatch(r"done_list_小红_\d{8}\.txt", done_files[0].name)
    lines = done_files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].endswith("我问了小红中午吃了什么")
    assert _re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", lines[0])  # 服务端补时间前缀


def test_execute_tools_error_paths(data_dir):
    from deskbot_server.service.application.llm_tool_runner import execute_llm_tools

    async def _go():
        return await execute_llm_tools(
            [
                {"tool": "update_user_info", "chat_message": "没有用户名"},
                {"tool": "update_user_info", "user_name": "小明", "chat_message": ""},
                {"tool": "update_user_info", "user_name": "a/b", "chat_message": "非法名"},
                {"tool": "update_daily_task", "user_name": "小红", "message": "x"},
                {"tool": "not_a_tool", "user_name": "小红", "message": "x"},
            ],
            device_id="dev1",
        )

    out = asyncio.run(_go())
    assert out[0]["ok"] is False and "user_name" in out[0]["error"]
    assert out[1]["ok"] is False and "chat_message" in out[1]["error"]
    assert out[2]["ok"] is False and "非法字符" in out[2]["error"]
    assert out[3]["ok"] is True
    assert out[4]["ok"] is False and "未知工具" in out[4]["error"]


def test_tools_require_device_id():
    from deskbot_server.service.application.llm_tool_runner import execute_llm_tools

    out = asyncio.run(
        execute_llm_tools([{"tool": "update_user_info", "user_name": "小明", "chat_message": "x"}])
    )
    assert out[0]["ok"] is False and "device_id" in out[0]["error"]


def test_parse_unwraps_openai_nested_function_shape(data_dir):
    """回归：模型在文本 JSON 里输出 OpenAI 嵌套 function-call 形状（真实事故现场）。"""
    from deskbot_server.infrastructure.llm.utils import parse_llm_reply

    raw = (
        '{"need_reply": true, "tts": "好嘞小明，海淀区我记下啦。", "tools": ['
        '{"type": "function", "function": {"name": "update_user_info", '
        '"arguments": {"user": "小明", "location": "北京市海淀区"}}}]}'
    )
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    tools = parsed["tools"]
    assert len(tools) == 1
    row = tools[0]
    assert row["tool"] == "update_user_info"
    assert row["user"] == "小明" and row["location"] == "北京市海淀区"  # arguments 并入平铺键
    assert "function" not in row and "type" not in row


def test_executor_tolerates_loose_arg_keys(data_dir):
    """模型用散键/近似键调用时仍能归档（user/location/无 chat_message）。"""
    from deskbot_server.service.application.llm_tool_runner import execute_llm_tools

    out = asyncio.run(
        execute_llm_tools(
            [
                # 嵌套形状 + 散键：user 当人名、location 当事实（无 chat_message → 拼键归档）
                {"tool": "update_user_info", "user": "小明", "location": "北京市海淀区", "age": 10},
                # user_name/content 直给
                {"tool": "update_user_info", "user_name": "小红", "content": "我养了两只猫"},
                # 近似键名 user/msg
                {"tool": "update_daily_task", "user": "小明", "msg": "我问了小明中午吃了什么"},
            ],
            device_id="dev1",
        )
    )
    assert [r["ok"] for r in out] == [True, True, True]
    lines = (data_dir / "dev1" / "user_info_小明.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].endswith("location: 北京市海淀区，age: 10")
    lines2 = (data_dir / "dev1" / "user_info_小红.txt").read_text(encoding="utf-8").splitlines()
    assert lines2[0].endswith("我养了两只猫")
    done = list((data_dir / "dev1").glob("done_list_小明_*.txt"))[0].read_text(encoding="utf-8").strip()
    assert done.endswith("我问了小明中午吃了什么")
