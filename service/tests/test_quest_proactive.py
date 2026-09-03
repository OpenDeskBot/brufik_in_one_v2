"""LiveService 剧本主动推进（冷场触发调度）与 QuestProactiveRunner 单测。

调度分支用 FakeHub / FakeRunner 隔离，DB 兜底通过 monkeypatch dao 层；均不落库。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeHub:
    """LiveService 依赖的 hub 最小接口（对应 DeviceWsService.note/last_convo_ts）。"""

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


def _reset_live() -> None:
    from deskbot_server.service.live_service import LiveService

    LiveService.reset_instance()


def _new_live(monkeypatch, *, runner_result: bool = True):
    """新建隔离的 LiveService（绑 FakeHub + FakeRunner），live_mode 恒开。"""
    from deskbot_server.service.live_service import LiveService

    monkeypatch.setattr("deskbot_server.service.live_service.get_live_mode", lambda dev: True)
    _reset_live()
    svc = LiveService()
    hub = FakeHub()
    runner = FakeRunner(result=runner_result)
    svc.bind(hub)
    svc.bind_quest_runner(runner)
    return svc, hub, runner


# ---------------------------------------------------------------------------
# LiveService 调度
# ---------------------------------------------------------------------------


def test_no_runner_never_attempts():
    from deskbot_server.service.live_service import LiveService

    _reset_live()
    svc = LiveService()
    svc.bind(FakeHub())
    # 不 bind_quest_runner：冷场 10 分钟也不触发
    async def _go():
        await svc._maybe_quest_attempt("dev1")

    asyncio.run(_go())


def test_cold_idle_triggers_after_one_minute(monkeypatch):
    svc, hub, runner = _new_live(monkeypatch)

    async def _go():
        # 最近刚对话过 → 不触发
        hub.set_convo_ts("dev1", time.time())
        await svc._maybe_quest_attempt("dev1")
        assert runner.calls == []
        # 冷场 ≥ 60s（内存打点为 0，DB 兜底无会话 → 视为冷场）→ 触发一次
        await svc._maybe_quest_attempt("dev2")
        await asyncio.sleep(0.05)
        assert runner.calls == ["dev2"]

    asyncio.run(_go())


def test_recent_convo_blocks_trigger(monkeypatch):
    svc, hub, runner = _new_live(monkeypatch)
    hub.set_convo_ts("dev1", time.time())  # 刚结束一轮对话

    async def _go():
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)

    asyncio.run(_go())
    assert runner.calls == []


def test_inflight_blocks_reentry(monkeypatch):
    svc, hub, runner = _new_live(monkeypatch)

    async def _go():
        # 冷场成立（无对话记录）；attempt 正在执行（inflight）期间不重入
        svc._quest_inflight.add("dev1")
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert runner.calls == []

        # in-flight 结束后可正常触发
        svc._quest_inflight.clear()
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert runner.calls == ["dev1"]

    asyncio.run(_go())


def test_no_task_cooldown(monkeypatch):
    svc, hub, runner = _new_live(monkeypatch, runner_result=False)

    async def _go():
        # 冷场成立 → 触发一次；runner 返回 False（无任务/离线）→ 置 60s 空转冷却
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert runner.calls == ["dev1"]
        assert svc._quest_next_ok.get("dev1", 0.0) > time.monotonic()

        # 冷却期间不重复触发（跳过 5s 检查节流，单独验证冷却生效）
        svc._quest_last_check.pop("dev1", None)
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert runner.calls == ["dev1"]

        # 冷却解除 → 再次允许触发
        svc._quest_next_ok["dev1"] = 0.0
        svc._quest_last_check.pop("dev1", None)
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert runner.calls == ["dev1", "dev1"]

    asyncio.run(_go())


def test_db_session_fallback_when_memory_empty(monkeypatch):
    """进程冷启动无内存打点时，用会话表 updated_at 兜底判定。"""
    import deskbot_server.dao.device_session_mapper as dsm

    svc, hub, runner = _new_live(monkeypatch)

    async def _go():
        # DB 兜底：最近会话在 120s 前 → 视为冷场满足 → 触发
        monkeypatch.setattr(dsm, "get_current_session", lambda dev: {"updated_at": time.time() - 120})
        await svc._maybe_quest_attempt("dev1")
        await asyncio.sleep(0.05)
        assert runner.calls == ["dev1"]

        # DB 兜底：会话刚刚更新（30s 前）→ 不触发
        monkeypatch.setattr(dsm, "get_current_session", lambda dev: {"updated_at": time.time() - 30})
        await svc._maybe_quest_attempt("dev2")
        await asyncio.sleep(0.05)
        assert "dev2" not in runner.calls

    asyncio.run(_go())


def test_on_face_tick_wires_quest_attempt(monkeypatch):
    """on_face_tick 有人脸 + live_mode 开 → 冷场成立时调度一次。"""
    svc, hub, runner = _new_live(monkeypatch)

    async def _go():
        await svc.on_face_tick("dev1", {"landmarks": [{"name": "nose", "x": 0.5, "y": 0.5}]})
        await asyncio.sleep(0.05)

    asyncio.run(_go())
    assert runner.calls == ["dev1"]
    # 无人脸帧不进入（回到活跃状态保护）
    async def _go2():
        await svc.on_face_tick("dev1", {"landmarks": []})

    asyncio.run(_go2())
    assert runner.calls == ["dev1"]


# ---------------------------------------------------------------------------
# QuestProactiveRunner
# ---------------------------------------------------------------------------


def test_runner_no_running_task_returns_false(monkeypatch):
    from deskbot_server.service.application.quest_proactive import QuestProactiveRunner
    from deskbot_server.service.quest_service import QuestService

    monkeypatch.setattr(QuestService, "get_current_tasks", lambda self, dev: [])
    runner = QuestProactiveRunner(chat=object(), device_ws=object())
    assert asyncio.run(runner.attempt("dev1")) is False


def test_runner_offline_returns_false(monkeypatch):
    from deskbot_server.service.application.quest_proactive import QuestProactiveRunner
    from deskbot_server.service.quest_service import QuestService

    task = {"task_id": "t1", "title": "喝水", "goal": "完成喝水", "strategy": "引导主人喝水"}
    monkeypatch.setattr(QuestService, "get_current_tasks", lambda self, dev: [task])

    class OfflineWs:
        def _get_ws(self, device_id):
            return None

    runner = QuestProactiveRunner(chat=object(), device_ws=OfflineWs())
    assert asyncio.run(runner.attempt("dev1")) is False


def test_runner_full_path(monkeypatch):
    import deskbot_server.service.application.quest_proactive as qp
    from deskbot_server.service.application.quest_proactive import QuestProactiveRunner
    from deskbot_server.service.quest_service import QuestService

    task = {
        "task_id": "t1",
        "title": "喝水打卡",
        "goal": "引导主人喝水",
        "strategy": "多提醒",
        "success_condition": "主人喝了水",
        "failure_condition": "主人拒绝",
    }
    monkeypatch.setattr(QuestService, "get_current_tasks", lambda self, dev: [task])

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
            status="ok", error=None, llm_text="主人，要不要喝口水？", llm_raw="",
            t_llm_end=1.0, t_tts_synth_end=2.0, voice_auto_reply_off=False,
        )

    async def _fake_publish(events, device_id, **kw):
        captured["publish"] = (device_id, kw.get("source"), kw.get("asr_text"))

    monkeypatch.setattr(qp, "run_chat_turn", _fake_run_chat_turn)
    monkeypatch.setattr(qp, "publish_chat_turn", _fake_publish)
    monkeypatch.setattr(qp, "_voice_was_played", lambda turn: True)

    runner = QuestProactiveRunner(chat=FakeChat(), device_ws=FakeWs())
    assert asyncio.run(runner.attempt("dev1")) is True
    assert captured["user_text"].startswith("[系统剧情推进]")
    assert "t1" in captured["user_text"]
    assert captured["kw"]["force_voice"] is True
    assert captured["publish"][1] == "quest_proactive"


# ---------------------------------------------------------------------------
# 对话轮打点（touch_device → note_convo）
# ---------------------------------------------------------------------------


def test_touch_device_notes_convo():
    from deskbot_server.infrastructure.ws.downlink_adapter import WsPipelineEventsAdapter

    class FakeDeviceWs:
        def __init__(self) -> None:
            self.touches: list[tuple[str, str | None]] = []
            self.convos: list[str] = []

        async def touch(self, device_id, status=None):
            self.touches.append((device_id, status))

        def note_convo(self, device_id):
            self.convos.append(device_id)

    fake = FakeDeviceWs()
    adapter = WsPipelineEventsAdapter(bus=None, device_ws=fake)

    async def _go():
        await adapter.touch_device("dev1", "ok")

    asyncio.run(_go())
    assert fake.touches == [("dev1", "ok")]
    assert fake.convos == ["dev1"]
