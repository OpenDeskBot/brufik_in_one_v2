"""SQLite 设备归属约束迁移。仅启动迁移调用，绝不在请求内重建表。

历史上设备可以先对话再绑定，解绑也曾删除 devices 行。保留这些设备的数据，
为其补建无所有者的设备身份；不能推断归属的数据中止迁移，不猜测账号。
"""

from __future__ import annotations

import logging

from sqlalchemy import Column, MetaData, inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

from deskbot_server.db.models import Base, Device, _new_id

logger = logging.getLogger("deskbot-server")

DEVICE_DATA_TABLES = (
    "device_memory", "device_profile_face", "device_profile_voice", "device_session",
    "scheduled_tasks", "device_usage", "quest_instance",
)
_TABLES = ("devices", *DEVICE_DATA_TABLES, "device_session_message")


def _needs_rebuild(engine, table_name: str) -> bool:
    inspector = inspect(engine)
    table = Base.metadata.tables[table_name]
    actual = inspector.get_foreign_keys(table_name)
    for fk in table.foreign_key_constraints:
        if not any(
            item["constrained_columns"] == [c.name for c in fk.columns]
            and item["referred_table"] == fk.referred_table.name
            and item["referred_columns"] == [e.column.name for e in fk.elements]
            and all(item.get("options", {}).get(key) == value for key, value in (
                ("ondelete", fk.ondelete), ("onupdate", fk.onupdate),
                ("deferrable", fk.deferrable), ("initially", fk.initially),
            ) if value is not None)
            for item in actual
        ):
            return True
    if table_name == "devices":
        owner = next(c for c in inspector.get_columns(table_name) if c["name"] == "owner_user_id")
        return not owner["nullable"] or not any(
            c["name"] == "ck_devices_valid_device_id" for c in inspector.get_check_constraints(table_name)
        )
    if table_name == "device_session":
        return not any(c["column_names"] == ["id", "device_id"] for c in inspector.get_unique_constraints(table_name))
    return False


def migrate_device_ownership(engine) -> None:
    rebuild = [name for name in _TABLES if _needs_rebuild(engine, name)]
    if not rebuild:
        return

    # Prepare model DDL without changing Base metadata. Preserve extra legacy columns,
    # custom indexes and triggers so this migration only changes ownership constraints.
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    inspector = inspect(engine)
    for name in rebuild:
        table = metadata.tables[name]
        for col in inspector.get_columns(name):
            if col["name"] not in table.c:
                default = text(col["default"]) if col["default"] is not None else None
                table.append_column(Column(col["name"], col["type"], nullable=col["nullable"], server_default=default))

    quote = engine.dialect.identifier_preparer.quote
    check = next(c for c in Device.__table__.constraints if c.name == "ck_devices_valid_device_id")
    raw = engine.raw_connection()
    try:
        # SQLite requires FK toggles outside a transaction. BEGIN IMMEDIATE makes the
        # audit, backfill, table replacement and final FK check one atomic operation.
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("BEGIN IMMEDIATE")
        for name in ("devices", *DEVICE_DATA_TABLES):
            bad = raw.execute(
                f"SELECT COUNT(*) FROM {quote(name)} WHERE device_id IS NULL OR NOT ({check.sqltext})"
            ).fetchone()[0]
            if bad:
                raise RuntimeError(f"设备归属迁移中止：{name} 有 {bad} 条无有效 device_id 的记录，请先核实归属")

        for name in DEVICE_DATA_TABLES:
            missing = raw.execute(
                f"SELECT DISTINCT device_id FROM {quote(name)} WHERE device_id NOT IN (SELECT device_id FROM devices)"
            ).fetchall()
            # The original devices table may still require an owner, so backfill
            # after rebuilding it below instead of fabricating a user account.
            if missing:
                logger.info("设备归属迁移：%s 中发现 %d 台未绑定设备", name, len(missing))

        cross_session = raw.execute(
            "SELECT COUNT(*) FROM scheduled_tasks t JOIN device_session s ON s.id=t.session_id "
            "WHERE t.device_id != s.device_id"
        ).fetchone()[0]
        if cross_session:
            raise RuntimeError(f"设备归属迁移中止：有 {cross_session} 条提醒关联其他设备的对话，请先核实归属")
        detached = raw.execute(
            "UPDATE scheduled_tasks SET session_id=NULL WHERE session_id IS NOT NULL "
            "AND session_id NOT IN (SELECT id FROM device_session)"
        ).rowcount
        if detached:
            logger.warning("设备归属迁移：解除 %d 条提醒已不存在的对话引用，提醒内容和设备归属保留", detached)

        for name in rebuild:
            table = metadata.tables[name]
            temporary = "_ownership_new_" + name
            saved_objects = raw.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
                (name,),
            ).fetchall()
            ddl = str(CreateTable(table).compile(engine))
            ddl = ddl.replace("CREATE TABLE " + quote(name), "CREATE TABLE " + quote(temporary), 1)
            raw.execute(ddl)
            cols = ", ".join(quote(c["name"]) for c in inspector.get_columns(name))
            raw.execute(f"INSERT INTO {quote(temporary)} ({cols}) SELECT {cols} FROM {quote(name)}")
            raw.execute(f"DROP TABLE {quote(name)}")
            raw.execute(f"ALTER TABLE {quote(temporary)} RENAME TO {quote(name)}")
            for (sql,) in saved_objects:
                raw.execute(sql)
            existing_indexes = {r[1] for r in raw.execute(f"PRAGMA index_list({quote(name)})")}
            for index in table.indexes:
                if index.name not in existing_indexes:
                    raw.execute(str(CreateIndex(index).compile(engine)))

        # Use model defaults for all device settings, never invent owner_user_id.
        connection = raw.cursor()
        for name in DEVICE_DATA_TABLES:
            for (did,) in raw.execute(
                f"SELECT DISTINCT device_id FROM {quote(name)} WHERE device_id NOT IN (SELECT device_id FROM devices)"
            ).fetchall():
                # Python-side column defaults are populated by SQLAlchemy's execution
                # layer; supply them explicitly for this raw transactional migration.
                defaults = dict(
                    owner_user_id=None, volume=80, fps=10, auto_reply=True, servo_mode="", live_mode=True,
                    conversation_provider="flecket", asr_provider="funasr", tts_provider="moss-tts-nano",
                    llm_provider="", decision_llm_provider="unee",
                )
                from deskbot_server.db.models import _utcnow

                values = dict(id=_new_id(), device_id=did, display_name=did, created_at=_utcnow(), **defaults)
                columns = ", ".join(quote(key) for key in values)
                connection.execute(
                    f"INSERT INTO devices ({columns}) VALUES ({','.join('?' for _ in values)})", tuple(values.values())
                )
        violations = raw.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            tables = ", ".join(sorted({r[0] for r in violations}))
            raise RuntimeError(f"设备归属迁移中止：{tables} 有 {len(violations)} 条无效关联，原数据已保留")
        raw.commit()
        logger.info("设备归属迁移完成：%s", ", ".join(rebuild))
    except BaseException:
        raw.rollback()
        raise
    finally:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.close()
