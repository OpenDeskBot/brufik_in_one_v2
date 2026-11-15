"""设备偏好请求的纯字段校验与转换。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DevicePreferenceUpdate:
    values: dict[str, bool | str]

    @classmethod
    def from_values(cls, *, asr_auto_reply=None, live_service=None, camera_servo_auto_mode=None):
        values = {}
        for field, raw in (("asr_auto_reply", asr_auto_reply), ("live_service", live_service)):
            if raw is not None:
                word = str(raw).strip().lower()
                if word not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
                    raise ValueError(f"invalid {field}")
                values[field] = word in ("1", "true", "yes", "on")
        if camera_servo_auto_mode is not None:
            mode = str(camera_servo_auto_mode).strip().lower()
            if mode not in ("", "off", "none", "follow", "follow_frontal", "gaze"):
                raise ValueError("invalid camera_servo_auto_mode")
            values["camera_servo_auto_mode"] = "" if mode in ("off", "none") else mode
        return cls(values)
