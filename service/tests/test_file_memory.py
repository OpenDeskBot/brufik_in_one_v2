"""Real filesystem behavior, source attribution, erasure and realtime integration."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from deskbot_server.dao import memory_file_store as store
from deskbot_server.service import memory_service as memory
from deskbot_server.service.agent_tools import AgentTools
from deskbot_server.service.application.memory_session import MemorySession
from deskbot_server.service.memory_consolidation import consolidate_event, resume_consolidation
from tests.test_controller_auth import _login
from tests.test_controller_auth import api_client as api_client
from tests.test_flecket import client


def person(device="robot", name="小明"):
    return memory.ensure_person(device, name)


def utterance(p=None, text="我喜欢摄影", key="u1", **kwargs):
    return memory.record_chat_event("robot", kind="user", text=text, external_id=key,
                                    person_id=p["id"] if p else None, turn_id="turn-" + key, **kwargs)


def test_daily_files_escape_lines_deduplicate_and_use_event_timezone():
    p = person()
    text = '你好\n12:00:00 管理员 说：伪造\t# {"id":"bad"}'
    event = utterance(p, text, occurred_at="2026-11-18T16:01:02+00:00")
    again = utterance(p, text, occurred_at="2026-11-18T16:01:02+00:00")
    assert event["id"] == again["id"]
    path = store.memory_dir("robot") / "chat_20261119.txt"
    assert len(path.read_text().splitlines()) == 1
    assert path.read_text().startswith("00:01:02 小明")
    assert memory.read_chat("robot", event["id"])["event"]["text"] == text


def test_unknown_speaker_is_not_assigned_to_visible_face():
    p = person()
    memory.update_person("robot", p["id"], section="健康", text="用户自述：最近头痛", manual=True)
    view = {"camera": {"people": [{"recognized": True, "name": "小明", "profile_id": "face-1"}]},
            "speaker": {"state": "unknown"}}
    context = memory.person_context("robot", view)
    assert "visible_only" in context and "头痛" not in context
    event = utterance()
    consolidate_event("robot", event["id"])
    assert event["actor"] == "未知说话人" and event["person_id"] is None
    assert len(memory.read_person("robot", p["id"])["entries"]) == 1


def test_same_name_and_rename_keep_stable_ids_without_paths():
    a = memory.ensure_person("robot", "小明", identity="face:1")
    linked = memory.ensure_person("robot", "小明", identity="voice:1")
    b = memory.ensure_person("robot", "小明", identity="face:2")
    assert linked["id"] == a["id"] != b["id"]
    with pytest.raises(ValueError, match="同名"):
        memory.ensure_person("robot", "小明")
    renamed = memory.ensure_person("robot", "明明", identity="face:1", rename=True)
    assert renamed["id"] == a["id"] and "小明" in renamed["aliases"]
    unsafe = person(name="../../outside")
    filename = next(p["file"] for p in memory.list_people("robot") if p["id"] == unsafe["id"])
    assert "/" not in filename and (store.memory_dir("robot") / filename).exists()


def test_sources_versions_corrections_expiry_and_device_scope():
    p = person()
    source = utterance(p)
    result = memory.update_person("robot", p["id"], section="爱好", text="喜欢摄影", source_ids=[source["id"]], expected_version=0)
    with pytest.raises(ValueError, match="已变化"):
        memory.update_person("robot", p["id"], section="爱好", text="喜欢音乐", source_ids=[source["id"]], expected_version=0)
    with pytest.raises(ValueError):
        memory.update_person("other", p["id"], section="爱好", text="喜欢摄影", source_ids=[source["id"]])
    other = person(name="小红")
    with pytest.raises(ValueError, match="来源必须"):
        memory.update_person("robot", other["id"], section="爱好", text="喜欢摄影", source_ids=[source["id"]])
    corrected = memory.update_person("robot", p["id"], section="爱好", text="现在更喜欢画画", source_ids=[source["id"]],
                                      entry_id=result["entry"]["id"], expected_version=1)
    assert corrected["version"] == 2
    assert [e["text"] for e in memory.read_person("robot", p["id"])["entries"]] == ["现在更喜欢画画"]
    memory.update_person("robot", p["id"], section="情绪", text="曾经低落", source_ids=[source["id"]], expires_at="2020-01-01T00:00:00+08:00")
    context = memory.person_context("robot", {"speaker": {"state": "found", "name": "小明"}})
    assert "曾经低落" not in context and "现在更喜欢画画" in context


def test_robot_output_cannot_be_a_person_fact_source():
    p = person()
    event = memory.record_chat_event("robot", kind="assistant", text="你喜欢猫", external_id="a", person_id=p["id"])
    with pytest.raises(ValueError, match="来源必须"):
        memory.update_person("robot", p["id"], section="爱好", text="喜欢猫", source_ids=[event["id"]])


def test_concurrent_retries_are_one_file_entry_without_sqlite():
    p = person()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: memory.add_memory("喜欢猫", device_id="robot", person_id=p["id"], request_key="same"), range(4)))
    assert len({r["id"] for r in results}) == 1
    assert len(memory.list_memory_for_device("robot")) == 1
    from deskbot_server.dao.device_memory_mapper import list_legacy_rows
    assert list_legacy_rows() == []
    with pytest.raises(ValueError, match="其他内容"):
        memory.add_memory("喜欢狗", device_id="robot", person_id=p["id"], request_key="same")


def test_management_and_search_have_complete_pagination():
    p = person()
    for i in range(35):
        event = utterance(p, f"我喜欢项目{i}", key=str(i))
        memory.update_person("robot", p["id"], section="爱好", text=f"项目{i}", source_ids=[event["id"]])
    assert len(memory.list_memory_for_device("robot")) == 35
    first = memory.search_chat("robot", "项目", limit=20)
    second = memory.search_chat("robot", "项目", limit=20, offset=20)
    assert first["total"] == 35 and first["has_more"]
    assert len(second["events"]) == 15 and not second["has_more"]
    assert len(memory.list_memory_for_device("robot", limit=20, offset=20)) == 15


def test_budget_skips_large_entry_without_blocking_short_facts():
    p = person()
    memory.add_memory("长" * 2000, device_id="robot", person_id=p["id"])
    memory.add_memory("称呼小明", device_id="robot", person_id=p["id"])
    context = memory.person_context("robot", {"speaker": {"state": "found", "name": "小明"}}, budget=1200)
    assert "称呼小明" in context and "长" * 100 not in context


def test_erase_removes_sources_replies_old_revisions_and_blocks_rebuild():
    p = person()
    source = utterance(p, "我喜欢摄影")
    reply = memory.record_chat_event("robot", kind="assistant", text="记住你喜欢摄影了", external_id="a", turn_id="turn-u1")
    first = memory.update_person("robot", p["id"], section="爱好", text="喜欢摄影", source_ids=[source["id"]])
    second = memory.update_person("robot", p["id"], section="爱好", text="现在喜欢画画", manual=True, entry_id=first["entry"]["id"])
    old_epoch = memory.memory_epoch("robot")
    memory.forget_memory("robot", entry_ids=[second["entry"]["id"]])
    assert memory.search_chat("robot", "摄影")["total"] == 0
    assert memory.read_person("robot", p["id"], include_history=True)["entries"] == []
    with pytest.raises(ValueError, match="遗忘"):
        memory.read_chat("robot", reply["id"])
    with pytest.raises(ValueError, match="失效"):
        memory.record_chat_event("robot", kind="user", text="迟到的旧内容", external_id="late", expected_epoch=old_epoch)
    with pytest.raises(ValueError, match="遗忘"):
        memory.update_person("robot", p["id"], section="爱好", text="喜欢摄影", source_ids=[source["id"]])
    resume_consolidation("robot", expected_epoch=memory.memory_epoch("robot"))
    assert memory.read_person("robot", p["id"])["entries"] == []
    for path in store.memory_dir("robot").glob("*"):
        if path.is_file():
            assert "摄影" not in path.read_text()


def test_forget_cascades_across_profile_and_history_recalls_without_erasing_independent_input():
    p = person()
    original = utterance(p, "我喜欢柠檬茶")
    entry = memory.update_person("robot", p["id"], section="爱好", text="喜欢柠檬茶", source_ids=[original["id"]])
    memory.link_turn_sources("robot", "recall", [original["id"]])
    independent = memory.record_chat_event("robot", kind="user", text="钥匙在绿色盒子", external_id="keys", turn_id="recall")
    first = memory.record_chat_event("robot", kind="assistant", text="你喜欢柠檬茶", external_id="recall-a", turn_id="recall")
    memory.link_turn_sources("robot", "history", [first["id"]])
    memory.record_chat_event("robot", kind="assistant", text="记录中说喜欢柠檬茶", external_id="history-a", turn_id="history")
    # A result can be read after the user input was already written. Its
    # provenance still applies to later answers and playback in that turn.
    memory.forget_memory("robot", entry_ids=[entry["entry"]["id"]])
    assert memory.search_chat("robot", "柠檬茶")["total"] == 0
    assert memory.read_chat("robot", independent["id"])["event"]["text"] == "钥匙在绿色盒子"
    assert memory.record_chat_event("robot", kind="assistant", text="迟到的柠檬茶", external_id="late-a", turn_id="history")["status"] == "erased"
    with pytest.raises(ValueError, match="遗忘"):
        memory.link_turn_sources("robot", "old-read", [original["id"]])


def test_profile_and_history_tool_reads_track_sources_through_later_unknown_turns():
    p = person()
    source = utterance(p)
    entry = memory.update_person("robot", p["id"], section="爱好", text="喜欢摄影", source_ids=[source["id"]])
    async def run():
        session = MemorySession("robot")
        await session.start()
        bridge = SimpleNamespace(memory=session, client=SimpleNamespace(_turn_id="history"), _current=lambda generation: True)
        tools = AgentTools("robot", bridge)
        result = await tools.execute("chat_read", {"event_id": source["id"], "context": 0},
                                     call_id="read", response_id="r", generation=0)
        assert result["ok"]
        await session.context("profile", {"speaker": {"state": "found", "name": "小明"}})
        await session.context("unknown", {"speaker": {"state": "unknown"}})
        for turn in ("history", "profile", "unknown"):
            session.offer("output.transcript.done", {"text": "你喜欢摄影", "turn_id": turn})
        await session.flush()
        memory.forget_memory("robot", entry_ids=[entry["entry"]["id"]])
        assert memory.search_chat("robot", "摄影")["total"] == 0
        await session.close()
    asyncio.run(run())


def test_superseded_turn_does_not_block_later_evidence_or_borrow_its_identity():
    p = person()

    async def run():
        session = MemorySession("robot")
        await session.start()
        try:
            session.begin_turn("old")
            session.offer("input.transcript.done", {"text": "旧的一句", "turn_id": "old"})
            await asyncio.sleep(0)
            # The old turn's context preparation was cancelled by new speech.
            session.begin_turn("new")
            session.observations["new"] = {"speaker": {"state": "found", "name": p["name"]}}
            session.offer("input.transcript.done", {"text": "新的一句", "turn_id": "new"})
            await asyncio.wait_for(session.flush(), timeout=.5)
            old = memory.read_chat("robot", session.input_events["old"])["event"]
            new = memory.read_chat("robot", session.input_events["new"])["event"]
            assert old.get("person_id") is None
            assert new["person_id"] == p["id"]
            assert old["text"] == "旧的一句" and new["text"] == "新的一句"
        finally:
            await session.close()

    asyncio.run(run())


def test_split_correction_captures_all_unanswered_fragments_for_forgetting():
    p = person()
    original = memory.add_memory("喜欢柠檬茶", device_id="robot", person_id=p["id"])
    async def run():
        session = MemorySession("robot")
        await session.start()
        for turn, text in (("first", "纠正一下，我现在喜欢桂花茶。"), ("second", "不喜欢柠檬茶了。")):
            session.observations[turn] = {"speaker": {"state": "found", "name": "小明"}}
            session.offer("input.transcript.done", {"text": text, "turn_id": turn})
        await session.flush()
        bridge = SimpleNamespace(memory=session, client=SimpleNamespace(_turn_id="second"), _current=lambda generation: True)
        tools = AgentTools("robot", bridge)
        arguments = {"person_id": p["id"], "section": "爱好", "text": "喜欢桂花茶，不喜欢柠檬茶了",
                     "entry_id": original["id"], "source_ids": [session.input_events["second"]],
                     "expected_version": memory.read_person("robot", p["id"])["version"]}
        result = await tools.execute("person_update", arguments, call_id="split", response_id="r", generation=0)
        assert result["ok"] and set(result["entry"]["source_ids"]) == set(session.input_events.values())
        session.offer("output.transcript.done", {"text": "已经纠正", "turn_id": "first"})
        await session.flush()
        assert list(session.pending_inputs) == ["second"]
        session.offer("output.transcript.done", {"text": "已经纠正", "turn_id": "second"})
        await session.flush()
        assert not session.pending_inputs
        retried = await tools.execute("person_update", arguments, call_id="split", response_id="retry", generation=0)
        assert retried["ok"] and retried["entry"]["id"] == result["entry"]["id"]
        memory.forget_memory("robot", entry_ids=[result["entry"]["id"]])
        assert memory.search_chat("robot", "桂花茶")["total"] == 0
        assert memory.search_chat("robot", "柠檬茶")["total"] == 0
        await session.close()
    asyncio.run(run())


def test_quiet_text_answer_is_recorded_with_sources_and_forgotten_without_playback():
    p = person()
    entry = memory.add_memory("喜欢摄影", device_id="robot", person_id=p["id"])
    async def run():
        session = MemorySession("robot")
        await session.start()
        await session.context("quiet-turn", {"speaker": {"state": "found", "name": "小明"}})
        value = client()
        value.ws.emit("response.created", response={"id": "quiet-response", "metadata": {
            "deskbot_turn": "quiet-turn", "input_generation": 0}})
        value.ws.emit("response.output_text.delta", response_id="quiet-response", item_id="quiet-item", delta="你喜欢")
        value.ws.emit("response.output_text.done", response_id="quiet-response", item_id="quiet-item", text="你喜欢摄影")
        delta = await value.receive()
        answer = await value.receive()
        assert delta.action == "output.transcript.delta" and delta.data["text"] == "你喜欢"
        assert answer.action == "output.transcript.done" and answer.data["source_turn_id"] == "quiet-turn"
        session.offer(answer.action, answer.data)
        await session.flush()
        recorded = memory.search_chat("robot", "你喜欢摄影")["events"]
        assert len(recorded) == 1 and recorded[0]["status"] == "generated_text_only"
        assert not memory.search_chat("robot", "播放")["events"]
        memory.forget_memory("robot", entry_ids=[entry["id"]])
        assert memory.search_chat("robot", "摄影")["total"] == 0
        await session.close()
        await value.close()
    asyncio.run(run())


def test_consolidation_is_idempotent_and_ignores_hypotheticals():
    p = person()
    event = utterance(p)
    consolidate_event("robot", event["id"])
    consolidate_event("robot", event["id"])
    imagined = utterance(p, "如果我喜欢猫，你会怎样？", key="hypothesis")
    consolidate_event("robot", imagined["id"])
    entries = memory.read_person("robot", p["id"])["entries"]
    assert len(entries) == 1 and entries[0]["source_ids"] == [event["id"]]


def test_transaction_recovers_partial_append_and_profile_write(monkeypatch):
    p = person()
    root = store.memory_dir("robot")
    original = store._apply
    failed = False

    def fail_once(root, operations):
        nonlocal failed
        if not failed:
            failed = True
            operation = next(o for o in operations if o["append"])
            (root / operation["name"]).write_bytes(b"broken partial append")
            raise OSError("simulated crash")
        original(root, operations)

    monkeypatch.setattr(store, "_apply", fail_once)
    with pytest.raises(OSError):
        utterance(p)
    assert (root / ".pending.json").exists()
    results = memory.search_chat("robot")
    assert results["total"] == 1 and results["events"][0]["text"] == "我喜欢摄影"
    assert not (root / ".pending.json").exists()


def test_symlink_and_invalid_device_paths_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        memory.ensure_person("../outside", "小明")
    root = store.memory_dir("robot")
    root.parent.mkdir(parents=True)
    root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        memory.ensure_person("robot", "小明")


def test_notification_failure_does_not_change_saved_result():
    def broken(**kwargs):
        raise OSError("offline")
    unsubscribe = memory.subscribe("robot", broken)
    try:
        result = memory.add_memory("可靠保存", device_id="robot")
        assert memory.get_memory(result["id"], device_id="robot")["text"] == "可靠保存"
    finally:
        unsubscribe()


def test_evidence_worker_is_independent_of_viewers_and_matches_exact_turn():
    async def run():
        session = MemorySession("robot")
        await session.start()
        session.observations["one"] = {"speaker": {"state": "found", "name": "小明"}}
        session.observations["two"] = {"speaker": {"state": "found", "name": "小红"}}
        session.offer("input.transcript.delta", {"text": "不保存的中间片段"})
        session.offer("input.transcript.done", {"text": "我喜欢摄影", "turn_id": "one", "item_id": "first"})
        session.offer("input.transcript.done", {"text": "我喜欢画画", "turn_id": "two", "item_id": "second"})
        await session.flush()
        values = memory.search_chat("robot")["events"]
        assert {e["actor"]: e["text"] for e in values} == {"小明": "我喜欢摄影", "小红": "我喜欢画画"}
        await session.close()
    asyncio.run(run())


def test_new_tool_catalog_and_sourced_update():
    p = person()
    source = utterance(p)
    async def run():
        bridge = SimpleNamespace(_current=lambda generation: generation == 0, hub=SimpleNamespace(_devices={}))
        tools = AgentTools("robot", bridge)
        names = {t["name"] for t in tools.schemas()}
        assert "memory" not in names
        assert {"chat_search", "chat_read", "person_read", "person_update", "memory_forget"} <= names
        result = await tools.execute("person_update", {"person_id": p["id"], "section": "爱好", "text": "喜欢摄影",
                                     "source_ids": [source["id"]], "expected_version": 0}, call_id="c", response_id="r", generation=0)
        assert result["ok"] and result["saved"]
        assert "喜欢摄影" not in tools.instructions()
    asyncio.run(run())


def test_erase_delivers_tool_result_before_closing_old_upstream_context():
    async def run():
        value = client()
        value.agent_tool_handler = AsyncMock(return_value={"ok": True, "reset_session": True})
        await value._complete_tools({"id": "r", "status": "completed", "output": [
            {"type": "function_call", "name": "memory_forget", "call_id": "c", "arguments": '{"entry_ids":["m1"]}'}]})
        assert value.ws.sent[-1]["item"]["type"] == "function_call_output" and value.ws.closed
        assert not any(e["type"] == "response.create" for e in value.ws.sent)
        await value.close()
    asyncio.run(run())


def test_file_memory_http_requires_owner_and_supports_source_and_pagination(api_client):
    assert api_client.get("/app/api/memory/chat").status_code == 401
    _login(api_client, current_device_id="device-owner")
    p = api_client.post("/app/api/memory/people", json={"name": "小明"}).json()["person"]
    created = api_client.post("/app/api/memories", json={"person_id": p["id"], "section": "爱好", "text": "喜欢摄影"})
    assert created.status_code == 200
    result = api_client.get("/app/api/memories?limit=1").json()
    assert result["total"] == 1 and result["people"][0]["id"] == p["id"]
    source_id = result["memories"][0]["source_ids"][0]
    assert api_client.get("/app/api/memory/chat/" + source_id).json()["event"]["kind"] == "profile_revision"
    assert api_client.get("/app/api/memory/chat?device_id=device-other").status_code == 403
    mid = result["memories"][0]["id"]
    assert api_client.delete("/app/api/memories/" + mid).status_code == 200
    assert api_client.get("/app/api/memory/chat?query=摄影").json()["total"] == 0


def test_cross_midnight_replayed_event_is_not_logged_twice():
    utterance(text="重复投递", occurred_at="2026-11-18T23:59:59+08:00")
    utterance(text="重复投递", occurred_at="2026-11-19T00:00:01+08:00")
    assert memory.search_chat("robot")["total"] == 1
    assert not (store.memory_dir("robot") / "chat_20261119.txt").exists()


def test_clear_invalidates_delayed_identity_and_background_writes():
    old = memory.memory_epoch("robot")
    p = person()
    utterance(p)
    memory.clear_file_memory("robot")
    with pytest.raises(ValueError, match="失效"):
        memory.ensure_person("robot", "旧会话人物", expected_epoch=old)
    assert memory.list_people("robot") == []
    with pytest.raises(ValueError, match="失效"):
        resume_consolidation("robot", expected_epoch=old)


def test_simple_correction_and_delayed_evidence_never_restore_old_fact():
    p = person()
    first = utterance(p, "我喜欢摄影")
    consolidate_event("robot", first["id"])
    second = utterance(p, "我不喜欢摄影", key="new")
    consolidate_event("robot", second["id"])
    entries = memory.read_person("robot", p["id"])["entries"]
    assert [e["text"] for e in entries] == ["用户自述：我不喜欢摄影"]
    delayed = utterance(p, "我喜欢摄影", key="late", occurred_at="2020-01-01T00:00:00+08:00")
    consolidate_event("robot", delayed["id"])
    assert memory.read_person("robot", p["id"])["entries"] == entries


def test_self_introduction_can_bind_unknown_source_with_audit():
    p = person()
    event = utterance(text="记住我的声音，我叫小明，喜欢摄影。")
    assert memory.attribute_self_introduction("robot", event["id"], p["id"])
    consolidate_event("robot", event["id"])
    source = memory.read_chat("robot", event["id"])["event"]
    assert source["person_id"] == p["id"] and source["identity_basis"] == "self_declared"
    assert "摄影" in str(memory.read_person("robot", p["id"])["entries"])
    other = person(name="小红")
    assert not memory.attribute_self_introduction("robot", event["id"], other["id"])


def test_profile_file_migration_is_one_way_and_resumes_partial_import(monkeypatch):
    from deskbot_server.dao import device_memory_mapper as legacy
    from deskbot_server.service.device_identity_service import ensure_device_identity
    from deskbot_server.service.memory_migration import migrate_legacy_memories

    ensure_device_identity("robot")
    legacy.insert("robot", "a", "", "旧资料一")
    legacy.insert("robot", "b", "", "旧资料二")
    original = memory.add_memory
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("migration interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(memory, "add_memory", interrupted)
    with pytest.raises(OSError):
        migrate_legacy_memories()
    monkeypatch.setattr(memory, "add_memory", original)
    migrate_legacy_memories()
    assert legacy.list_legacy_rows() == []
    assert {e["text"] for e in memory.list_memory_for_device("robot")} == {"旧资料一", "旧资料二"}
    memory.clear_file_memory("robot")
    migrate_legacy_memories()
    assert memory.list_memory_for_device("robot") == []


def test_input_transcript_keeps_original_turn_when_arriving_late():
    async def run():
        value = client()
        value.ws.emit("input_audio_buffer.speech_started", item_id="old-item")
        old_turn = (await value.receive()).data["turn_id"]
        value.ws.emit("input_audio_buffer.speech_stopped", item_id="old-item")
        value.ws.emit("input_audio_buffer.speech_started", item_id="new-item")
        new_turn = (await value.receive()).data["turn_id"]
        value.ws.emit("conversation.item.input_audio_transcription.completed", item_id="old-item", transcript="迟到的旧句")
        result = await value.receive()
        assert old_turn != new_turn and result.data["turn_id"] == old_turn
        assert result.data["item_id"] == "old-item"
        await value.close()
    asyncio.run(run())


def test_tool_continuation_reads_latest_profile_without_session_snapshot():
    from deskbot_server.model.system_event import SystemEvent

    p = person()
    original = memory.add_memory("喜欢摄影", device_id="robot", person_id=p["id"])
    event = SystemEvent.create("perception_snapshot", "e", "perception", {"speaker": {"state": "found", "name": "小明"}})
    async def run():
        value = client()
        value._session_instructions = "固定人设"
        value._remember_event(event, 0)
        async def context(turn_id, event, **kwargs):
            return memory.person_context("robot", event.as_dict()["data"])
        value.memory_context_provider = context
        value._memory_context = await context(None, event)
        memory.update_memory(original["id"], "喜欢画画", device_id="robot")
        value.agent_tool_handler = AsyncMock(return_value={"ok": True})
        await value._complete_tools({"id": "r", "status": "completed", "metadata": {"system_event_id": "e"}, "output": [
            {"type": "function_call", "name": "person_read", "call_id": "read", "arguments": "{}"}]})
        instructions = value.ws.sent[-1]["response"]["instructions"]
        assert "喜欢画画" in instructions and "喜欢摄影" not in instructions
        assert value._session_instructions == "固定人设"
        await value.close()
    asyncio.run(run())


def test_duplicate_fact_keeps_all_sources_for_complete_forgetting():
    p = person()
    for key in ("one", "two"):
        event = utterance(p, "我喜欢摄影", key=key)
        consolidate_event("robot", event["id"])
    entries = memory.read_person("robot", p["id"])["entries"]
    assert len(entries) == 1 and len(entries[0]["source_ids"]) == 2
    memory.forget_memory("robot", entry_ids=[entries[0]["id"]])
    assert memory.search_chat("robot", "摄影")["total"] == 0


def test_reconfirmed_transient_state_refreshes_expiry_without_duplicate():
    p = person()
    memory.update_person("robot", p["id"], section="情绪", text="用户自述：很开心", manual=True,
                         expires_at="2020-01-01T00:00:00+08:00")
    assert memory.read_person("robot", p["id"])["entries"][0]["expired"]
    memory.update_person("robot", p["id"], section="情绪", text="用户自述：很开心", manual=True)
    entries = memory.read_person("robot", p["id"])["entries"]
    assert len(entries) == 1 and not entries[0]["expired"] and len(entries[0]["source_ids"]) == 2


def test_historical_mood_does_not_become_current_during_recovery():
    p = person()
    event = utterance(p, "我很开心", occurred_at="2020-01-01T00:00:00+08:00")
    consolidate_event("robot", event["id"])
    entries = memory.read_person("robot", p["id"])["entries"]
    assert entries[0]["expired"]
    assert "很开心" not in memory.person_context("robot", {"speaker": {"state": "found", "name": "小明"}})


def test_agent_reads_long_evidence_in_bounded_continuable_chunks():
    source = utterance(text="长记录" * 2000)
    async def run():
        tools = AgentTools("robot", SimpleNamespace(_current=lambda generation: True))
        first = await tools.execute("chat_read", {"event_id": source["id"]}, call_id="r1", response_id="r", generation=0)
        second = await tools.execute("chat_read", {"event_id": source["id"], "text_offset": first["next_text_offset"]},
                                     call_id="r2", response_id="r", generation=0)
        assert first["ok"] and first["has_more"] and len(first["event"]["text"]) == 2000
        assert second["text_offset"] == 2000 and second["total_chars"] == 6000
    asyncio.run(run())


def test_expired_speaker_observation_cannot_load_private_profile_on_continuation():
    from deskbot_server.model.system_event import SystemEvent
    from deskbot_server.service.moss_live_service import FlecketBridge

    event = SystemEvent.create("perception_snapshot", "expired-speaker", "perception", {
        "speaker": {"state": "found", "name": "小明", "expires_at": "2020-01-01T00:00:00+00:00"}})
    async def run():
        recorder = SimpleNamespace(context=AsyncMock(return_value="context"))
        bridge = SimpleNamespace(memory=recorder)
        await FlecketBridge._memory_context_for_turn(bridge, "turn", event)
        assert recorder.context.call_args.args[1]["speaker"] == {"state": "stale"}
    asyncio.run(run())
