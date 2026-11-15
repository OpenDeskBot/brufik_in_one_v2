"""具名 SQL 执行、结果映射和事务参与；业务 DAO 只声明 SQL 与对象。"""

from __future__ import annotations

import functools
import inspect
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, MultipleResultsFound, SQLAlchemyError

from deskbot_server.db.engine import new_session
from deskbot_server.model.errors import DataAccessError


def _map_row(row, model):
    if model is None:
        return dict(row._mapping)
    try:
        columns = model.__table__.columns
        values = {key: value for key, value in row._mapping.items() if key in columns}
        for column in model.__table__.columns:
            value = values.get(column.name)
            target = column.type.python_type
            if isinstance(value, str) and target in (datetime, date):
                values[column.name] = target.fromisoformat(value)
        return model(**values)
    except (TypeError, ValueError, AttributeError) as exc:
        raise DataAccessError("mapping", "数据结果映射失败") from exc


def _operation(sql: str, kind: str, model=None, id_param=None):
    def decorate(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def execute_sql(*args, **kwargs):
            supplied = kwargs.pop("transaction", None)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            params = {key: value for key, value in bound.arguments.items() if key not in ("self", "cls")}
            session = supplied if supplied is not None else new_session()
            try:
                result = session.execute(text(sql), params)
                if kind == "query":
                    output = [_map_row(row, model) for row in result.fetchall()]
                elif kind == "scalar":
                    row = result.one_or_none()
                    output = (
                        None
                        if row is None
                        else (_map_row(row, model) if model is not None or len(row._mapping) != 1 else row[0])
                    )
                elif kind == "insert":
                    output = (
                        0
                        if result.rowcount == 0
                        else params[id_param]
                        if id_param
                        else int(session.execute(text("SELECT last_insert_rowid()")).scalar_one())
                    )
                else:
                    output = int(result.rowcount or 0)
                if supplied is None and kind not in ("query", "scalar"):
                    session.commit()
                return output
            except BaseException as exc:
                if supplied is not None:
                    session.info["rollback_only"] = True
                else:
                    session.rollback()
                if isinstance(exc, DataAccessError):
                    raise
                if isinstance(exc, IntegrityError):
                    raise DataAccessError("conflict", "数据约束冲突") from exc
                if isinstance(exc, MultipleResultsFound):
                    raise DataAccessError("cardinality", "单条查询返回多条记录") from exc
                if isinstance(exc, SQLAlchemyError):
                    raise DataAccessError("execution") from exc
                raise
            finally:
                if supplied is None:
                    session.close()

        execute_sql._sql_annotation = sql
        execute_sql._sql_kind = kind
        parameters = list(signature.parameters.values())
        parameters.append(inspect.Parameter("transaction", inspect.Parameter.KEYWORD_ONLY, default=None))
        execute_sql.__signature__ = signature.replace(parameters=parameters)
        return execute_sql

    return decorate


def select(sql: str, model=None):
    return _operation(sql, "query", model)


def select_one(sql: str, model=None):
    return _operation(sql, "scalar", model)


def insert(sql: str, *, id_param=None, model=None):
    return _operation(sql, "insert", model, id_param)


def update(sql: str, model=None):
    return _operation(sql, "update", model)


def delete(sql: str):
    return _operation(sql, "delete")


def execute(sql: str, model=None):
    """旧操作类别兼容入口。"""
    return _operation(sql, "update", model)


sql_query, sql_scalar, sql_insert, sql_update = select, select_one, insert, update
