"""剧本任务实例表 SQL Mapper — MyBatis 注解风格。

定义（goal/激活分数/后继等）在剧本 JSON 文件里，本表只存每设备每任务的运行态。
"""

from __future__ import annotations

from deskbot_server.db.models import QuestInstance
from deskbot_server.db.sql_decorators import execute, select, select_one


@select_one(
    "SELECT * FROM quest_instance WHERE device_id = :device_id AND playbook = :playbook AND task_id = :task_id",
    model=QuestInstance,
)
def get_instance(device_id: str, playbook: str, task_id: str) -> QuestInstance | None:
    """按 设备+剧本+任务 查实例。"""


@select(
    "SELECT * FROM quest_instance WHERE device_id = :device_id AND playbook = :playbook ORDER BY task_id",
    model=QuestInstance,
)
def list_instances(device_id: str, playbook: str) -> list[QuestInstance]:
    """列出设备在某个剧本下的所有任务实例。"""


@select(
    "SELECT * FROM quest_instance WHERE playbook = :playbook ORDER BY device_id, task_id",
    model=QuestInstance,
)
def list_by_playbook(playbook: str) -> list[QuestInstance]:
    """列出某剧本下所有设备的实例（删除剧本时清理用）。"""


@execute(
    """
    INSERT INTO quest_instance
        (id, device_id, playbook, task_id, status, current_score,
         started_at, finished_at, result, strategy_override, created_at, updated_at)
    VALUES
        (:id, :device_id, :playbook, :task_id, :status, :current_score,
         :started_at, :finished_at, :result, :strategy_override, :created_at, :updated_at)
    """
)
def insert_instance(
    id: str,
    device_id: str,
    playbook: str,
    task_id: str,
    status: str,
    current_score: int,
    started_at: str | None,
    finished_at: str | None,
    result: str | None,
    strategy_override: str | None,
    created_at: str,
    updated_at: str,
) -> int:
    """插入任务实例（时间列传 ISO 字符串）。"""


@execute(
    """
    UPDATE quest_instance
    SET status = :status, current_score = :current_score, started_at = :started_at,
        finished_at = :finished_at, result = :result, strategy_override = :strategy_override,
        updated_at = :updated_at
    WHERE id = :id
    """
)
def update_instance(
    id: str,
    status: str,
    current_score: int,
    started_at: str | None,
    finished_at: str | None,
    result: str | None,
    strategy_override: str | None,
    updated_at: str,
) -> int:
    """更新实例运行态字段。"""


@execute(
    "UPDATE quest_instance SET task_id = :new_task_id, updated_at = datetime('now') "
    "WHERE device_id = :device_id AND playbook = :playbook AND task_id = :old_task_id"
)
def rename_instance(device_id: str, playbook: str, old_task_id: str, new_task_id: str) -> int:
    """任务改名后同步实例（不破坏运行态）。"""


@execute("DELETE FROM quest_instance WHERE device_id = :device_id AND playbook = :playbook")
def delete_instances(device_id: str, playbook: str) -> int:
    """删除设备在某个剧本下的全部实例（重置用）。"""


@execute(
    "DELETE FROM quest_instance WHERE device_id = :device_id AND playbook = :playbook AND task_id = :task_id"
)
def delete_instance(device_id: str, playbook: str, task_id: str) -> int:
    """删除单条实例（任务被删除时清理）。"""


@execute("DELETE FROM quest_instance WHERE playbook = :playbook")
def delete_by_playbook(playbook: str) -> int:
    """删除某剧本下所有实例（删除剧本时清理）。"""
