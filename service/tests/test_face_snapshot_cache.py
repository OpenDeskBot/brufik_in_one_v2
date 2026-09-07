"""face_snapshot_cache：人脸快照 + 帧级识别耗时（detect_ms）测试。"""

from __future__ import annotations

import pytest

from deskbot_server.service.application import face_snapshot_cache as fsc


def _face(fid: int, **extra) -> dict:
    row = {"face_id": fid, "face_score": 0.95, "person_name": "小明", "identity_score": 0.87}
    row.update(extra)
    return row


def test_update_and_list_device_faces():
    fsc.update_device_faces("dev-f1", [_face(1), _face(2)])
    faces = fsc.list_device_faces("dev-f1")
    assert set(faces) == {1, 2}
    assert faces[1]["person_name"] == "小明"


def test_detect_ms_roundtrip():
    fsc.update_device_faces("dev-f2", [_face(3)], detect_ms=83.6)
    assert fsc.face_snapshot_detect_ms("dev-f2") == 83
    # 更新帧覆盖旧耗时
    fsc.update_device_faces("dev-f2", [], detect_ms=120)
    assert fsc.face_snapshot_detect_ms("dev-f2") == 120
    # 负值/缺省钳制
    fsc.update_device_faces("dev-f2", [], detect_ms=-5)
    assert fsc.face_snapshot_detect_ms("dev-f2") == 0


def test_detect_ms_none_when_never_detected_or_no_device():
    assert fsc.face_snapshot_detect_ms("dev-none") is None
    assert fsc.face_snapshot_detect_ms("") is None
    fsc.update_device_faces("dev-f3", [_face(4)])  # 未传 detect_ms
    assert fsc.face_snapshot_detect_ms("dev-f3") is None


def test_detect_ts_still_tracked():
    fsc.update_device_faces("dev-f4", [_face(5)], detect_ms=66)
    assert fsc.face_snapshot_ts("dev-f4") is not None
    assert fsc.face_snapshot_detect_ms("dev-f4") == 66
