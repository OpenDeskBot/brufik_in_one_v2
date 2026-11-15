"""旧导入路径兼容；SQL 执行能力统一归 dao.decorators。"""

from deskbot_server.dao.decorators import execute, select, select_one, sql_insert, sql_query, sql_scalar, sql_update

__all__ = ["execute", "select", "select_one", "sql_insert", "sql_query", "sql_scalar", "sql_update"]
