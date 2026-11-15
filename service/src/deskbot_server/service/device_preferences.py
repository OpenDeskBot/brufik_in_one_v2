"""设备偏好校验与原子更新；Controller 只传入参数并转换响应。"""

from deskbot_server.dao import device_mapper
from deskbot_server.db.transaction import transaction
from deskbot_server.model.device_preferences import DevicePreferenceUpdate


def preferences(device_id):
    row = device_mapper.get_by_device_id(device_id) if device_id else None
    return {
        "asr_auto_reply": bool(row.auto_reply) if row else True,
        "live_service": bool(row.live_mode) if row else True,
        "camera_servo_auto_mode": str(row.servo_mode or "") if row else "",
    }


def update_preferences(device_id, *, asr_auto_reply=None, live_service=None, camera_servo_auto_mode=None):
    if not device_id:
        raise ValueError("请先选择设备")
    changes = DevicePreferenceUpdate.from_values(
        asr_auto_reply=asr_auto_reply, live_service=live_service, camera_servo_auto_mode=camera_servo_auto_mode
    ).values
    writers = {
        "asr_auto_reply": device_mapper.update_auto_reply,
        "live_service": device_mapper.update_live_mode,
        "camera_servo_auto_mode": device_mapper.update_servo_mode,
    }
    with transaction() as tx:
        for field, value in changes.items():
            writers[field](device_id, value, transaction=tx)
    return preferences(device_id)
