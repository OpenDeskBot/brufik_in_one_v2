"""设备归属：隔离、换绑、数据库约束及旧库迁移。"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, MetaData, String, UniqueConstraint, inspect, text
from sqlalchemy.exc import IntegrityError

from deskbot_server.dao import device_mapper
from deskbot_server.db.device_ownership import DEVICE_DATA_TABLES, migrate_device_ownership
from deskbot_server.db.engine import init_engine, reset_engine
from deskbot_server.db.models import Base, Device, DeviceUsage, QuestInstance, ScheduledTask, User
from deskbot_server.service import face_profile_service as face
from deskbot_server.service import memory_service as memory
from deskbot_server.service import scheduled_task_service as reminder
from deskbot_server.service import session_service as sessions
from deskbot_server.service import voice_profile_service as voice
from deskbot_server.service.device_data_paths import legacy_device_data_dir, safe_device_data_dir
from deskbot_server.service.device_identity_service import ensure_device_identity
from deskbot_server.service.user_service import UserService
from tests.device_bind_helpers import bind_device_online

DID = "brfk_aabbccddeeff1234"
OTHER = "brfk_aabbccddeeff5678"


def _seed(device_id):
    mem = memory.add_memory("喜欢猫", device_id=device_id)
    fp = face.upsert_profile(device_id, name="小明", descriptor=[1.0] + [0.0] * 255)
    vp = voice.upsert_voice_profile(device_id, name="小明", descriptor=[1.0] + [0.0] * 255)
    session = sessions.create_session(device_id)
    sessions.append_turn(device_id, session["session_id"], "你好", "你好呀")
    task = reminder.create_scheduled_task(device_id, "喝水", delay_minutes=10, session_id=session["session_id"])
    with init_engine().begin() as conn:
        conn.execute(DeviceUsage.__table__.insert().values(device_id=device_id, date=date(2026, 11, 18)))
        conn.execute(QuestInstance.__table__.insert().values(device_id=device_id, playbook="demo", task_id="t1"))
    ddir = safe_device_data_dir(device_id)
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "user_info_小明.txt").write_text("设备认识的小明", encoding="utf-8")
    return dict(memory=mem, face=fp, voice=vp, session=session, task=task)


def _accounts():
    svc = UserService()
    owner = svc.register("owner@example.com", "password1234")
    other = svc.register("other@example.com", "password1234")
    bind_device_online(owner.id, DID)
    bind_device_online(owner.id, OTHER)
    return svc, owner, other


def test_unbind_and_rebind_only_change_owner():
    svc, owner, other = _accounts()
    original = svc.get_device(DID)
    device_mapper.update_volume(DID, 33)
    data = _seed(DID)
    memory.add_memory("另一台设备的记忆", device_id=OTHER)
    assert not svc.unbind_device(other.id, DID)
    assert svc.unbind_device(owner.id, DID)
    unbound = svc.get_device(DID)
    assert unbound.id == original.id and unbound.owner_user_id is None
    assert not svc.user_owns_device(owner.id, DID)
    assert [d.device_id for d in svc.list_devices(owner.id)] == [OTHER]
    bind_device_online(other.id, DID)
    rebound = svc.get_device(DID)
    assert rebound.id == original.id and rebound.volume == 33
    assert svc.user_owns_device(other.id, DID)
    assert not svc.user_owns_device(owner.id, DID)
    assert memory.get_memory(data["memory"]["id"], device_id=DID)["text"] == "喜欢猫"
    assert face.list_face_profiles_summary(device_id=DID)[0]["id"] == data["face"]["id"]
    assert voice.list_voice_profiles_summary(device_id=DID)[0]["id"] == data["voice"]["id"]
    assert sessions.load_session(DID, data["session"]["session_id"])["messages"]
    assert reminder.get_scheduled_task(data["task"]["id"], device_id=DID)
    assert (safe_device_data_dir(DID) / "user_info_小明.txt").read_text() == "设备认识的小明"
    with init_engine().connect() as conn:
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()


def test_same_owner_devices_never_share_resource_ids():
    _accounts()
    data = _seed(DID)
    mid = data["memory"]["id"]
    assert memory.get_memory(mid, device_id=OTHER) is None
    assert memory.update_memory(mid, "不能覆盖", device_id=OTHER) is None
    assert not memory.delete_memory(mid, device_id=OTHER)
    assert face.update_face_profile_name(data["face"]["id"], "不能覆盖", device_id=OTHER) is None
    assert not face.delete_face_profile(data["face"]["id"], device_id=OTHER)
    assert voice.update_voice_profile_name(data["voice"]["id"], "不能覆盖", device_id=OTHER) is None
    assert not voice.delete_voice_profile(data["voice"]["id"], device_id=OTHER)
    assert sessions.load_session(OTHER, data["session"]["session_id"]) is None
    assert reminder.get_scheduled_task(data["task"]["id"], device_id=OTHER) is None
    assert reminder.update_scheduled_task(data["task"]["id"], device_id=OTHER, description="不能覆盖") is None
    assert not reminder.delete_scheduled_task(data["task"]["id"], device_id=OTHER)
    with pytest.raises(ValueError, match="属于当前设备"):
        reminder.create_scheduled_task(OTHER, "不能借用对话", delay_minutes=10, session_id=data["session"]["session_id"])


@pytest.mark.parametrize("call", [
    lambda: memory.list_memory_for_device(), lambda: memory.add_memory("无设备"),
    lambda: memory.get_memory("1"), lambda: memory.update_memory("1", "无设备"), lambda: memory.delete_memory("1"),
    lambda: face.load_face_profiles(), lambda: face.delete_face_profile(1), lambda: face.update_face_profile_name(1, "无设备"),
    lambda: voice.load_voice_profiles(), lambda: voice.delete_voice_profile(1), lambda: voice.update_voice_profile_name(1, "无设备"),
    lambda: reminder.get_scheduled_task("1"), lambda: reminder.update_scheduled_task("1"), lambda: reminder.delete_scheduled_task("1"),
])
def test_missing_device_scope_is_rejected(call):
    with pytest.raises(ValueError, match="device_id"):
        call()


@pytest.mark.parametrize("table", DEVICE_DATA_TABLES)
def test_database_rejects_unregistered_device(table):
    values = {
        "device_memory": dict(title="记忆", parent="", text="不能成为孤儿"),
        "device_profile_face": dict(name="小明", descriptor="[]"),
        "device_profile_voice": dict(name="小明", descriptor="[]"),
        "device_session": dict(title="对话"),
        "scheduled_tasks": dict(description="提醒", next_run_at=reminder.cst_now()),
        "device_usage": dict(date=date(2026, 11, 18)), "quest_instance": dict(playbook="demo", task_id="t1"),
    }[table]
    for device_id in ("", "missing"):
        with pytest.raises(IntegrityError), init_engine().begin() as conn:
            conn.execute(Base.metadata.tables[table].insert().values(device_id=device_id, **values))


@pytest.mark.parametrize("device_id", ["", " ", ".", "..", "global", "device", "a/b", "a" * 129])
def test_database_rejects_invalid_device_identity(device_id):
    with pytest.raises(IntegrityError), init_engine().begin() as conn:
        conn.execute(Device.__table__.insert().values(device_id=device_id))


def test_database_preserves_device_when_account_is_deleted():
    _, owner, _ = _accounts()
    data = _seed(DID)
    with init_engine().begin() as conn:
        conn.execute(User.__table__.delete().where(User.id == owner.id))
    assert device_mapper.get_by_device_id(DID).owner_user_id is None
    assert memory.get_memory(data["memory"]["id"], device_id=DID)
    with pytest.raises(IntegrityError), init_engine().begin() as conn:
        conn.execute(Device.__table__.delete().where(Device.device_id == DID))


def test_database_rejects_reminder_from_another_devices_session():
    data = _seed(DID)
    ensure_device_identity(OTHER)
    with pytest.raises(IntegrityError), init_engine().begin() as conn:
        conn.execute(ScheduledTask.__table__.insert().values(
            device_id=OTHER, description="跨设备提醒", next_run_at=reminder.cst_now(), session_id=data["session"]["session_id"],
        ))
    other_session = sessions.create_session(OTHER)
    with pytest.raises(IntegrityError), init_engine().begin() as conn:
        conn.execute(text("UPDATE scheduled_tasks SET session_id=:sid WHERE id=:tid"), dict(sid=other_session["session_id"], tid=data["task"]["id"]))


def test_reset_id_cascades_all_device_data_and_moves_files():
    svc, _, _ = _accounts()
    data = _seed(DID)
    legacy = legacy_device_data_dir(DID)
    legacy.mkdir(parents=True)
    (legacy / "sample.txt").write_text("legacy")
    new_id = svc.reset_device_id(DID)
    assert new_id != DID and svc.get_device(DID) is None
    with init_engine().connect() as conn:
        for table in DEVICE_DATA_TABLES:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE device_id=:did"), dict(did=DID)).scalar() == 0
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE device_id=:did"), dict(did=new_id)).scalar() == (0 if table == "device_memory" else 1)
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    assert reminder.get_scheduled_task(data["task"]["id"], device_id=new_id)["session_id"] == data["session"]["session_id"]
    assert (safe_device_data_dir(new_id) / "user_info_小明.txt").exists()
    assert (legacy_device_data_dir(new_id) / "sample.txt").read_text() == "legacy"
    assert memory.get_memory(data["memory"]["id"], device_id=new_id)["text"] == "喜欢猫"


def _legacy_engine(tmp_path):
    reset_engine()
    engine = init_engine(tmp_path / "legacy.db")
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    for table in metadata.tables.values():
        for constraint in list(table.constraints):
            if isinstance(constraint, (ForeignKeyConstraint, CheckConstraint)) or (table.name == "device_session" and isinstance(constraint, UniqueConstraint)):
                table.constraints.remove(constraint)
    metadata.tables["devices"].c.owner_user_id.nullable = False
    metadata.tables["devices"].append_column(Column("legacy_note", String, server_default="kept"))
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX legacy_note_index ON devices(legacy_note)"))
        conn.execute(metadata.tables["users"].insert().values(id="owner", email="owner@example.com", password_hash="hash"))
        conn.execute(metadata.tables["devices"].insert().values(id="pk", device_id=DID, owner_user_id="owner", volume=27))
        conn.execute(metadata.tables["device_memory"].insert().values(device_id="unbound_device", title="旧记忆", text="保留"))
    return engine, metadata


def test_migrate_legacy_database_preserves_data_and_is_idempotent(tmp_path):
    engine, metadata = _legacy_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(metadata.tables["scheduled_tasks"].insert().values(device_id=DID, description="旧提醒", next_run_at=reminder.cst_now(), session_id="deleted-session"))
    migrate_device_ownership(engine)
    migrate_device_ownership(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT owner_user_id,volume,legacy_note FROM devices WHERE device_id=:did"), dict(did=DID)).one() == ("owner", 27, "kept")
        assert conn.execute(text("SELECT owner_user_id FROM devices WHERE device_id='unbound_device'")).one() == (None,)
        assert conn.execute(text("SELECT text FROM device_memory")).scalar() == "保留"
        assert conn.execute(text("SELECT session_id FROM scheduled_tasks")).scalar() is None
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    assert "legacy_note_index" in {item["name"] for item in inspect(engine).get_indexes("devices")}
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(metadata.tables["device_memory"].insert().values(device_id="new_orphan", title="拒绝", text="孤儿"))


@pytest.mark.parametrize("invalid", ["missing_device", "cross_device_session", "orphan_message"])
def test_migration_rolls_back_ambiguous_legacy_data(tmp_path, invalid):
    engine, metadata = _legacy_engine(tmp_path)
    with engine.begin() as conn:
        if invalid == "missing_device":
            conn.execute(metadata.tables["device_memory"].insert().values(device_id="", title="未知", text="人工核实"))
        elif invalid == "cross_device_session":
            conn.execute(metadata.tables["device_session"].insert().values(id="other-session", device_id=OTHER))
            conn.execute(metadata.tables["scheduled_tasks"].insert().values(device_id=DID, description="归属矛盾", next_run_at=reminder.cst_now(), session_id="other-session"))
        else:
            conn.execute(metadata.tables["device_session_message"].insert().values(session_id="missing", role="user", content="保留"))
    with pytest.raises(RuntimeError, match="迁移中止"):
        migrate_device_ownership(engine)
    assert next(c for c in inspect(engine).get_columns("devices") if c["name"] == "owner_user_id")["nullable"] is False
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM device_memory").scalar() >= 1
        assert not conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '_ownership_new_%'").all()
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_open_subscriber_loses_access_after_unbinding():
    import asyncio

    from websockets.exceptions import ConnectionClosedError

    from deskbot_server.controller.auth import _DeviceSubscriberSocket

    async def exercise():
        svc, owner, _ = _accounts()
        raw = SimpleNamespace(state=SimpleNamespace(user_id=owner.id), url=SimpleNamespace(query="", path="/camera_view"),
                              client=None, headers={}, send_text=AsyncMock(), close=AsyncMock(), client_state=None)
        socket = _DeviceSubscriberSocket(raw, DID)
        await socket.send("before")
        assert svc.unbind_device(owner.id, DID)
        with pytest.raises(ConnectionClosedError):
            await socket.send("after")
        raw.send_text.assert_awaited_once_with("before")
        raw.close.assert_awaited_once_with(code=1008, reason="forbidden_device")
    asyncio.run(exercise())


def test_resource_queries_cannot_mutate_another_devices_rows():
    from deskbot_server.dao import device_profile_face_mapper, device_profile_voice_mapper, scheduled_task_mapper

    data = _seed(DID)
    ensure_device_identity(OTHER)
    assert memory.get_memory(data["memory"]["id"], device_id=OTHER) is None
    assert memory.update_memory(data["memory"]["id"], "跨设备覆盖", device_id=OTHER) is None
    assert not memory.delete_memory(data["memory"]["id"], device_id=OTHER)
    for mapper, kind in ((device_profile_face_mapper, "face"), (device_profile_voice_mapper, "voice")):
        pid = data[kind]["id"]
        assert mapper.get_by_id(pid, OTHER) is None
        assert mapper.update_name(pid, "跨设备覆盖", OTHER) == 0
        assert mapper.delete_by_id(pid, OTHER) == 0
    assert scheduled_task_mapper.get_for_device(data["task"]["id"], OTHER) is None
    assert scheduled_task_mapper.delete_one(data["task"]["id"], OTHER) == 0


def test_console_access_follows_device_after_rebinding():
    from deskbot_server.web.app import create_app

    _, owner, other = _accounts()
    data = _seed(DID)
    app = create_app()
    old_client, new_client = app.test_client(), app.test_client()
    for client, email in ((old_client, owner.email), (new_client, other.email)):
        assert client.post("/login", data=dict(email=email, password="password1234")).status_code == 302
    endpoints = ("memories", "scheduled-tasks", "face-profiles", "voice-profiles")
    for name in endpoints:
        assert old_client.get(f"/app/api/{name}?device_id={DID}").status_code == 200
        assert new_client.get(f"/app/api/{name}?device_id={DID}").status_code == 403
    # Even a developer can only access devices currently bound to that account.
    assert old_client.delete(f"/app/api/devices/{DID}").status_code == 200
    assert new_client.post("/app/api/devices", json=dict(device_id=DID)).status_code == 200
    for name in endpoints:
        assert old_client.get(f"/app/api/{name}?device_id={DID}").status_code == 403
        assert new_client.get(f"/app/api/{name}?device_id={DID}").status_code == 200
    assert new_client.get(f"/app/api/memories/{data['memory']['id']}?device_id={DID}").get_json()["memory"]["text"] == "喜欢猫"
    assert old_client.get(f"/app/api/memories/{data['memory']['id']}?device_id={OTHER}").status_code == 404


def test_concurrent_claims_do_not_overwrite_another_owner():
    from concurrent.futures import ThreadPoolExecutor

    svc = UserService()
    users = [svc.register(f"claim-{n}@example.com", "password1234") for n in range(2)]
    from tests.device_bind_helpers import mark_device_online

    mark_device_online(DID)
    def claim(user):
        try:
            svc.bind_device(user.id, DID)
            return user.id
        except ValueError as exc:
            assert "其他账号" in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, users))
    winners = [outcome for outcome in outcomes if outcome]
    assert len(winners) == 1
    assert svc.get_device(DID).owner_user_id == winners[0]


def test_legacy_json_import_registers_device_without_claiming_a_user(tmp_path, monkeypatch):
    import json

    from deskbot_server.db.init_db import init_database
    from deskbot_server.utils import paths

    root = tmp_path / "legacy-data"
    device_dir = root / DID
    device_dir.mkdir(parents=True)
    (device_dir / "user_memory.json").write_text(json.dumps(dict(entries=[dict(text="旧设备记忆")])))
    (device_dir / "face_profiles.json").write_text(json.dumps(dict(profiles=[dict(name="小明", descriptor=[1.] + [0.] * 255)])))
    monkeypatch.setattr(paths, "DATA_DIR", root)
    from deskbot_server.service import device_data_paths

    monkeypatch.setattr(device_data_paths, "DATA_DIR", root)
    init_database()
    init_database()
    assert device_mapper.get_by_device_id(DID).owner_user_id is None
    assert [entry["text"] for entry in memory.list_memory_for_device(DID)] == ["旧设备记忆"]
    assert [profile["name"] for profile in face.list_face_profiles_summary(device_id=DID)] == ["小明"]
    with init_engine().connect() as conn:
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
