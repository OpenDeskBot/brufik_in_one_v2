"""VoiceprintService / 档案 / LLM 注入 / 注册链路测试（fake 声纹引擎 + 内存 DB）。"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from deskbot_server.infrastructure.llm.utils import build_llm_user_message
from deskbot_server.infrastructure.voice.vpr_http_client import VprHttpError
from deskbot_server.service.application import voice_snapshot_cache as snap
from deskbot_server.service.application.tool_interim_tts import build_tool_interim_tts, phrase_for_tool
from deskbot_server.service.application.voice_registration import register_voice_for_device
from deskbot_server.service.voice_profile_service import (
    delete_voice_profile,
    list_voice_profiles_summary,
    load_voice_profiles,
    update_voice_profile_name,
    upsert_voice_profile,
)
from deskbot_server.service.voiceprint_service import VoiceprintService, build_voiceprint_runtime

DV = "voice-test-dev"


# ────────────────── 基建 ─────────────────────────


def _axis(k: int) -> list[float]:
    v = [0.0] * 256
    v[k] = 1.0
    return v


def _combo(a: float, b: float) -> list[float]:
    """轴 0 分量 a、轴 1 分量 b 的单位向量：与 _axis(0) 余弦 = a/√(a²+b²)。"""
    n = math.sqrt(a * a + b * b)
    v = [0.0] * 256
    v[0] = a / n
    v[1] = b / n
    return v


A = _axis(0)   # 小明档案向量
B = _axis(1)   # 小红档案向量
E = _axis(7)   # 与 A/B 均正交（未匹配）


@pytest.fixture()
def db_env(monkeypatch, tmp_path):
    """初始化内存 DB，返回 device_id。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setattr("deskbot_server.utils.device_data.ensure_device_data_initialized", lambda _did: False)
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    reset_engine()
    init_engine(db_path)
    init_database()
    return DV


class _FakeVpr:
    """替身 vpr-engine 客户端：注入 embedding 队列 / 异常 / 门控。"""

    base_url = ""
    embeddings: list[list[float]] = []
    errors: list[BaseException] = []
    calls = 0
    gate: asyncio.Event | None = None
    started: asyncio.Event | None = None

    def __init__(self, base_url: str, timeout_s: float = 5.0) -> None:
        self.base_url = base_url

    async def embedding(self, pcm_bytes: bytes, sample_rate: int = 16000) -> list[float]:
        type(self).calls += 1
        if type(self).gate is not None:
            type(self).started.set()
            await type(self).gate.wait()
        if type(self).errors:
            raise type(self).errors.pop(0)
        if type(self).embeddings:
            return list(type(self).embeddings.pop(0))
        return list(A)

    @classmethod
    def reset(cls) -> None:
        cls.embeddings = []
        cls.errors = []
        cls.calls = 0
        cls.gate = None
        cls.started = None


def _enable_vpr(monkeypatch) -> None:
    """configure 单例为 vpr 模式（fake 客户端）。"""
    monkeypatch.setattr("deskbot_server.service.voiceprint_service.VprHttpClient", _FakeVpr)
    VoiceprintService().configure(
        build_voiceprint_runtime(
            {
                "voiceprint": {
                    "mode": "vpr",
                    "external_url": "http://127.0.0.1:9240",
                    "external_timeout_s": 1,
                    "identity_similarity_threshold": 0.5,
                    "sample_max_age_s": 60,
                }
            }
        )
    )


@pytest.fixture(autouse=True)
def _reset_svc(monkeypatch):
    VoiceprintService().configure(build_voiceprint_runtime({}))  # mode=none：清快照/样本
    VoiceprintService()._locks.clear()  # noqa: SLF001 锁绑定旧 event loop，测试间清空
    _FakeVpr.reset()
    yield
    VoiceprintService().configure(build_voiceprint_runtime({}))
    VoiceprintService()._locks.clear()  # noqa: SLF001
    _FakeVpr.reset()


# ────────────────── identify 终态 ─────────────────────────


def test_mode_none_zero_traffic(monkeypatch, db_env):
    """mode=none：identify 直接返回，零 embedding 调用、快照不变。"""
    _FakeVpr.calls = 0
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1"))
    assert _FakeVpr.calls == 0
    assert snap.get_voice_snapshot(DV) is None


