"""社交主动问候：LiveService 节流调度 + SocialProactiveRunner + run_chat_turn 行为。

调度分支沿用 test_quest_proactive.py 的 FakeHub / FakeRunner 隔离；
chat_flow 行为（社交轮静默退出 / last_talk 打点）用 FakeChat 直驱 run_chat_turn。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


class FakeHub:
    def __init__(self) -> None:
        self._ts: dict[str, float] = {}

    def note_convo(self, device_id: str) -> None:
        if device_id:
            self._ts[str(device_id)] = time.time()

    def last_convo_ts(self, device_id: str) -> float:
        return self._ts.get(str(device_id or "").strip(), 0.0)

    def set_convo_ts(self, device_id: str, ts: float) -> None:
        self._ts[str(device_id)] = ts


class FakeRunner:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[str] = []

    async def attempt(self, device_id: str) -> bool:
        self.calls.append(device_id)
        return self.result


def _patch_faces(monkeypatch, names: list[str]):
    import deskbot_server.service.application.face_snapshot_cache as fsc

    monkeypatch.setattr(
        fsc,
        "list_recognized_faces",
        lambda dev, limit=5: [{"person_name": n, "identity_score": 0.95 - i * 0.05} for i, n in enumerate(names)],
    )


def _reset_live() -> None:
    from deskbot_server.service.live_service import LiveService

    LiveService.reset_instance()


def _new_live(monkeypatch, *, quest: bool = True, social: bool = True, runner_result: bool = True):
    """隔离的 LiveService：可选绑定 quest/social FakeRunner，live_mode 恒开。"""
    from deskbot_server.service.live_service import LiveService

    monkeypatch.setattr("deskbot_server.service.live_service.get_live_mode", lambda dev: True)
    _reset_live()
    svc = LiveService()
    hub = FakeHub()
    quest_runner = FakeRunner(result=runner_result) if quest else None
    social_runner = FakeRunner(result=runner_result) if social else None
    svc.bind(hub)
    if quest_runner is not None:
        svc.bind_quest_runner(quest_runner)
    if social_runner is not None:
        svc.bind_social_runner(social_runner)
    return svc, hub, quest_runner, social_runner


# ---------------------------------------------------------------------------
# LiveService 调度（社交与 quest 并列）
# ---------------------------------------------------------------------------


def test_social_spawned_after_cold_and_quest_deferred(monkeypatch):
    svc, hub, quest, social = _new_live(monkeypatch)
    _patch_faces(monkeypatch, ["小明"])

    async def _go():
        hub.set_convo_ts("dev1", time.time())  # 刚对话过 → 不触发
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert social.calls == [] and quest.calls == []
        # 冷场成立 → 社交优先 spawn，本 tick 不再起 quest
        await svc._maybe_quest_attempt("dev2")
        await asyncio.sleep(0.05)
        assert social.calls == ["dev2"]
        assert quest.calls == []  # 同 tick social 已占坑

    asyncio.run(_go())


def test_social_no_known_face_cooldown(monkeypatch):
    svc, hub, quest, social = _new_live(monkeypatch, quest=False)
    _patch_faces(monkeypatch, [])  # 无已识别已知用户

    async def _go():
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert social.calls == []
        assert svc._social_next_ok.get("dev1", 0.0) > time.monotonic()  # 60s 空转冷却

    asyncio.run(_go())


def test_social_gap_limits_and_face_change_retry(monkeypatch):
    from deskbot_server.service.live_service import SOCIAL_RETRY_GAP_SEC

    svc, hub, quest, social = _new_live(monkeypatch, quest=False)
    _patch_faces(monkeypatch, ["小明"])

    async def _go():
        svc._quest_last_check.pop("dev1", None)  # 绕过 5s 检查节流
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert social.calls == ["dev1"]
        # 间隔不足（同面孔 300s 内）不重复
        svc._quest_last_check.pop("dev1", None)
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert social.calls == ["dev1"]
        # 面孔集合变化 + 超过 RETRY_GAP → 允许提前重试（返回的老朋友 / 新面孔）
        svc._social_last_attempt_m["dev1"] = time.monotonic() - SOCIAL_RETRY_GAP_SEC - 10
        svc._social_last_names["dev1"] = frozenset({"小红"})
        _patch_faces(monkeypatch, ["小明"])
        svc._quest_last_check.pop("dev1", None)
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert social.calls == ["dev1", "dev1"]

    asyncio.run(_go())


def test_social_no_runner_keeps_quest_behavior(monkeypatch):
    """不绑 social runner → 只走 quest（旧行为不变）。"""
    svc, hub, quest, social = _new_live(monkeypatch, social=False)
    _patch_faces(monkeypatch, ["小明"])

    async def _go():
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert quest.calls == ["dev1"]
        assert social is None

    asyncio.run(_go())


def test_on_face_tick_wires_social(monkeypatch):
    svc, hub, quest, social = _new_live(monkeypatch, quest=False)
    _patch_faces(monkeypatch, ["小明"])

    async def _go():
        await svc.on_face_tick("dev1", {"landmarks": [{"name": "nose", "x": 0.5, "y": 0.5}]})
        await asyncio.sleep(0.05)

    asyncio.run(_go())
    assert social.calls == ["dev1"]


# ---------------------------------------------------------------------------
# SocialProactiveRunner
# ---------------------------------------------------------------------------


def test_social_runner_offline_returns_false(monkeypatch):
    from deskbot_server.service.application.social_proactive import SocialProactiveRunner

    _patch_faces(monkeypatch, ["小明"])

    class OfflineWs:
        def _get_ws(self, device_id):
            return None

    runner = SocialProactiveRunner(chat=object(), device_ws=OfflineWs())
    assert asyncio.run(runner.attempt("dev1")) is False


def test_social_runner_no_known_face_returns_false(monkeypatch):
    from deskbot_server.service.application.social_proactive import SocialProactiveRunner

    _patch_faces(monkeypatch, [])

    class OnlineWs:
        def _get_ws(self, device_id):
            return object()

    runner = SocialProactiveRunner(chat=object(), device_ws=OnlineWs())
    assert asyncio.run(runner.attempt("dev1")) is False


def test_social_runner_full_path(monkeypatch):
    import deskbot_server.service.application.social_proactive as sp
    from deskbot_server.service.application.social_proactive import SocialProactiveRunner

    _patch_faces(monkeypatch, ["小明", "小红"])
    captured: dict = {}

    class FakeWs:
        def _get_ws(self, device_id):
            return object()

    class FakeChat:
        settings = SimpleNamespace()

    async def _fake_run_chat_turn(downlink, chat, user_text, **kw):
        captured["user_text"] = user_text
        captured["kw"] = kw
        return SimpleNamespace(
            status="ok", error=None, llm_text="小明早上好！", llm_raw="",
            t_llm_end=1.0, t_tts_synth_end=2.0, voice_auto_reply_off=False,
        )

    async def _fake_publish(events, device_id, **kw):
        captured["publish"] = (device_id, kw.get("source"))

    monkeypatch.setattr(sp, "run_chat_turn", _fake_run_chat_turn)
    monkeypatch.setattr(sp, "publish_chat_turn", _fake_publish)
    runner = SocialProactiveRunner(chat=FakeChat(), device_ws=FakeWs(), bus_service=None)
    assert asyncio.run(runner.attempt("dev1")) is True
    assert captured["user_text"].startswith("[系统主动问候]")
    assert "小明" in captured["user_text"] and "小红" in captured["user_text"]
    assert captured["kw"]["force_voice"] is True
    assert captured["kw"]["device_id"] == "dev1"
    assert captured["publish"] == ("dev1", "social_proactive")


# ---------------------------------------------------------------------------
# run_chat_turn：社交轮静默兜底 + last_talk 打点
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_env(monkeypatch, tmp_path):
    """临时 data 目录 + 临时数据库（run_chat_turn 会话链）。"""
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


class _FakeChat:
    """直驱 run_chat_turn 的最小 Chat：返回固定 JSON（不做 TTS/pb）。"""

    def __init__(self, llm_json: str) -> None:
        self.llm_json = llm_json
        self.tts_calls = 0
        self.settings = SimpleNamespace()

    async def llm(self, text, **kw):
        return self.llm_json


def _run_turn(monkeypatch, chat, user_text, *, device_id="dev_chat", known_users=("小明",)):
    """以文本轮驱动 run_chat_turn（免语音装配），返回 (result, downlink)。

    need_reply=true 的口播链以 no-op 顶替（行为判定在 LLM 结果层，不跑真 TTS/pb）。
    """
    import deskbot_server.service.application.chat_flow as cf

    monkeypatch.setattr(cf, "native_tools_enabled", lambda dev: False)
    monkeypatch.setattr(cf, "recognized_known_users", lambda dev: list(known_users))

    class _DL:
        def __init__(self) -> None:
            self.stages: list[str] = []

        async def emit_stage(self, stage, **kw):
            self.stages.append(stage)

    dl = _DL()

    async def _fake_playback(chat_, **kw):
        return None

    monkeypatch.setattr(cf, "_run_pb_playback", _fake_playback)

    async def _go():
        return await cf.run_chat_turn(dl, chat, user_text, device_id=device_id, force_voice=True)

    return asyncio.run(_go()), dl


def test_social_round_meta_report_silenced(temp_env, monkeypatch):
    """社交轮 LLM 输出 meta 汇报语 → 兜底静默（不口播）。"""
    chat = _FakeChat(json.dumps({"need_reply": True, "tts": "已向小明问好", "tools": []}, ensure_ascii=False))
    result, dl = _run_turn(monkeypatch, chat, "[系统主动问候] 检测到认识的人（小明）在面前")
    assert result.need_reply is False  # meta 文案不照字朗读
    assert "tts_start" not in dl.stages


def test_social_round_real_greeting_kept(temp_env, monkeypatch):
    """社交轮正常问候语 → need_reply 保持 true（真实口播路径需真 TTS，此处只验判定）。"""
    chat = _FakeChat(json.dumps({"need_reply": True, "tts": "小明，早上好呀！", "tools": []}, ensure_ascii=False))
    result, _dl = _run_turn(monkeypatch, chat, "[系统主动问候] 检测到认识的人（小明）在面前")
    assert result.need_reply is True
    assert result.llm_text == "小明，早上好呀！"


def test_user_round_stamps_last_talk(temp_env, monkeypatch):
    """用户发起轮识别出已知用户 → 成功后自动打点 last_talk。"""
    chat = _FakeChat(json.dumps({"need_reply": False, "tts": "", "tools": []}, ensure_ascii=False))
    result, _dl = _run_turn(monkeypatch, chat, "早上好呀", known_users=("小明",))
    assert result.status == "ok"
    p = temp_env / "dev_chat" / "user_last_talk_小明.txt"
    assert p.is_file()
    first = p.read_text(encoding="utf-8").splitlines()[0]
    assert first[:4].isdigit() and len(first) == 19  # "YYYY-MM-DD HH:MM:SS"


def test_system_rounds_do_not_stamp(temp_env, monkeypatch):
    """三种系统前缀轮（定时/剧情/主动问候）都不打点 last_talk。"""
    from deskbot_server.service.application import chat_flow as cf

    for prefix in ("[系统定时任务]", "[系统剧情推进]", "[系统主动问候]"):
        chat = _FakeChat(json.dumps({"need_reply": False, "tts": "", "tools": []}, ensure_ascii=False))
        _run_turn(monkeypatch, chat, f"{prefix} 试试", known_users=("小明",))
        assert not (temp_env / "dev_chat" / "user_last_talk_小明.txt").exists(), prefix
