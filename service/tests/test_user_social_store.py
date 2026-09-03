"""user_social_store 三文件读写：路径/时间前缀/去重/裁剪/非法名防护。"""

from __future__ import annotations

import datetime as dt
import re

import pytest


@pytest.fixture()
def data_dir(monkeypatch, tmp_path):
    """把 data 根目录重定向到 tmp_path（镜像 test_device_data.py 做法）。"""
    from deskbot_server.utils import device_data as dd

    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(dd, "DATA_DIR", d)
    return d


@pytest.fixture()
def freeze_clock(monkeypatch):
    """钉死北京时钟（文件前缀与日期均确定）。"""
    import deskbot_server.dao.user_social_store as s

    frozen = dt.datetime(2026, 9, 3, 8, 0, 0)
    monkeypatch.setattr(s, "_beijing_now", lambda: frozen)

    def _shift(seconds: int):
        monkeypatch.setattr(s, "_beijing_now", lambda: frozen + dt.timedelta(seconds=seconds))

    return _shift


def _store():
    from deskbot_server.dao import user_social_store as s

    return s


def test_append_user_info_creates_and_reads(data_dir, freeze_clock):
    s = _store()
    r = s.append_user_info_line("dev1", "小明", "我叫小明，今年10岁")
    assert r["ok"] is True and r["created"] is True and r["line_count"] == 1
    p = data_dir / "dev1" / "user_info_小明.txt"
    assert p.is_file()
    assert p.read_text(encoding="utf-8") == "2026-09-03 08:00:00 我叫小明，今年10岁\n"
    # 第二次追加：created=False、时间随 clock 前进
    freeze_clock(10)
    r2 = s.append_user_info_line("dev1", "小明", "我喜欢乐高")
    assert r2["created"] is False and r2["line_count"] == 2
    block = s.read_user_info_block("dev1", "小明")
    assert block is not None
    lines = block.splitlines()
    assert len(lines) == 2 and lines[0].startswith("2026-09-03 08:00:00")


def test_done_list_filename_uses_beijing_date(data_dir, freeze_clock):
    s = _store()
    s.append_daily_task_line("dev1", "小红", "小红说要吃面条")
    files = sorted(p.name for p in (data_dir / "dev1").iterdir())
    assert files == ["done_list_小红_20260903.txt"]
    line = (data_dir / "dev1" / files[0]).read_text(encoding="utf-8").strip()
    assert line.startswith("2026-09-03 08:00:00 小红说要吃面条")


def test_daily_task_prefix_preserved_or_prepended(data_dir, freeze_clock):
    s = _store()
    # 自带时间前缀（- / / / T 分隔）原样保留
    for msg in (
        "2026-09-03 08:01:00 我跟小明说了早上好",
        "2026/09/03 08:02 我问了小明中午吃了什么",
        "2026-09-03T08:03 小明说要吃面条",
    ):
        s.append_daily_task_line("dev1", "小明", msg)
    # 无前缀 → 服务端补当前时间
    s.append_daily_task_line("dev1", "小明", "我跟小明说了下午好")
    lines = (data_dir / "dev1" / "done_list_小明_20260903.txt").read_text(encoding="utf-8").splitlines()
    assert lines == [
        "2026-09-03 08:01:00 我跟小明说了早上好",
        "2026/09/03 08:02 我问了小明中午吃了什么",
        "2026-09-03T08:03 小明说要吃面条",
        "2026-09-03 08:00:00 我跟小明说了下午好",
    ]


def test_dedup_same_content_same_second(data_dir, freeze_clock):
    s = _store()
    s.append_user_info_line("dev1", "小明", "我喜欢猫")
    r = s.append_user_info_line("dev1", "小明", "我喜欢猫")  # 同秒 → 前缀相同 → 行级去重
    assert r["deduped"] is True and r["line_count"] == 1
    freeze_clock(1)
    r2 = s.append_user_info_line("dev1", "小明", "我喜欢猫")  # 下一秒前缀不同 → 正常追加
    assert r2["deduped"] is False and r2["line_count"] == 2


def test_user_info_trim_keeps_recent(data_dir, freeze_clock):
    s = _store()
    for i in range(205):
        freeze_clock(i)
        s.append_user_info_line("dev1", "小红", f"第{i}条")
    p = data_dir / "dev1" / "user_info_小红.txt"
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= s.USER_INFO_MAX_LINES
    assert lines[-1].endswith("第204条")  # 最新一条仍在
    assert not any(ln.endswith("第0条") for ln in lines)  # 最旧被裁掉
    # 读侧另有默认上限（30 行）→ 返回截断头注
    block = s.read_user_info_block("dev1", "小红")
    assert block is not None
    blines = block.splitlines()
    assert len(blines) <= 31
    assert blines[0].startswith("（记录较多，仅显示最近")


def test_last_talk_overwrite_and_read(data_dir, freeze_clock):
    s = _store()
    assert s.read_user_last_talk("dev1", "小明") is None  # 无文件 → None
    ts1 = s.stamp_user_last_talk("dev1", "小明")
    freeze_clock(5)
    ts2 = s.stamp_user_last_talk("dev1", "小明")
    assert ts1 != ts2 and ts2 == "2026-09-03 08:00:05"
    p = data_dir / "dev1" / "user_last_talk_小明.txt"
    assert p.read_text(encoding="utf-8") == "2026-09-03 08:00:05\n"  # 覆盖写单行
    assert s.read_user_last_talk("dev1", "小明") == "2026-09-03 08:00:05"


def test_name_validation_and_stem_fallback(data_dir):
    s = _store()
    ok_names = ["小明", "Zhang San", "xiaoming_1", "李-雷", "张.三", "a" * 32]
    for name in ok_names:
        assert s.validate_user_name(name) == name.strip()
    assert s.validate_user_name("  小明  ") == "小明"  # 首尾空白先 strip（不算非法）
    for bad in ["", "a/b", "..", "a\\b", "a" * 33, "a:b", "小\t明"]:
        with pytest.raises(ValueError):
            s.validate_user_name(bad)
    # 常规名原样作文件名主干；非常规名回退 hash，读/写同函数保持一致
    assert s.user_file_stem("小明") == "小明"
    stem = s.user_file_stem("a/b")
    assert stem.startswith("u_") and re.fullmatch(r"u_[0-9a-f]{10}", stem)
    assert s.user_file_stem("a/b") == stem  # 两次一致 → 读写同文件
    with pytest.raises(ValueError):
        s.user_file_stem("")
