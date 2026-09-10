"""设备「清除数据」：路径护栏 / 全量删库 / 保留绑定与设置 / 重新播种 / 幂等。

注意：``clear_device_data`` 只 import ``device_data`` 里的**函数**（不 import ``DATA_DIR``），
所以 ``monkeypatch.setattr("deskbot_server.utils.device_data.DATA_DIR", ...)`` 必须生效——
否则这些用例会 rmtree 真实 ``service/data/``。
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

DID = "brfk_wipe"
OTHER = "brfk_other"


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


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    """隔离的 ``data/`` 根：含共享目录、种子模板与另一台设备的目录。"""
    root = tmp_path / "data"
    for name in ("global", "quest", "services", "device"):
        (root / name).mkdir(parents=True)
    for name in ("servo.json", "device_volume.json", "doubao_tts_speakers.json"):
        (root / name).write_text('{"seed": true}', encoding="utf-8")
    (root / "global" / "llm_system.txt").write_text("共享 prompt\n", encoding="utf-8")
    (root / "quest" / "xiaoy.json").write_text('{"playbook": true}', encoding="utf-8")
    (root / OTHER).mkdir()
    (root / OTHER / "user_info_小明.txt").write_text("别人的数据", encoding="utf-8")
    monkeypatch.setattr("deskbot_server.utils.device_data.DATA_DIR", root)
    return root


def _seed_device_dir(data_root: Path, device_id: str = DID) -> Path:
    ddir = data_root / device_id
    (ddir / "miot" / "cache").mkdir(parents=True)
    (ddir / "servo.json").write_text('{"user": "改过的"}', encoding="utf-8")
    # 内容带 device_id，便于断言「只删了本设备」
    (ddir / "user_info_小明.txt").write_text(f"user_info:{device_id}", encoding="utf-8")
    (ddir / "done_list_小明_20260910.txt").write_text(f"done:{device_id}", encoding="utf-8")
    (ddir / "miot" / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
    (ddir / "miot" / "cache" / "blob.bin").write_bytes(b"x")
    legacy = data_root / "device" / device_id / "audio"
    legacy.mkdir(parents=True)
    (legacy / "req_bot.wav").write_bytes(b"RIFF")
    return ddir


def result_dir_state(data_root: Path, device_id: str = DID) -> set[str]:
    """设备目录里现存的文件名（目录本身不存在时返回空集）。"""
    ddir = data_root / device_id
    return {p.name for p in ddir.iterdir()} if ddir.is_dir() else set()


def _client(email: str):
    from deskbot_server.web.app import create_app

    app = create_app()
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password1234"})
    return client


def _owner(email: str = "wipe@example.com"):
    from tests._auth_compat import create_user
    from tests.device_bind_helpers import bind_device_online

    user = create_user(email, "password1234")
    bind_device_online(user.id, DID)
    return user


def _seed_all_tables(device_id: str = DID) -> dict[str, int]:
    """给 9 张表各播一批该设备的行，返回期望删除数。"""
    from deskbot_server.dao import (
        device_memory_mapper,
        device_profile_face_mapper,
        device_profile_voice_mapper,
        quest_mapper,
    )
    from deskbot_server.dao import device_session_mapper as session_mapper
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import DeviceUsage, _new_id
    from deskbot_server.service.scheduled_task_service import create_scheduled_task

    device_memory_mapper.insert(device_id, "住处", "", "我住在北京")
    device_memory_mapper.insert(device_id, "喜好", "", "喜欢猫")
    device_profile_face_mapper.insert(device_id, "小明", "[0.1,0.2,0.3,0.4]", "embedding")
    device_profile_voice_mapper.insert(device_id, "小明", "[0.1,0.2,0.3,0.4]", "voice")

    sid = _new_id()
    session_mapper.insert_session(sid, device_id, "打招呼")
    session_mapper.insert_message(sid, "user", "你好")
    session_mapper.insert_message(sid, "assistant", "你好呀")

    now_iso = "2026-09-10T00:00:00+00:00"
    quest_mapper.insert_instance(
        _new_id(), device_id, "xiaoy", "t1", "active", None, None, None, now_iso, now_iso
    )

    create_scheduled_task(device_id, "喝水提醒", delay_minutes=10)

    # device_usage 无 mapper，时间列靠 ORM default，故走模型插入
    session = get_session()
    session.add(DeviceUsage(id=_new_id(), date=date(2026, 9, 10), device_id=device_id))
    session.commit()

    return {
        "memory": 2,
        "face_profiles": 1,
        "voice_profiles": 1,
        "session_messages": 2,
        "sessions": 1,
        "quest_instances": 1,
        "scheduled_tasks": 1,
        "usage_rows": 1,
    }


def _table_names() -> set[str]:
    from sqlalchemy import inspect

    from deskbot_server.db.engine import get_session

    session = get_session()
    bind = session.get_bind()
    return set(inspect(bind).get_table_names()) if bind is not None else set()


def _count(device_id: str) -> dict[str, int]:
    from sqlalchemy import text

    from deskbot_server.db.engine import get_session

    session = get_session()
    out = {}
    for label, sql in (
        ("memory", "SELECT COUNT(*) FROM device_memory WHERE device_id = :d"),
        ("face_profiles", "SELECT COUNT(*) FROM device_profile_face WHERE device_id = :d"),
        ("voice_profiles", "SELECT COUNT(*) FROM device_profile_voice WHERE device_id = :d"),
        ("sessions", "SELECT COUNT(*) FROM device_session WHERE device_id = :d"),
        (
            "session_messages",
            "SELECT COUNT(*) FROM device_session_message WHERE session_id IN "
            "(SELECT id FROM device_session WHERE device_id = :d)",
        ),
        ("quest_instances", "SELECT COUNT(*) FROM quest_instance WHERE device_id = :d"),
        ("scheduled_tasks", "SELECT COUNT(*) FROM scheduled_tasks WHERE device_id = :d"),
        ("usage_rows", "SELECT COUNT(*) FROM device_usage WHERE device_id = :d"),
        ("legacy_usage_rows", "SELECT COUNT(*) FROM usage_daily_device WHERE device_id = :d"),
    ):
        if label == "legacy_usage_rows" and "usage_daily_device" not in _table_names():
            continue
        out[label] = int(session.execute(text(sql), {"d": device_id}).scalar() or 0)
    return out


# ─────────────────────── 路径护栏 ───────────────────────


@pytest.mark.parametrize("bad", ["..", ".", "...", "", "global", "quest", "services", "test", "device", "a/b"])
def test_safe_device_data_dir_rejects_unsafe_ids(bad, data_root):
    from deskbot_server.utils.device_data import safe_device_data_dir

    with pytest.raises(ValueError):
        safe_device_data_dir(bad)


def test_safe_device_data_dir_accepts_real_id(data_root):
    from deskbot_server.utils.device_data import safe_device_data_dir

    assert safe_device_data_dir(DID) == data_root / DID


def test_clear_device_data_refuses_traversal_without_touching_disk(temp_db, data_root):
    """``..`` 不得逃出 DATA_DIR：外层哨兵与 data_root 都要完好。"""
    from deskbot_server.service.device_data_service import clear_device_data

    sentinel = data_root.parent / "sentinel.txt"
    sentinel.write_text("别删我", encoding="utf-8")

    result = clear_device_data("..")

    assert result["errors"], "越界 device_id 必须报错"
    assert sentinel.read_text(encoding="utf-8") == "别删我"
    assert data_root.is_dir()
    assert (data_root / "global" / "llm_system.txt").is_file()
    sentinel.unlink()


# ─────────────────────── 全量清理 ───────────────────────


def test_clear_device_data_removes_all_db_rows(temp_db, data_root):
    expected = _seed_all_tables()
    from deskbot_server.service.device_data_service import clear_device_data

    result = clear_device_data(DID)

    assert result["errors"] == []
    for label, want in expected.items():
        assert result["deleted"][label] == want, label
    assert _count(DID) == dict.fromkeys(expected, 0)


def test_clear_device_data_wipes_and_reseeds_device_dir(temp_db, data_root):
    _seed_device_dir(data_root)
    from deskbot_server.service.device_data_service import clear_device_data

    result = clear_device_data(DID)

    assert result["errors"] == []
    assert result["files"]["dir_removed"] is True
    assert result["files"]["legacy_dir_removed"] is True

    ddir = data_root / DID
    # 用户数据清空
    assert not (ddir / "user_info_小明.txt").exists()
    assert not (ddir / "done_list_小明_20260910.txt").exists()
    assert not (ddir / "miot").exists()
    assert not (data_root / "device" / DID).exists()
    # 重新播种：等于「刚绑定」的状态，而非空目录
    assert (ddir / "servo.json").read_text(encoding="utf-8") == '{"seed": true}'
    assert (ddir / "device_volume.json").is_file()


def test_clear_device_data_preserves_shared_and_other_devices(temp_db, data_root):
    _seed_device_dir(data_root)
    _seed_device_dir(data_root, OTHER)
    from deskbot_server.service.device_data_service import clear_device_data

    clear_device_data(DID)

    assert (data_root / "global" / "llm_system.txt").read_text(encoding="utf-8") == "共享 prompt\n"
    assert (data_root / "quest" / "xiaoy.json").is_file()
    assert (data_root / "services").is_dir()
    assert (data_root / OTHER / "user_info_小明.txt").read_text(encoding="utf-8") == f"user_info:{OTHER}"
    assert (data_root / "device" / OTHER / "audio" / "req_bot.wav").is_file()


def _create_legacy_usage_table() -> None:
    """建出历史表 ``usage_daily_device``——新库由 create_all 生成，没有这张表。"""
    from sqlalchemy import text

    from deskbot_server.db.engine import get_session

    get_session().execute(
        text(
            "CREATE TABLE usage_daily_device ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY, api_key_id VARCHAR(36) NOT NULL, "
            "device_id VARCHAR(128) NOT NULL, usage_date DATE NOT NULL, asr_bytes BIGINT NOT NULL, "
            "face_bytes BIGINT NOT NULL, llm_bytes BIGINT NOT NULL, tts_bytes BIGINT NOT NULL)"
        )
    )
    get_session().commit()


def test_clear_device_data_clears_legacy_usage_table(temp_db, data_root):
    """历史用量表带 device_id 且仍有数据，须一并清除。"""
    from sqlalchemy import text

    from deskbot_server.db.engine import get_session
    from deskbot_server.service.device_data_service import clear_device_data

    _create_legacy_usage_table()
    session = get_session()
    session.execute(
        text(
            "INSERT INTO usage_daily_device "
            "(id, api_key_id, device_id, usage_date, asr_bytes, face_bytes, llm_bytes, tts_bytes) "
            "VALUES ('ud1', 'k1', :did, '2026-09-10', 1, 2, 3, 4)"
        ),
        {"did": DID},
    )
    session.execute(
        text(
            "INSERT INTO usage_daily_device "
            "(id, api_key_id, device_id, usage_date, asr_bytes, face_bytes, llm_bytes, tts_bytes) "
            "VALUES ('ud2', 'k1', :did, '2026-09-11', 1, 2, 3, 4)"
        ),
        {"did": OTHER},
    )
    session.commit()

    result = clear_device_data(DID)

    assert result["errors"] == []
    assert result["deleted"]["legacy_usage_rows"] == 1
    assert _count(DID)["legacy_usage_rows"] == 0
    assert _count(OTHER)["legacy_usage_rows"] == 1, "别的设备不许被误删"


def test_clear_device_data_skips_absent_legacy_table(temp_db, data_root):
    """新库没有 usage_daily_device：守卫须静默跳过而非抛 OperationalError。"""
    from deskbot_server.service.device_data_service import clear_device_data

    assert "usage_daily_device" not in _table_names()
    result = clear_device_data(DID)

    assert result["errors"] == []
    assert result["deleted"]["legacy_usage_rows"] == 0


def test_clear_device_data_is_idempotent(temp_db, data_root):
    _seed_all_tables()
    _seed_device_dir(data_root)
    from deskbot_server.service.device_data_service import clear_device_data

    first = clear_device_data(DID)
    second = clear_device_data(DID)

    # 第二次跑时目录已被重新播种，故会被再次删除——幂等性看的是终态而非 dir_removed
    assert first["files"]["dir_removed"] is True
    assert second["errors"] == []
    assert all(v == 0 for v in second["deleted"].values())
    assert result_dir_state(data_root) == {"servo.json", "device_volume.json", "doubao_tts_speakers.json"}


# ─────────────────────── 保留绑定与设置 ───────────────────────


def test_clear_device_data_keeps_binding_and_settings(temp_db, data_root):
    """回归：devices 行与设备级设置一个都不许动。"""
    from deskbot_server.dao import device_mapper
    from deskbot_server.service.device_data_service import clear_device_data

    user = _owner()
    device_mapper.update_volume(DID, 90)
    device_mapper.update_llm_provider(DID, "qwen")
    device_mapper.update_llm_param(DID, json.dumps({"context_window": 8192}))
    device_mapper.update_quest_id(DID, "xiaoy")
    before = device_mapper.get_by_device_id(DID)

    clear_device_data(DID)

    after = device_mapper.get_by_device_id(DID)
    assert after is not None, "绑定必须保留"
    assert after.owner_user_id == user.id
    assert after.volume == 90
    assert after.llm_provider == "qwen"
    assert after.llm_param == before.llm_param
    assert after.quest_id == "xiaoy"
    assert after.display_name == before.display_name


def test_clear_device_data_api_keeps_device_listed(temp_db, data_root):
    user = _owner()
    client = _client("wipe@example.com")

    resp = client.delete(f"/app/api/devices/{DID}/data")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    listed = client.get("/app/api/devices").get_json()
    assert any(d["device_id"] == DID for d in listed["devices"])
    assert user.id


# ─────────────────────── 内存缓存 ───────────────────────


def test_clear_device_data_clears_caches_and_bumps_versions(temp_db, data_root):
    from deskbot_server.service import face_profile_service, voice_profile_service
    from deskbot_server.service.application import face_snapshot_cache, interaction_feedback, voice_snapshot_cache
    from deskbot_server.service.application.convo_audio_store import ConvoAudioStore
    from deskbot_server.service.device_data_service import clear_device_data

    _seed_all_tables()
    voice_snapshot_cache.begin_identification(DID, "r1")
    voice_snapshot_cache.store_voice_sample(DID, "r1", [0.1, 0.2, 0.3, 0.4])
    face_snapshot_cache.update_device_faces(DID, [{"face_id": 1, "person_name": "小明"}], detect_ms=42)
    interaction_feedback.note_face_analysis(DID, {"landmarks": [[0.0, 0.0]]})
    ConvoAudioStore().put_raw(DID, "r1", "face", b"jpeg")

    face_v0 = face_profile_service.get_version()
    voice_v0 = voice_profile_service.get_version()
    clear_device_data(DID)

    assert voice_snapshot_cache.get_voice_snapshot(DID) is None
    assert voice_snapshot_cache.take_voice_sample(DID) is None
    assert face_snapshot_cache.list_device_faces(DID) == {}
    assert face_snapshot_cache.face_snapshot_ts(DID) is None
    assert interaction_feedback.get_valid_face_analysis(DID) is None
    assert ConvoAudioStore().get(DID, "r1", "face") is None
    assert face_profile_service.get_version() > face_v0
    assert voice_profile_service.get_version() > voice_v0


def test_clear_device_data_keeps_ws_registry_online(temp_db, data_root):
    """硬约束：设备可能在线，注册表绝不能被清（否则伪造离线并掐断链路）。"""
    from deskbot_server.service.device_data_service import clear_device_data
    from deskbot_server.service.device_ws_service import DeviceWsService

    _owner()  # bind_device_online 已把设备标记为在线
    clear_device_data(DID)

    svc = DeviceWsService.instance()
    assert svc is not None
    assert svc.is_device_online(DID) is True


# ─────────────────────── 鉴权 ───────────────────────


def test_clear_device_data_forbidden_for_other_users_device(temp_db, data_root):
    from tests._auth_compat import create_user

    _owner("owner@example.com")
    create_user("intruder@example.com", "password1234")
    _seed_all_tables()
    _seed_device_dir(data_root)

    client = _client("intruder@example.com")
    resp = client.delete(f"/app/api/devices/{DID}/data")

    assert resp.status_code == 403
    assert resp.get_json()["error"] == "设备不属于当前账号"
    assert _count(DID)["memory"] == 2
    assert (data_root / DID / "user_info_小明.txt").is_file()


def test_clear_device_data_rejects_malformed_id(temp_db, data_root):
    _owner()
    client = _client("wipe@example.com")

    resp = client.delete("/app/api/devices/bad%20id/data")

    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_endpoint_registered():
    from deskbot_server.web.blueprints.app_bp import ENDPOINTS

    assert ENDPOINTS["app.api_clear_device_data"] == "/app/api/devices/{device_id}/data"
