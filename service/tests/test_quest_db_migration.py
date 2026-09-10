"""quest_instance 表迁移测试：success/failed→completed；删 current_score/strategy_override。

用临时 sqlite：先按旧 DDL 建表并灌入旧状态样本，再跑 init_database()，
断言列已删、状态归一、行数据保留，且二次运行幂等。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def legacy_db(monkeypatch):
    from sqlalchemy import text

    from deskbot_server.db.engine import init_engine, reset_engine

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db_path = tmp / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        reset_engine()
        engine = init_engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE quest_instance (
                        id VARCHAR(36) NOT NULL PRIMARY KEY,
                        device_id VARCHAR(128) NOT NULL,
                        playbook VARCHAR(64) NOT NULL DEFAULT 'default',
                        task_id VARCHAR(64) NOT NULL,
                        status VARCHAR(16) NOT NULL DEFAULT 'not_started',
                        current_score INTEGER NOT NULL DEFAULT 0,
                        started_at DATETIME,
                        finished_at DATETIME,
                        result TEXT,
                        strategy_override TEXT,
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO quest_instance
                        (id, device_id, playbook, task_id, status, current_score,
                         started_at, finished_at, result, strategy_override, created_at, updated_at)
                    VALUES
                        ('i1', 'dev1', 'xiaoy', 'g_a', 'success', 10,
                         '2026-09-01 00:00:00', '2026-09-02 00:00:00', '拿到了名字', '策略A',
                         '2026-09-01 00:00:00', '2026-09-02 00:00:00'),
                        ('i2', 'dev1', 'xiaoy', 'g_b', 'failed', 6,
                         '2026-09-01 00:00:00', '2026-09-02 00:00:00', '没问出来', NULL,
                         '2026-09-01 00:00:00', '2026-09-02 00:00:00'),
                        ('i3', 'dev1', 'xiaoy', 'g_c', 'running', 0,
                         '2026-09-02 00:00:00', NULL, NULL, NULL,
                         '2026-09-01 00:00:00', '2026-09-02 00:00:00'),
                        ('i4', 'dev1', 'xiaoy', 'g_d', 'not_started', 0,
                         NULL, NULL, NULL, NULL,
                         '2026-09-01 00:00:00', '2026-09-01 00:00:00')
                    """
                )
            )
        yield db_path
        reset_engine()


def _table_cols(db_path: Path) -> set[str]:
    from sqlalchemy import create_engine, text

    eng = create_engine(f"sqlite:///{db_path}")
    with eng.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(quest_instance)")).fetchall()
    return {r[1] for r in rows}


def _dump(db_path: Path) -> list[tuple]:
    from sqlalchemy import create_engine, text

    eng = create_engine(f"sqlite:///{db_path}")
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT task_id, status, result, started_at, finished_at "
                "FROM quest_instance ORDER BY task_id"
            )
        ).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def test_migrate_legacy_quest_table(legacy_db):
    from deskbot_server.db.init_db import init_database

    init_database()
    cols = _table_cols(legacy_db)
    assert "current_score" not in cols
    assert "strategy_override" not in cols
    rows = _dump(legacy_db)
    assert len(rows) == 4
    by_task = {r[0]: r for r in rows}
    # success/failed → completed；running/not_started 不动；result/时间保留
    assert by_task["g_a"][1] == "completed"
    assert by_task["g_a"][2] == "拿到了名字"
    assert by_task["g_b"][1] == "completed"
    assert by_task["g_b"][2] == "没问出来"
    assert by_task["g_c"][1] == "running"
    assert by_task["g_d"][1] == "not_started"


def test_migrate_quest_table_idempotent(legacy_db):
    from deskbot_server.db.init_db import init_database

    init_database()
    init_database()  # 二跑幂等：不抛、列不再变
    cols = _table_cols(legacy_db)
    assert "current_score" not in cols and "strategy_override" not in cols
    assert len(_dump(legacy_db)) == 4


def test_fresh_db_no_legacy_columns(monkeypatch, tmp_path):
    """全新库 create_all 直接是新模型：无旧列、迁移不炸。"""
    import tempfile

    from deskbot_server.db.engine import reset_engine
    from deskbot_server.db.init_db import init_database

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fresh.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        reset_engine()
        init_database()
        init_database()
        cols = _table_cols(db_path)
        assert "current_score" not in cols
        assert "strategy_override" not in cols
        assert "task_id" in cols and "result" in cols
