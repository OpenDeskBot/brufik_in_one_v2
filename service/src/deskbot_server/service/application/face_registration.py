"""从摄像头快照注册人脸档案。"""

from __future__ import annotations

from typing import Any

from deskbot_server.service.application.face_snapshot_cache import list_device_faces, resolve_descriptor_from_payload


def register_face_for_device(
    device_id: str, name: str, *, face_id: int | None = None, extra: dict[str, Any] | None = None,
    max_age_s: float | None = None,
    person_id: str | None = None,
    memory_epoch: str | None = None,
) -> dict[str, Any]:
    """将当前帧 ``face_id`` 的人脸写入 ``face_profiles.json``。"""
    from deskbot_server.service.camera_face_service import CameraFaceService

    device_id = str(device_id or "").strip()
    name = str(name or "").strip()
    if not device_id:
        raise ValueError("device_id required")
    if not name:
        raise ValueError("name required")
    if person_id:
        from deskbot_server.service.memory_service import read_person

        read_person(device_id, person_id)

    faces = list_device_faces(device_id, max_age_s=max_age_s)
    if not faces:
        raise ValueError("当前画面无人脸，请确保 ESP32 正在推流且已检出脸")

    if face_id is None:
        if len(faces) == 1:
            face_id = next(iter(faces.keys()))
        else:
            raise ValueError(f"画面中有 {len(faces)} 张脸，请指定 face_id 或让用户说明是左/右/哪一位")
    else:
        face_id = int(face_id)

    face = faces.get(face_id)
    if not face:
        ids = ", ".join(str(k) for k in sorted(faces.keys()))
        raise ValueError(f"face_id={face_id} 不在当前帧中（可用: {ids}）")

    payload = {**face, **(extra or {}), "device_id": device_id, "face_id": face_id, "name": name}
    desc = resolve_descriptor_from_payload(payload)
    if desc is None:
        raise ValueError("无法提取人脸 embedding/特征，请让人正对镜头、五官清晰后再试")

    profile = CameraFaceService().register_face_embedding(name, desc, device_id=device_id)
    from deskbot_server.service.memory_service import ensure_person

    try:
        person = ensure_person(device_id, name, identity="face:" + str(profile["id"]), person_id=person_id,
                               rename=True, expected_epoch=memory_epoch)
        profile["memory_person_id"] = person["id"]
        profile["memory_status"] = "linked"
    except (ValueError, OSError):
        profile["memory_status"] = "pending"
    return {"ok": True, "profile": profile, "face_id": face_id, "device_id": device_id}
