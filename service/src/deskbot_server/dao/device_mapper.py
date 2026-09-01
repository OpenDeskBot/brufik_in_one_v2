"""设备表 SQL Mapper — MyBatis 注解风格。"""

from __future__ import annotations

import json

from deskbot_server.db.models import Device
from deskbot_server.db.sql_decorators import execute, select, select_one

# ────────────────────────── 查询 ──────────────────────────


@select("SELECT * FROM devices WHERE owner_user_id = :user_id ORDER BY claimed_at DESC", model=Device)
def list_for_user(user_id: str) -> list[Device]:
    """列出用户所有设备。"""


@select_one("SELECT * FROM devices WHERE device_id = :device_id", model=Device)
def get_by_device_id(device_id: str) -> Device | None:
    """根据 device_id 查找设备。"""


@select("SELECT device_id FROM devices WHERE owner_user_id = :user_id")
def device_ids_for_user(user_id: str) -> list[str]:
    """返回用户绑定的所有 device_id。"""


# ────────────────────────── 写操作 ──────────────────────────


@execute(
    """
    INSERT INTO devices (id, device_id, owner_user_id, display_name, volume, fps, version, auto_reply, servo_mode, live_mode, asr_provider, tts_provider, claimed_at, created_at)
    VALUES (:uid, :device_id, :user_id, :display_name, :volume, :fps, :version, 1, '', 1, 'funasr', 'moss-tts-nano', datetime('now'), datetime('now'))
    """
)
def insert(
    uid: str,
    device_id: str,
    user_id: str,
    display_name: str,
    volume: int = 80,
    fps: int = 10,
    version: str | None = None,
) -> int:
    """插入新设备记录。"""


@execute(
    """
    UPDATE devices
    SET owner_user_id = :user_id,
        display_name  = :display_name
    WHERE id = :device_id_pk
    """
)
def update_owner(device_id_pk: str, user_id: str, display_name: str) -> int:
    """更新设备归属（转绑 / 重绑）。"""


@execute("UPDATE devices SET volume = :volume WHERE device_id = :device_id")
def update_volume(device_id: str, volume: int) -> int:
    """更新设备音量。"""


@execute("UPDATE devices SET device_id = :new_device_id WHERE device_id = :old_device_id")
def update_device_id(old_device_id: str, new_device_id: str) -> int:
    """重置设备 ID（随机后缀变更）。"""


@execute("UPDATE devices SET auto_reply = :auto_reply WHERE device_id = :device_id")
def update_auto_reply(device_id: str, auto_reply: bool) -> int:
    """更新自动应答开关。"""


@execute("UPDATE devices SET servo_mode = :servo_mode WHERE device_id = :device_id")
def update_servo_mode(device_id: str, servo_mode: str) -> int:
    """更新舵机跟随模式。"""


@execute("UPDATE devices SET live_mode = :live_mode WHERE device_id = :device_id")
def update_live_mode(device_id: str, live_mode: bool) -> int:
    """更新活体模式开关。"""


@execute("UPDATE devices SET asr_provider = :asr_provider WHERE device_id = :device_id")
def update_asr_provider(device_id: str, asr_provider: str) -> int:
    """更新设备级 ASR provider。"""


@execute("UPDATE devices SET quest_id = :quest_id WHERE device_id = :device_id")
def update_quest_id(device_id: str, quest_id: str | None) -> int:
    """绑定/解绑设备剧本（None 或空串 = 解绑，置 NULL）。"""


def get_asr_provider(device_id: str) -> str:
    """设备级 ASR provider；设备不存在或未设置 → 默认 funasr。"""
    dev = get_by_device_id(device_id)
    if dev is None:
        return "funasr"
    provider = str(dev.asr_provider or "").strip().lower()
    return provider or "funasr"


def set_asr_provider(device_id: str, provider: str) -> None:
    """设置设备级 ASR provider（funasr / doubao）。"""
    update_asr_provider(device_id, str(provider or "").strip().lower())


@execute("UPDATE devices SET asr_param = :asr_param WHERE device_id = :device_id")
def update_asr_param(device_id: str, asr_param: str | None) -> int:
    """更新设备级 ASR 参数（JSON 文本；None 置 NULL 表示清除）。"""


def get_asr_param(device_id: str) -> dict:
    """设备 asr_param 解析为 dict；无 / 坏 JSON / 设备不存在 → {}。"""
    dev = get_by_device_id(device_id)
    raw = dev.asr_param if dev is not None else None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@execute("UPDATE devices SET tts_provider = :tts_provider WHERE device_id = :device_id")
def update_tts_provider(device_id: str, tts_provider: str) -> int:
    """更新设备级 TTS provider。"""


def get_tts_provider(device_id: str) -> str:
    """设备级 TTS provider；设备不存在或未设置 → 默认 moss-tts-nano。"""
    dev = get_by_device_id(device_id)
    if dev is None:
        return "moss-tts-nano"
    provider = str(dev.tts_provider or "").strip().lower()
    return provider or "moss-tts-nano"


def set_tts_provider(device_id: str, provider: str) -> None:
    """设置设备级 TTS provider（moss-tts-nano / doubao）。"""
    update_tts_provider(device_id, str(provider or "").strip().lower())


@execute("UPDATE devices SET tts_param = :tts_param WHERE device_id = :device_id")
def update_tts_param(device_id: str, tts_param: str | None) -> int:
    """更新设备级 TTS 参数（JSON 文本；None 置 NULL 表示清除）。"""


def get_tts_param(device_id: str) -> dict:
    """设备 tts_param 解析为 dict；无 / 坏 JSON / 设备不存在 → {}。"""
    dev = get_by_device_id(device_id)
    raw = dev.tts_param if dev is not None else None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@execute("DELETE FROM devices WHERE device_id = :device_id AND owner_user_id = :user_id")
def delete_by_device_id(device_id: str, user_id: str) -> int:
    """删除设备绑定。"""


# ────────────────────────── 调试偏好（原 debug_prefs_store）──────────────────────────

_VALID_SERVO_AUTO_MODES = frozenset({"", "follow", "follow_frontal", "gaze"})


def normalize_camera_servo_auto_mode(raw: object) -> str:
    mode = str(raw or "").strip()
    return mode if mode in _VALID_SERVO_AUTO_MODES else ""


def get_auto_reply(device_id: str) -> bool:
    dev = get_by_device_id(device_id)
    return bool(dev.auto_reply) if dev else True


def set_auto_reply(device_id: str, enabled: bool) -> None:
    update_auto_reply(device_id, bool(enabled))
    if not enabled:
        update_servo_mode(device_id, "")


def get_camera_servo_auto_mode(device_id: str) -> str:
    dev = get_by_device_id(device_id)
    if dev is None:
        return ""
    return normalize_camera_servo_auto_mode(dev.servo_mode)


def set_camera_servo_auto_mode(device_id: str, mode: object) -> str:
    norm = normalize_camera_servo_auto_mode(mode)
    update_servo_mode(device_id, norm)
    return norm


def get_live_mode(device_id: str) -> bool:
    dev = get_by_device_id(device_id)
    return bool(dev.live_mode) if dev else True


def set_live_mode(device_id: str, enabled: bool) -> None:
    update_live_mode(device_id, bool(enabled))
