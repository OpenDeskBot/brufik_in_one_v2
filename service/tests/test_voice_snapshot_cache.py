"""voice_snapshot_cache：快照 seq 写守卫 + 注册样本槽测试。"""

from __future__ import annotations

import time

import pytest

from deskbot_server.service.application import voice_snapshot_cache as snap


@pytest.fixture(autouse=True)
def clean_cache():
    snap.clear_all_devices()
    yield
    snap.clear_all_devices()


def test_begin_clears_previous_result():
    seq1 = snap.begin_identification("dev1", "r1")
    snap.finish_identification("dev1", seq1, state=snap.STATE_FOUND, name="小明", score=0.8)
    assert snap.get_voice_snapshot("dev1")["state"] == snap.STATE_FOUND

    seq2 = snap.begin_identification("dev1", "r2")  # 新句开始 → 置 identifying 并清旧名字
    cur = snap.get_voice_snapshot("dev1")
    assert cur["state"] == snap.STATE_IDENTIFYING
    assert cur["seq"] == seq2 == seq1 + 1
    assert cur["name"] is None
    assert cur["score"] is None


def test_finish_only_latest_seq_writes():
    seq1 = snap.begin_identification("dev1", "r1")
    seq2 = snap.begin_identification("dev1", "r2")
    # 旧 seq 的结果写不进（防串句错名）
    assert not snap.finish_identification("dev1", seq1, state=snap.STATE_FOUND, name="小明", score=0.9)
    cur = snap.get_voice_snapshot("dev1")
    assert cur["state"] == snap.STATE_IDENTIFYING
    # 当前 seq 可写
    assert snap.finish_identification("dev1", seq2, state=snap.STATE_UNKNOWN)
    cur = snap.get_voice_snapshot("dev1")
    assert cur["state"] == snap.STATE_UNKNOWN
    assert cur["name"] is None


def test_snapshot_none_when_idle_or_unknown_device():
    assert snap.get_voice_snapshot("dev-no-record") is None
    assert snap.get_voice_snapshot("") is None
    snap.begin_identification("dev2", "r1")
    cur = snap.get_voice_snapshot("dev2")
    assert cur is not None and cur["state"] == snap.STATE_IDENTIFYING


def test_finish_clears_stale_identifying():
    """mode 关闭后 clear 会清除 pending 快照：旧 seq finish 不再生效。"""
    snap.begin_identification("dev3", "r1")
    snap.clear_device("dev3")
    assert not snap.finish_identification("dev3", 1, state=snap.STATE_FOUND, name="小明", score=0.9)
    assert snap.get_voice_snapshot("dev3") is None


def test_clear_device_resets_snapshot_and_sample():
    snap.begin_identification("dev4", "r1")
    snap.store_voice_sample("dev4", "r1", [1.0] * 256)
    snap.clear_device("dev4")
    assert snap.get_voice_snapshot("dev4") is None
    assert snap.take_voice_sample("dev4", max_age_s=60) is None


def test_sample_store_and_take_with_age_expiry():
    emb = [float(i) for i in range(256)]
    snap.store_voice_sample("dev5", "r1", emb)
    assert snap.take_voice_sample("dev5", max_age_s=60) == emb

    snap._samples["dev5"]["ts"] = time.time() - 100.0  # noqa: SLF001 直改时间戳模拟过期
    assert snap.take_voice_sample("dev5", max_age_s=60) is None
    assert snap.take_voice_sample("dev5", max_age_s=200) == emb  # 放宽有效期仍可取


def test_store_voice_sample_requires_device():
    snap.store_voice_sample("", "r1", [1.0] * 256)
    assert snap.take_voice_sample("", max_age_s=60) is None
