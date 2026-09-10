"""设备数据清除：删除某台设备的全部云端数据。

**保留绑定与设备设置**——``devices`` 行上的 ``display_name`` / ``volume`` /
``asr``·``tts``·``llm`` 的 provider 与 param / ``quest_id`` 一概不动，只清「跑出来的
内容数据」：记忆、对话、人脸与声纹档案、提醒、剧本进度、米家授权、用量统计、
``data/{device_id}/`` 目录。

刻意为空转的几处（勿"顺手修好"）：

- ``DeviceWsService`` 设备注册表：设备此刻可能在线，清了会伪造离线状态并掐断链路；
- ``LiveService`` / ``camera_servo_follower`` / ``uplink_rate_stats`` 里的每设备
  状态：都是无用户数据的瞬时运行态，自会重建。
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from typing import Any

from sqlalchemy import inspect, text

from deskbot_server.dao import device_memory_mapper, device_usage_mapper, quest_mapper
from deskbot_server.dao import device_session_mapper as session_mapper
from deskbot_server.db.engine import get_session
from deskbot_server.service import face_profile_service, scheduled_task_service, voice_profile_service
from deskbot_server.utils.device_data import (
    ensure_device_data_initialized,
    legacy_device_data_dir,
    safe_device_data_dir,
)

logger = logging.getLogger("deskbot-server")

#: 历史用量表：已无任何读写方，仅旧库残留，故需存在性守卫。
_LEGACY_USAGE_TABLE = "usage_daily_device"


def _delete_legacy_usage_rows(device_id: str) -> int:
    """清理历史用量表；新库没有这张表，直接返回 0。"""
    session = get_session()
    bind = session.get_bind()
    if bind is None or _LEGACY_USAGE_TABLE not in inspect(bind).get_table_names():
        return 0
    result = session.execute(
        text(f"DELETE FROM {_LEGACY_USAGE_TABLE} WHERE device_id = :device_id"),  # noqa: S608 — 表名是常量
        {"device_id": device_id},
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return int(result.rowcount or 0)


def _db_steps(device_id: str) -> list[tuple[str, Callable[[], int]]]:
    """按序执行的删库步骤；每步返回删除行数，目标不存在时返回 0（天然幂等）。"""
    return [
        ("memory", lambda: device_memory_mapper.delete_by_device(device_id)),
        ("face_profiles", lambda: face_profile_service.delete_device_profiles(device_id)),
        ("voice_profiles", lambda: voice_profile_service.delete_device_profiles(device_id)),
        # 顺序有要求：device_session_message 无 device_id，靠 session_id 关联子查询定位
        ("session_messages", lambda: session_mapper.delete_messages_by_device(device_id)),
        ("sessions", lambda: session_mapper.delete_sessions_by_device(device_id)),
        ("quest_instances", lambda: quest_mapper.delete_instances_by_device(device_id)),
        ("scheduled_tasks", lambda: scheduled_task_service.delete_scheduled_tasks_for_device(device_id)),
        ("usage_rows", lambda: device_usage_mapper.delete_by_device(device_id)),
        ("legacy_usage_rows", lambda: _delete_legacy_usage_rows(device_id)),
    ]


def _wipe_files(device_id: str) -> dict[str, Any]:
    """删除设备目录与历史目录，随后重新播种（让善后状态等于「刚绑定」）。

    重新播种而非留空：绑定被保留、设备很可能在线并正在读 ``servo.json`` /
    ``scene_playbooks.json``，缺失时只会降级到硬编码默认值，与刚绑定时的状态
    有细微差异。``ensure_device_data_initialized`` 只复制不存在的文件，
    不会覆盖设备刚写入的值。
    """
    out: dict[str, Any] = {"dir_removed": False, "legacy_dir_removed": False, "seeded": False}
    target = safe_device_data_dir(device_id)  # 非法 id 在此抛 ValueError
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
        out["dir_removed"] = True
    legacy = legacy_device_data_dir(device_id)
    if legacy.is_dir() and not legacy.is_symlink():
        shutil.rmtree(legacy)
        out["legacy_dir_removed"] = True
    out["seeded"] = bool(ensure_device_data_initialized(device_id))
    return out


def _wipe_caches(device_id: str) -> None:
    """清除每设备内存缓存；放最后，读方在清库期间重新填充的也会被一并清掉。"""
    from deskbot_server.service.application import face_snapshot_cache, interaction_feedback, voice_snapshot_cache
    from deskbot_server.service.application.convo_audio_store import ConvoAudioStore

    ConvoAudioStore().clear(device_id)
    voice_snapshot_cache.clear_device(device_id)
    face_snapshot_cache.clear_device(device_id)
    interaction_feedback.clear_face_analysis(device_id)


def clear_device_data(device_id: str) -> dict[str, Any]:
    """清除设备全部云端数据，返回 ``{deleted, files, errors}``。

    单步失败只记进 ``errors`` 而不中断——每一步在目标已不存在时都不报错，
    所以部分失败后直接重试即可收敛。
    """
    did = str(device_id or "").strip()
    deleted: dict[str, int] = {}
    errors: list[str] = []
    files: dict[str, Any] = {"dir_removed": False, "legacy_dir_removed": False, "seeded": False}

    if not did:
        return {"deleted": deleted, "files": files, "errors": ["device_id 不能为空"]}

    for label, step in _db_steps(did):
        try:
            deleted[label] = int(step() or 0)
        except Exception as exc:  # noqa: BLE001 — 单步失败不阻断其余清理
            errors.append(f"{label}: {exc}")
            logger.exception("[device_data] 清除失败 step=%s device_id=%s", label, did)

    try:
        files = _wipe_files(did)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"files: {exc}")
        logger.exception("[device_data] 清除文件失败 device_id=%s", did)

    try:
        _wipe_caches(did)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"caches: {exc}")
        logger.exception("[device_data] 清除缓存失败 device_id=%s", did)

    logger.info("[device_data] 已清除 device_id=%s deleted=%s files=%s errors=%s", did, deleted, files, errors)
    return {"deleted": deleted, "files": files, "errors": errors}
