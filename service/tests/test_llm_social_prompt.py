"""社交情境注入 system prompt 测试：recognized_known_users 与三段新附录组装。"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """临时 data 目录 + 临时数据库（build_llm_system_prompt 内部读长期记忆表）。"""
    from deskbot_server.utils import device_data as dd

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(dd, "DATA_DIR", data_dir)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DESKBOT_DB_PATH", str(Path(tmp) / "test.db"))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(Path(tmp) / "test.db")
        init_database()
        yield data_dir
        reset_engine()


def _patch_sights(monkeypatch, *, faces=None, voice=None):
    """伪造人脸/声纹快照：faces=[(name, score)]；voice=name 或 None(无判定)。"""
    import deskbot_server.service.application.face_snapshot_cache as fsc
    import deskbot_server.service.application.voice_snapshot_cache as vsc

    monkeypatch.setattr(
        fsc, "list_recognized_faces", lambda dev, limit=5: [{"person_name": n, "identity_score": s} for n, s in (faces or [])]
    )
    if voice is None:
        monkeypatch.setattr(vsc, "get_voice_snapshot", lambda dev: {"state": "unknown", "name": None})
    else:
        monkeypatch.setattr(vsc, "get_voice_snapshot", lambda dev: {"state": "found", "name": voice})


def _freeze_clock(monkeypatch):
    import deskbot_server.dao.user_social_store as s

    frozen = dt.datetime(2026, 9, 3, 8, 0, 0)
    monkeypatch.setattr(s, "_beijing_now", lambda: frozen)


def _build(device_id="dev1"):
    from deskbot_server.infrastructure.llm.utils import build_llm_system_prompt

    return build_llm_system_prompt("你是助手", device_id=device_id)


def test_no_known_users_keeps_old_prompt(env, monkeypatch):
    _patch_sights(monkeypatch, faces=[], voice=None)
    sp = _build()
    assert "你的当前任务" not in sp
    assert "上一次对话的时间是" not in sp
    assert "的资料（" not in sp
    assert "已识别到认识的人" not in sp
    assert sp.endswith("（北京时间，东八区）")


def test_both_face_and_voice_users_injected_with_files(env, monkeypatch):
    _freeze_clock(monkeypatch)
    from deskbot_server.dao.user_social_store import (
        append_daily_task_line,
        append_user_info_line,
        stamp_user_last_talk,
    )

    # 人脸=小明(0.9)+小红(0.7)，声纹=小红（不一致 → 两人都注入；声纹名优先展示）
    append_user_info_line("dev1", "小明", "我叫小明，今年10岁，喜欢乐高")
    append_daily_task_line("dev1", "小明", "我跟小明说了早上好")
    stamp_user_last_talk("dev1", "小明")
    append_daily_task_line("dev1", "小红", "我问了小红中午吃了什么")
    _patch_sights(monkeypatch, faces=[("小明", 0.9), ("小红", 0.7)], voice="小红")

    from deskbot_server.infrastructure.llm.utils import recognized_known_users

    assert recognized_known_users("dev1") == ["小红", "小明"]  # 声纹说话人优先
    sp = _build()
    assert "小红 的资料" in sp and "小明 的资料" in sp  # 两人档案块
    assert "小红 今日已完成的主动问候/关心" in sp
    assert "今日暂无主动互动记录" not in sp  # 两人今日都有记录
    assert "我叫小明，今年10岁，喜欢乐高" in sp
    assert "我问了小红中午吃了什么" in sp
    assert "你的当前任务" in sp
    # 上一次对话行拼在当前时间之后
    assert sp.rfind("与小明上一次对话的时间是2026-09-03 08:00:00") > sp.rfind("当前时间是:")


def test_last_talk_only_when_recorded(env, monkeypatch):
    _freeze_clock(monkeypatch)
    from deskbot_server.dao.user_social_store import stamp_user_last_talk

    stamp_user_last_talk("dev1", "小明")
    _patch_sights(monkeypatch, faces=[("小明", 0.9)], voice=None)
    sp = _build()
    assert "与小明上一次对话的时间是2026-09-03 08:00:00" in sp

    # 小红无打点记录 → 无其行
    _patch_sights(monkeypatch, faces=[("小明", 0.9), ("小红", 0.8)], voice=None)
    sp2 = _build()
    assert "与小明上一次对话的时间是" in sp2
    assert "与小红上一次对话的时间是" not in sp2


def test_missing_files_show_placeholder_not_error(env, monkeypatch):
    _patch_sights(monkeypatch, faces=[("陌生人档案未建", 0.5)], voice=None)
    from deskbot_server.infrastructure.llm.utils import recognized_known_users

    names = recognized_known_users("dev1")
    sp = _build()
    if names:  # 名字本身合法即会注入空占位
        assert "暂无已记录的自我介绍，不要编造用户资料" in sp
        assert "（今日暂无主动互动记录）" in sp


def test_recognized_users_capped_at_three(env, monkeypatch):
    _patch_sights(
        monkeypatch,
        faces=[("甲", 0.99), ("乙", 0.88), ("丙", 0.77), ("丁", 0.66)],
        voice=None,
    )
    from deskbot_server.infrastructure.llm.utils import recognized_known_users

    assert recognized_known_users("dev1") == ["甲", "乙", "丙"]  # 按分取前 3，丢低分


def test_social_context_total_char_cap(env, monkeypatch):
    # 每个用户档案都塞满（超读侧 30 行/600 字符上限，含截断头注）→
    # 单用户块 ≈700 字符，三人累计超 2000 → 第三人在预算处被丢弃
    _patch_sights(monkeypatch, faces=[("小明", 0.9), ("小红", 0.8), ("小刚", 0.7)], voice=None)
    from deskbot_server.dao.user_social_store import user_file_stem
    from deskbot_server.utils.device_data import device_data_dir

    dev_dir = device_data_dir("dev1")
    dev_dir.mkdir(parents=True, exist_ok=True)
    for name in ("小明", "小红", "小刚"):
        p = dev_dir / f"user_info_{user_file_stem(name)}.txt"
        lines = [f"2026-09-03 08:00:{i % 60:02d} " + "爱好是收藏邮票，" * 8 for i in range(120)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sp = _build()
    assert "小明 的资料" in sp and "小红 的资料" in sp
    assert "小刚 的资料" not in sp  # 总字符预算内停止追加后续用户


def test_direct_read_reflects_new_writes(env, monkeypatch):
    """附录直读不缓存：写入新行后再次 build 立即可见。"""
    from deskbot_server.dao.user_social_store import append_user_info_line

    append_user_info_line("dev1", "小明", "我叫小明")
    _patch_sights(monkeypatch, faces=[("小明", 0.9)], voice=None)
    assert "我叫小明" in _build()
    append_user_info_line("dev1", "小明", "我养了一只猫")
    assert "我养了一只猫" in _build()


def test_build_no_legacy_text_tools_ad(env):
    """默认（None）语义 = 原生名单 directive：legacy 文本 tools 广告段整体移除。"""
    from deskbot_server.infrastructure.llm.utils import build_llm_system_prompt

    sp = build_llm_system_prompt("你是助手", device_id="nobody")
    # 原生 directive 在，且不出现文本 JSON 形式的工具写法
    assert "可用工具（原生 function calling）" in sp
    assert '{"tool":"register_face"' not in sp
    assert "register_face:" not in sp
    assert '{"tool":"memory_add"' not in sp
    # 剧情工具文本契约段不再注入（native schema 已含契约）
    assert "update_task_result：" not in sp


def test_build_no_tools_mention_when_names_empty(env):
    """收口/无工具轮（native_tool_names=[]）→ prompt 完全不提工具。"""
    from deskbot_server.infrastructure.llm.utils import build_llm_system_prompt

    sp = build_llm_system_prompt("你是助手", device_id="nobody", native_tool_names=[])
    assert "可用工具" not in sp
    assert "memory_add" not in sp
    assert "register_face" not in sp
    assert "当前时间是: " in sp  # 其余情境段不受影响
