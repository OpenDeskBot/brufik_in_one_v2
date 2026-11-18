"""设备身份独立于账号；业务数据只引用设备身份。"""

from deskbot_server.dao import device_mapper
from deskbot_server.db.models import _new_id
from deskbot_server.service.device_data_paths import safe_device_data_dir


def require_device_id(device_id: str | None) -> str:
    did = str(device_id or "").strip()
    if not did or len(did) > 128:
        raise ValueError("device_id required (1–128 characters)")
    safe_device_data_dir(did)
    return did


def ensure_device_identity(device_id: str, *, transaction=None) -> str:
    """首次产生数据时登记未绑定设备；重复调用不改设备资料或所有权。"""
    did = require_device_id(device_id)
    device_mapper.ensure_identity(_new_id(), did, transaction=transaction)
    return did
