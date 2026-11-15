"""Service 创建事务，DAO 通过 transaction 参数参与；只有创建者结束事务。"""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy.orm import Session

from deskbot_server.db.engine import new_session
from deskbot_server.model.errors import TransactionError


@contextmanager
def transaction(existing: Session | None = None):
    if existing is not None:
        try:
            yield existing
        except BaseException:
            existing.info["rollback_only"] = True
            raise
        return
    session = new_session()
    try:
        yield session
        if session.info.get("rollback_only"):
            raise TransactionError()
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