def test_identify_found_writes_snapshot_name(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    upsert_voice_profile(DV, name="小明", descriptor=A, merge_threshold=1.01)
    _FakeVpr.embeddings = [A]
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1"))
    cur = snap.get_voice_snapshot(DV)
    assert cur["state"] == snap.STATE_FOUND
    assert cur["name"] == "小明"
    assert cur["score"] == pytest.approx(1.0, abs=1e-3)


def test_identify_below_threshold_writes_unknown(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    upsert_voice_profile(DV, name="小明", descriptor=A, merge_threshold=1.01)
    _FakeVpr.embeddings = [E]  # 与档案正交
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1"))
    cur = snap.get_voice_snapshot(DV)
    assert cur["state"] == snap.STATE_UNKNOWN
    assert cur["name"] is None


def test_identify_multi_profile_picks_best(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    upsert_voice_profile(DV, name="小明", descriptor=A, merge_threshold=1.01)
    upsert_voice_profile(DV, name="小红", descriptor=B, merge_threshold=1.01)
    _FakeVpr.embeddings = [_combo(0.6, 0.8)]  # 与小明 0.6、与小红 0.8 → 应判小红
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1"))
    cur = snap.get_voice_snapshot(DV)
    assert cur["state"] == snap.STATE_FOUND
    assert cur["name"] == "小红"
    assert cur["score"] == pytest.approx(0.8, abs=1e-3)


def test_identify_422_writes_unknown_no_raise(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    _FakeVpr.errors = [VprHttpError("audio too short", code="AUDIO_TOO_SHORT")]
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 80, request_id="r1"))
    cur = snap.get_voice_snapshot(DV)
    assert cur is not None and cur["state"] == snap.STATE_UNKNOWN


def test_identify_unreachable_writes_degraded_no_raise(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    _FakeVpr.errors = [VprHttpError("vpr-engine 不可达: connection refused")]
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1"))
    cur = snap.get_voice_snapshot(DV)
    assert cur is not None and cur["state"] == snap.STATE_DEGRADED


def test_identify_unexpected_error_degraded_no_raise(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    _FakeVpr.errors = [RuntimeError("boom")]
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1"))
    cur = snap.get_voice_snapshot(DV)
    assert cur is not None and cur["state"] == snap.STATE_DEGRADED


def test_identify_singleflight_serializes_and_last_wins(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    upsert_voice_profile(DV, name="小明", descriptor=A, merge_threshold=1.01)

    async def _run():
        _FakeVpr.gate = asyncio.Event()
        _FakeVpr.started = asyncio.Event()
        _FakeVpr.embeddings = [A, E]  # 第 1 句判小明；第 2 句（重叠）未匹配
        t1 = asyncio.create_task(
            VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1")
        )
        await asyncio.wait_for(_FakeVpr.started.wait(), 2.0)  # 第 1 句已持锁在 embedding
        t2 = asyncio.create_task(
            VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r2")
        )
        await asyncio.sleep(0.05)
        # 单飞行：第 2 句必须等第 1 句完成才开始（calls 仍为 1）
        assert _FakeVpr.calls == 1
        _FakeVpr.gate.set()
        await asyncio.gather(t1, t2)
        assert _FakeVpr.calls == 2

    asyncio.run(_run())
    cur = snap.get_voice_snapshot(DV)
    assert cur["seq"] == 2  # 快照对应当前最新 utterance（第 2 句结果）
    assert cur["state"] == snap.STATE_UNKNOWN


def test_identify_stores_sample_even_when_unknown(monkeypatch, db_env):
    """识别失败匹配也存注册样本（新声音首次注册依赖）。"""
    _enable_vpr(monkeypatch)
    _FakeVpr.embeddings = [E]
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1"))
    assert snap.take_voice_sample(DV, max_age_s=60) == E


# ────────────────── 注册链路 ─────────────────────────


def test_register_voice_for_device_uses_fresh_sample(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    snap.store_voice_sample(DV, "r1", A)
    out = register_voice_for_device(DV, "小明")
    assert out["ok"] is True
    profile = out["profile"]
    assert profile["name"] == "小明"
    assert profile["descriptor_kind"] == "voice"
    rows = load_voice_profiles(device_id=DV)
    assert [r["name"] for r in rows] == ["小明"]


def test_register_voice_for_device_stale_sample_rejected(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    snap.store_voice_sample(DV, "r1", A)
    snap._samples[DV]["ts"] = time.time() - 200.0  # noqa: SLF001
    with pytest.raises(ValueError, match="还没有可用的声音样本"):
        register_voice_for_device(DV, "小明")


def test_register_voice_for_device_disabled_rejected(monkeypatch, db_env):
    snap.store_voice_sample(DV, "r1", A)  # mode=none 时样本本会被清，这里直塞验证错误分支
    with pytest.raises(ValueError, match="声纹识别未开启"):
        register_voice_for_device(DV, "小明")


def test_register_voice_embedding_merges_same_name(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    p1 = VoiceprintService().register_voice_embedding("小明", A, device_id=DV)
    p2 = VoiceprintService().register_voice_embedding("小明", _combo(0.9, math.sqrt(1 - 0.81)), device_id=DV)
    assert p1["id"] == p2["id"]  # cos=0.9 ≥ 0.85 → EMA 合并，不新建
    assert p2["descriptor_kind"] == "voice"
    assert len(load_voice_profiles(device_id=DV)) == 1


def test_register_voice_embedding_low_sim_inserts_new(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    VoiceprintService().register_voice_embedding("小明", A, device_id=DV)
    VoiceprintService().register_voice_embedding("小红", B, device_id=DV)  # 与小明正交 → 新档案
    rows = load_voice_profiles(device_id=DV)
    assert sorted(r["name"] for r in rows) == ["小明", "小红"]


def test_register_voice_embedding_validation(monkeypatch, db_env):
    _enable_vpr(monkeypatch)
    with pytest.raises(ValueError, match="name required"):
        VoiceprintService().register_voice_embedding("  ", A, device_id=DV)
    with pytest.raises(ValueError, match="device_id required"):
        VoiceprintService().register_voice_embedding("小明", A, device_id="")
    with pytest.raises(ValueError, match="embedding required"):
        VoiceprintService().register_voice_embedding("小明", [0.1] * 4, device_id=DV)


# ────────────────── 非归一化向量回归（点积钳制 bug） ─────────────────────────


def test_voice_cosine_unnormalized_vectors_not_clamped():
    """wespeaker 输出未归一化（norm 1~6）：相似度必须除模长，不能点积钳到 1.0。

    回归：旧实现点积不除模长，4·A 与 2·(0.6A+0.8B) 点积=4.8 → 钳制 1.000。
    """
    from deskbot_server.service.voice_profile_service import (
        best_voice_similarity,
        voice_cosine_similarity,
    )

    p = {"id": 1, "name": "小明", "descriptor": [4 * x for x in A], "descriptor_kind": "voice"}
    e = [2 * (0.6 * A[i] + 0.8 * B[i]) for i in range(256)]  # norm=2, 与 4·A 真实余弦 0.6
    assert voice_cosine_similarity([4 * x for x in A], e) == pytest.approx(0.6, abs=1e-6)
    best, sim = best_voice_similarity([p], e)
    assert best["name"] == "小明"
    assert sim == pytest.approx(0.6, abs=1e-6)
    assert sim < 0.999  # 绝不能被钳到 1.0


def test_upsert_normalizes_descriptor_storage(db_env):
    """建档统一存单位向量（wespeaker 原始输出 norm≈4.85 之类不入库）。"""
    from deskbot_server.service.voice_profile_service import _l2_norm, load_voice_profiles, upsert_voice_profile

    p = upsert_voice_profile(DV, name="小明", descriptor=[4 * x for x in A], merge_threshold=1.01)
    stored = load_voice_profiles(device_id=DV)[0]["descriptor"]
    assert _l2_norm(stored) == pytest.approx(1.0, abs=1e-6)
    assert p["descriptor_kind"] == "voice"


def test_identify_legacy_unnormalized_profile_uses_true_cosine(monkeypatch, db_env):
    """旧版存的非归一化档案（norm 4.85）也应正确判同人/他人。"""
    import json

    from deskbot_server.dao import device_profile_voice_mapper as vm

    # 模拟旧实现入库的非归一化档案:4·A(norm=4)
    vm.insert(DV, "小明", json.dumps([4 * x for x in A], ensure_ascii=False), "voice")
    # 阈 0.7:真实余弦 0.6 → 应 unknown(旧点积逻辑 4.8 会被钳成 1.0 → found)
    _enable_vpr(monkeypatch)
    VoiceprintService().configure(
        build_voiceprint_runtime(
            {
                "voiceprint": {
                    "mode": "vpr",
                    "external_url": "http://127.0.0.1:9240",
                    "identity_similarity_threshold": 0.7,
                    "sample_max_age_s": 60,
                }
            }
        )
    )
    e = [2 * (0.6 * A[i] + 0.8 * B[i]) for i in range(256)]
    _FakeVpr.embeddings = [e]
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r1"))
    cur = snap.get_voice_snapshot(DV)
    assert cur["state"] == snap.STATE_UNKNOWN  # 真实余弦 0.6 < 0.7

    # 阈 0.5:found 且 score=0.6(不是钳制后的 1.0)
    VoiceprintService().configure(
        build_voiceprint_runtime(
            {
                "voiceprint": {
                    "mode": "vpr",
                    "external_url": "http://127.0.0.1:9240",
                    "identity_similarity_threshold": 0.5,
                    "sample_max_age_s": 60,
                }
            }
        )
    )
    _FakeVpr.embeddings = [e]
    asyncio.run(VoiceprintService().identify(device_id=DV, pcm_bytes=b"\x00\x01" * 800, request_id="r2"))
    cur = snap.get_voice_snapshot(DV)
    assert cur["state"] == snap.STATE_FOUND
    assert cur["name"] == "小明"
    assert cur["score"] == pytest.approx(0.6, abs=1e-3)


def test_voice_profile_delete_and_rename(db_env):
    del db_env  # 复用内存 DB 初始化
    p1 = upsert_voice_profile(DV, name="小明", descriptor=A, merge_threshold=1.01)
    p2 = upsert_voice_profile(DV, name="小红", descriptor=B, merge_threshold=1.01)
    assert len(load_voice_profiles(device_id=DV)) == 2

    assert delete_voice_profile(p1["id"], device_id=DV)
    rows = load_voice_profiles(device_id=DV)
    assert [r["id"] for r in rows] == [p2["id"]]
    summary = list_voice_profiles_summary(device_id=DV)
    assert [r["id"] for r in summary] == [p2["id"]]
    assert "descriptor" not in summary[0]  # summary 不带向量

    renamed = update_voice_profile_name(p2["id"], "新名字", device_id=DV)
    assert renamed["name"] == "新名字"
    assert update_voice_profile_name(9999, "x", device_id=DV) is None
    assert delete_voice_profile(9999, device_id=DV) is False


def test_llm_tool_runner_register_voiceprint(monkeypatch, db_env):
    from deskbot_server.service.application.llm_tool_runner import execute_llm_tools

    _enable_vpr(monkeypatch)
    snap.store_voice_sample(DV, "r1", A)
    results = asyncio.run(
        execute_llm_tools([{"tool": "register_voiceprint", "name": "小明"}], device_id=DV)
    )
    assert results[0]["ok"] is True
    assert results[0]["name"] == "小明"
    assert results[0]["profile_id"] > 0

    snap.clear_device(DV)  # 清掉样本槽 → 无可用样本
    results = asyncio.run(execute_llm_tools([{"tool": "register_voiceprint", "name": "小红"}], device_id=DV))
    assert results[0]["ok"] is False
    assert "声音样本" in results[0]["error"]


def test_tool_interim_tts_register_voiceprint():
    assert phrase_for_tool("register_voiceprint") == "我记住你的声音了"
    assert build_tool_interim_tts([{"tool": "register_voiceprint"}]) == "稍等，我记住你的声音了。"


# ────────────────── LLM user 消息注入 ─────────────────────────


def _seed_snapshot(state: str, *, name: str | None = None, score: float | None = None) -> None:
    seq = snap.begin_identification(DV, "req-x")
    snap.finish_identification(DV, seq, state=state, name=name, score=score)


def test_build_llm_user_message_includes_voice_section_when_found():
    _seed_snapshot(snap.STATE_FOUND, name="小明", score=0.72)
    msg = build_llm_user_message("你好", device_id=DV)
    assert "声音识别:" in msg
    assert "name=小明" in msg
    assert "说话人识别置信度=0.72" in msg


def test_build_llm_user_message_unknown_section():
    _seed_snapshot(snap.STATE_UNKNOWN)
    msg = build_llm_user_message("你好", device_id=DV)
    assert "声音识别:" in msg
    assert "未识别出已知说话人" in msg


def test_build_llm_user_message_unchanged_when_no_judgement():
    """无快照 / identifying / degraded → 不追加声音识别段（默认消息格式零变化）。"""
    snap.clear_device(DV)
    baseline = build_llm_user_message("你好", device_id=DV)
    assert "声音识别:" not in baseline

    snap.begin_identification(DV, "r1")  # identifying：不给结论
    msg = build_llm_user_message("你好", device_id=DV)
    assert "声音识别:" not in msg
    assert msg == baseline

    _seed_snapshot(snap.STATE_DEGRADED)
    msg = build_llm_user_message("你好", device_id=DV)
    assert "声音识别:" not in msg
    assert msg == baseline
    snap.clear_device(DV)


def test_format_sight_voice_text(monkeypatch):
    from deskbot_server.infrastructure.llm.utils import format_sight_voice_text

    assert format_sight_voice_text(None) is None
    assert format_sight_voice_text("") is None
    assert format_sight_voice_text("no-record-dev") is None

    _seed_snapshot(snap.STATE_FOUND, name="小红", score=0.55)
    assert format_sight_voice_text(DV) == "声音识别:\n  name=小红, 说话人识别置信度=0.55"

    _seed_snapshot(snap.STATE_UNKNOWN)
    assert format_sight_voice_text(DV) == "声音识别:\n  (未识别出已知说话人)"

    _seed_snapshot(snap.STATE_DEGRADED)
    assert format_sight_voice_text(DV) is None
    snap.clear_device(DV)
