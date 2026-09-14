"""收尾轮纯文本处理 + ``strip_llm_preamble`` 清洗测试。

背景：收尾轮 content 不是 JSON envelope 时，原实现无条件丢弃 content、重发
``_legacy_final_round()``。实测该路径下重发有相当比例返回
``{"need_reply": false, "tts": ""}``——一句本该说出口的回复既没播报也没进历史，
下一轮模型自然把同一个问题再问一遍。这里钉住修复后的行为。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(db_path)
        init_database()
        yield db_path
        # 必须收尾：临时目录随 with 块删除，若 engine 仍指向它，同进程后续测试
        # 会以 "unable to open database file" 失败（取决于执行顺序，很难排查）
        reset_engine()


def _round(content: str = "", tool_calls: list | None = None):
    from deskbot_server.infrastructure.llm.openai_compat import LlmToolRoundResult

    return LlmToolRoundResult(content=content, tool_calls=list(tool_calls or []), meta={})


def _silent_env() -> str:
    return json.dumps(
        {"need_reply": False, "tts": "", "gesture": [], "expression": []}, ensure_ascii=False
    )


# ────────────────────── strip_llm_preamble ──────────────────────


def test_strip_llm_preamble_extracts_tts_from_envelope_with_cot():
    """推理前言 + JSON envelope（实测最常见的污染形态）→ 只取 tts。"""
    from deskbot_server.infrastructure.llm.utils import strip_llm_preamble

    raw = (
        "声纹判定为陌生人，画面识别到小明。身份不一致，不硬套小明名字。\n\n"
        '{"need_reply": true, "tts": "小明，你周末一般都爱做点啥呀？", '
        '"gesture": [], "expression": []}'
    )
    assert strip_llm_preamble(raw) == "小明，你周末一般都爱做点啥呀？"


def test_strip_llm_preamble_silent_envelope_returns_empty():
    """静默轮（tts 空）必须返回空串，绝不能拿整段 JSON 充数。"""
    from deskbot_server.infrastructure.llm.utils import strip_llm_preamble

    raw = '声纹判定为陌生人，保持安静。\n\n{"need_reply": false, "tts": "", "gesture": [], "expression": []}'
    assert strip_llm_preamble(raw) == ""
    assert strip_llm_preamble("") == ""


def test_strip_llm_preamble_plain_text_takes_last_line():
    """无 envelope 的纯文本：取最后一段非空行，丢掉推理前言。"""
    from deskbot_server.infrastructure.llm.utils import strip_llm_preamble

    assert strip_llm_preamble("这个人我不认识，还在识别中，先不打扰。\n小明，你说啥呀？") == "小明，你说啥呀？"
    assert strip_llm_preamble("小明，你周末一般都爱做点啥呀？") == "小明，你周末一般都爱做点啥呀？"


# ────────────────────── 收尾轮分支 ──────────────────────


def test_plain_text_final_round_adopted_without_resend(temp_db):
    """纯文本收尾应直接采纳，不再触发第二次 LLM 调用（省一次约 2.9s 往返）。"""
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    chat = AsyncMock()
    chat.llm_tool_round = AsyncMock(
        side_effect=[
            _round(tool_calls=[{"id": "c1", "name": "memory_add", "arguments": json.dumps({"text": "喜欢猫"})}]),
            _round(content="小明，你周末一般都爱做点啥呀？"),
        ]
    )

    turn = asyncio.run(
        complete_llm_with_tool_loop(chat, "记住我喜欢猫", device_id="deskbot_a", request_id="req_plain")
    )

    assert turn.parsed["reply"] == "小明，你周末一般都爱做点啥呀？"
    assert turn.parsed["need_reply"] is True
    chat.llm.assert_not_called()  # 关键：没有再发一轮
    assert len(turn.llm_calls) == 2


def test_plain_text_final_round_strips_cot_preamble(temp_db):
    """纯文本里夹着推理前言时，只采纳最后一行口语。"""
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    chat = AsyncMock()
    chat.llm_tool_round = AsyncMock(
        side_effect=[
            _round(tool_calls=[{"id": "c1", "name": "memory_add", "arguments": json.dumps({"text": "喜欢猫"})}]),
            _round(content="我方才有段听不清。\n小明，你再说一遍好不好？"),
        ]
    )

    turn = asyncio.run(
        complete_llm_with_tool_loop(chat, "记住我喜欢猫", device_id="deskbot_a", request_id="req_cot")
    )
    assert turn.parsed["reply"] == "小明，你再说一遍好不好？"
    chat.llm.assert_not_called()


def test_broken_json_final_round_still_resends(temp_db):
    """坏 JSON（以 { 开头）仍走重发——不能把半截 JSON 当口语念出来。"""
    from deskbot_server.service.application.chat_flow import complete_llm_with_tool_loop

    chat = AsyncMock()
    chat.llm_tool_round = AsyncMock(
        side_effect=[
            _round(tool_calls=[{"id": "c1", "name": "memory_add", "arguments": json.dumps({"text": "喜欢猫"})}]),
            _round(content='{"need_reply": tru'),
        ]
    )
    chat.llm = AsyncMock(return_value=json.dumps(
        {"need_reply": True, "tts": "重发后的回复", "gesture": [], "expression": []}, ensure_ascii=False
    ))

    turn = asyncio.run(
        complete_llm_with_tool_loop(chat, "记住我喜欢猫", device_id="deskbot_a", request_id="req_broken")
    )
    assert turn.parsed["reply"] == "重发后的回复"
    chat.llm.assert_awaited_once()


def test_quote_need_reply_in_prose_is_not_treated_as_json(temp_db):
    """含 ``"need_reply"`` 字样的输出不许当口语采纳（防半截 JSON 被朗读）。"""
    from deskbot_server.service.application.chat_flow import _looks_like_plain_reply, _session_user_text

    assert _looks_like_plain_reply('{"need_reply": true}') is False
    assert _looks_like_plain_reply('前言… "need_reply": false, "tts": ""') is False
    # 不含引号的普通口语照常采纳（"need_reply" 带引号才是 JSON 形态）
    assert _looks_like_plain_reply("小明，你再说一遍好不好？") is True
    assert _looks_like_plain_reply("") is False
    # 用户真实语音轮原样保留
    assert _session_user_text("小明，你周末都干啥？", is_system_round=False) == "小明，你周末都干啥？"


# ────────────────────── 系统轮历史压缩 ──────────────────────


def test_session_user_text_compacts_system_rounds():
    """系统轮进历史时压成紧凑标识，避免任务 prompt 原文反复喂回模型。"""
    from deskbot_server.service.application.chat_flow import _session_user_text

    # 重构前格式
    old = "[系统剧情推进] 主人约 1 分钟没有和本机器人对话…：[g_city] 位置\n  goal：获取用户所在位置"
    assert _session_user_text(old, is_system_round=True) == "[系统剧情推进] g_city 位置"

    # 重构后格式
    new = "[系统剧情推进] 主人约 1 分钟…：[g_task3]（长期）：尽量收集用户的个人兴趣…"
    assert _session_user_text(new, is_system_round=True) == "[系统剧情推进] g_task3 长期"

    social = "[系统主动问候] 检测到认识的人（小明）在面前，且有一段时间没有对话，现在轮到你主动开口。"
    assert _session_user_text(social, is_system_round=True) == "[系统主动问候] 小明"

    sched = "[系统定时任务] 请向主人朗声提醒并执行以下任务：提醒你到时间休息了\n要求：need_reply 必须为 true"
    assert _session_user_text(sched, is_system_round=True) == "[系统定时任务] 提醒你到时间休息了"


def test_session_user_text_real_proactive_payloads():
    """用真实主动轮 payload（照 quest_proactive/social_proactive 的构造原样）钉住压缩结果。"""
    from deskbot_server.service.application.chat_flow import _session_user_text

    # quest_proactive._build_user_text 的 long_term 形态
    quest = (
        "[系统剧情推进] 主人约 1 分钟没有和本机器人对话，但人就在面前，现在需要主动推进剧情任务："
        "[g_task3]（长期）：* 尽量收集用户的个人兴趣/偏好/生活习惯/业余爱好，家庭信息/住址/成员/亲戚；"
        "即便已经收集一些信息了，也可以借机补充更多。\n"
        "  - 长期任务：可自然聊聊相关话题；只在此次对话取得实质新进展时调 complete_task。\n"
        "要求：有可开口推进/问候/记录的事项就开口；若该任务今日已对当前用户记录过，就自然闲聊或保持安静。"
    )
    # social_proactive._build_user_text 的形态
    social = (
        "[系统主动问候] 检测到认识的人（小明）在面前，且有一段时间没有对话，现在轮到你主动开口。"
        "请依据 system 提示中的「你的当前任务」规则判断：\n- 属于该主动表达的情形：先调用 update_daily_task 记账…"
    )
    assert _session_user_text(quest, is_system_round=True) == "[系统剧情推进] g_task3 长期"
    assert _session_user_text(social, is_system_round=True) == "[系统主动问候] 小明"


def test_session_user_text_is_idempotent():
    """对已压缩文本再压一次必须返回原值。

    清洗脚本会反复跑；非幂等会让每跑一次就丢一点信息（任务号/人名被逐次吃掉），
    永远收敛不到稳定状态——这个坑已经踩过一次。
    """
    from deskbot_server.service.application.chat_flow import _session_user_text

    raws = [
        "[系统剧情推进] 主人约 1 分钟…：[g_city] 位置\n  goal：获取用户所在位置",
        "[系统剧情推进] 主人约 1 分钟…：[g_task3]（长期）：* 尽量收集…\n要求：…",
        "[系统主动问候] 检测到认识的人（小明）在面前，且有一段时间没有对话，现在轮到你主动开口。请依据…",
        "[系统定时任务] 请向主人朗声提醒并执行以下任务：提醒你到时间休息了\n要求：need_reply 必须为 true",
        "[系统剧情推进] 格式完全不认识的文本",
    ]
    for raw in raws:
        once = _session_user_text(raw, is_system_round=True)
        twice = _session_user_text(once, is_system_round=True)
        assert once == twice, f"非幂等：{raw!r} -> {once!r} -> {twice!r}"
        # 压缩后必须比原文短，且不残留换行
        assert len(once) <= len(raw) and "\n" not in once


def test_session_user_text_falls_back_to_prefix_on_unknown_shape():
    """认不出任务号时保留首行短标识，绝不返回整段指令原文。"""
    from deskbot_server.service.application.chat_flow import _session_user_text

    out = _session_user_text("[系统剧情推进] 格式完全不认识的文本", is_system_round=True)
    assert out.startswith("[系统剧情推进]")
    long_raw = "[系统剧情推进] " + "很长的未知指令" * 20
    assert len(_session_user_text(long_raw, is_system_round=True)) < len(long_raw)
