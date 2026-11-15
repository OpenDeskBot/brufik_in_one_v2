"""定时提醒表的直接 SQL；时间统一使用北京时间的 SQLite 文本。"""

from deskbot_server.dao.decorators import delete as db_delete
from deskbot_server.dao.decorators import insert as db_insert
from deskbot_server.dao.decorators import select, select_one
from deskbot_server.dao.decorators import update as db_update
from deskbot_server.db.models import ScheduledTask


@select_one("SELECT * FROM scheduled_tasks WHERE id = :task_id", model=ScheduledTask)
def get(task_id: str): ...


@select_one(
    """SELECT * FROM scheduled_tasks WHERE device_id=:device_id AND description=:description
AND cron_expr=:cron AND task_kind=:kind AND enabled=1 AND status IN ('active','running')
ORDER BY created_at, id LIMIT 1""",
    model=ScheduledTask,
)
def find_active(device_id: str, description: str, cron: str, kind: str): ...


@select(
    """SELECT * FROM scheduled_tasks WHERE device_id=:device_id
ORDER BY next_run_at, id LIMIT :limit OFFSET :offset""",
    model=ScheduledTask,
)
def list_for_device(device_id: str, limit: int, offset: int): ...


@select_one("SELECT COUNT(*) FROM scheduled_tasks WHERE device_id=:device_id")
def count_for_device(device_id: str) -> int: ...


@db_insert(
    """INSERT INTO scheduled_tasks
(id,device_id,description,cron_expr,task_kind,enabled,next_run_at,session_id,status,created_at)
VALUES (:uid,:device_id,:description,:cron,:kind,1,:next_run,:session_id,'active',:created_at)""",
    id_param="uid",
)
def create(
    uid: str,
    device_id: str,
    description: str,
    cron: str,
    kind: str,
    next_run: str,
    session_id: str | None,
    created_at: str,
) -> str: ...


@db_update("""UPDATE scheduled_tasks SET description=:description,cron_expr=:cron,task_kind=:kind,
enabled=:enabled,next_run_at=:next_run,session_id=:session_id,status=:status,
result_summary=:summary,executed_at=:executed_at WHERE id=:task_id""")
def save(
    task_id: str,
    description: str,
    cron: str,
    kind: str,
    enabled: bool,
    next_run: str,
    session_id: str | None,
    status: str,
    summary: str | None,
    executed_at: str | None,
) -> int: ...


@db_delete("DELETE FROM scheduled_tasks WHERE device_id=:device_id")
def delete_for_device(device_id: str) -> int: ...


@db_delete("DELETE FROM scheduled_tasks WHERE id=:task_id")
def delete_one(task_id: str) -> int: ...


@select(
    """SELECT * FROM scheduled_tasks WHERE enabled=1 AND status='active'
AND next_run_at <= :before AND (:after IS NULL OR next_run_at >= :after)
ORDER BY next_run_at,id LIMIT :limit""",
    model=ScheduledTask,
)
def pending(before: str, after: str | None, limit: int): ...


@db_update("UPDATE scheduled_tasks SET status='running' WHERE id=:task_id AND status='active' AND enabled=1")
def claim(task_id: str) -> int: ...
