"""旧表清理兼容；动态存在性判断和 SQL 均留在数据访问层。"""

from sqlalchemy import inspect

from deskbot_server.dao.decorators import delete as db_delete
from deskbot_server.db.engine import init_engine


def table_exists() -> bool:
    return inspect(init_engine()).has_table("usage_daily_device")


@db_delete("DELETE FROM usage_daily_device WHERE device_id=:device_id")
def delete_by_device(device_id: str) -> int: ...
