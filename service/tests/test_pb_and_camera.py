from __future__ import annotations

from deskbot_server.pb.shapes import enumerate_zh_phonemes, normalize_primitive_shape
from deskbot_server.pb.wire import build_pb_wire_pairs
from deskbot_server.service.application.camera_frame import (
    analyze_face_detection,
    analyze_face_detections,
    build_face_info_message,
    pick_primary_face,
)
from deskbot_server.service.application.face_tracker import FaceTracker


def _sample_landmarks(
    *,
    left_eye=(100.0, 80.0),
    right_eye=(140.0, 80.0),
    nose=(120.0, 100.0),
    mouth_left=(105.0, 120.0),
    mouth_right=(135.0, 120.0),
) -> list[dict]:
    lx, ly = left_eye
    rx, ry = right_eye
    nx, ny = nose
    mlx, mly = mouth_left
    mrx, mry = mouth_right
    return [
        {"name": "left_eye_outer", "x": lx - 8, "y": ly},
        {"name": "left_eye_inner", "x": lx + 8, "y": ly},
        {"name": "left_eye_iris", "x": lx, "y": ly},
        {"name": "right_eye_inner", "x": rx - 8, "y": ry},
        {"name": "right_eye_outer", "x": rx + 8, "y": ry},
        {"name": "right_eye_iris", "x": rx, "y": ry},
        {"name": "nose", "x": nx, "y": ny},
        {"name": "mouth_left", "x": mlx, "y": mly},
        {"name": "mouth_right", "x": mrx, "y": mry},
    ]


def test_enumerate_zh_phonemes_contains_silence():
    phones = enumerate_zh_phonemes()
    assert "_" in phones
    assert "sil" in phones


def test_normalize_primitive_shape_aliases():
    assert normalize_primitive_shape("fill_rect") == "rect"
    assert normalize_primitive_shape("ellipse_fill") == "ellipse_fill"


def test_face_lcd_scale_and_defaults():
    from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH, scale_primitive
    from deskbot_server.pb.shapes import default_face_circles

    assert FACE_LCD_WIDTH == 284
    assert FACE_LCD_HEIGHT == 240
    nose = scale_primitive({"shape": "circle", "x": 64, "y": 34, "r": 5})
    assert nose["x"] == 142
    assert nose["y"] == 124
    assert nose["r"] == 11
    fc = default_face_circles()
    assert fc["nose"][0]["x"] == 142


def test_analyze_face_detection_empty_landmarks():
    result = analyze_face_detection({"landmarks": []})
    assert "frontal_score" in result
    assert "face_score" in result
    assert result["yaw_deg"] is None
    assert result["gaze_yaw_deg"] is None
    assert result["is_looking_at_camera"] is None


def test_compute_gaze_angles_with_iris():
    from deskbot_server.vision.geometry import compute_gaze_angles, compute_is_looking_at_camera

    gaze = compute_gaze_angles(10.0, 5.0, {"left_eye": 0.5, "right_eye": 0.5}, eye_yaw_range_deg=50.0)
    assert gaze["eye_yaw_offset_deg"] == 0.0
    assert gaze["gaze_yaw_deg"] == 10.0
    assert gaze["gaze_pitch_deg"] == 5.0
    assert compute_is_looking_at_camera(gaze["gaze_yaw_deg"], gaze["gaze_pitch_deg"]) is True


def test_compute_face_score_with_landmarks():
    from deskbot_server.vision.geometry import compute_face_score

    landmarks = _sample_landmarks()
    score = compute_face_score(landmarks, image_w=320, image_h=240)
    assert 0.0 <= score <= 1.0
    assert score > 0.5


def test_decompose_facial_transform_identity():
    from deskbot_server.vision.geometry import decompose_facial_transform_matrix

    # 单位旋转：yaw/pitch/roll ≈ 0
    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    pose = decompose_facial_transform_matrix(ident)
    assert pose is not None
    assert abs(pose["yaw_deg"]) < 0.2
    assert abs(pose["pitch_deg"]) < 0.2


def test_estimate_camera_matrix_from_fov():
    from deskbot_server.vision.geometry import estimate_camera_matrix_from_fov

    k = estimate_camera_matrix_from_fov(320, 240, 120.0)
    assert len(k) == 3
    assert k[0][0] > 0
    assert abs(k[0][2] - 160.0) < 0.01


def test_normalize_camera_face_document_frame_size():
    from deskbot_server.dao.camera_face_config_store import normalize_camera_face_document

    cfg = normalize_camera_face_document({"frame_width": 640, "frame_height": 480})
    assert cfg["frame_width"] == 640
    assert cfg["frame_height"] == 480
    clamped = normalize_camera_face_document({"frame_width": 999, "frame_height": 50})
    assert clamped["frame_width"] == 640
    assert clamped["frame_height"] == 120


def test_build_face_info_skipped_without_yaw():
    analysis = {"yaw_deg": None, "pitch_deg": None, "landmarks": []}
    assert build_face_info_message("d1", analysis, send_face_info=True) is None


def test_analyze_face_detections_multi():
    faces = [
        {
            "face_id": 1,
            "landmarks": _sample_landmarks(),
            "image_w": 320,
            "image_h": 240,
        },
        {
            "face_id": 2,
            "landmarks": _sample_landmarks(
                left_eye=(220.0, 90.0),
                right_eye=(260.0, 90.0),
                nose=(240.0, 110.0),
                mouth_left=(225.0, 130.0),
                mouth_right=(255.0, 130.0),
            ),
            "image_w": 320,
            "image_h": 240,
        },
    ]
    result = analyze_face_detections(faces)
    assert result["face_count"] == 2
    assert len(result["faces"]) == 2
    assert result["landmarks"]


def test_face_tracker_profile_hysteresis():
    import math

    from deskbot_server.vision.face_identity import is_embedding_vector

    # 主服务不推理：descriptor 由外部服务 /detect 算好随 faces 返回，这里用常量向量模拟
    landmarks = _sample_landmarks(nose=(120.0, 115.0))
    emb = [1.0 / math.sqrt(512)] * 512
    face = {
        "landmarks": landmarks,
        "embedding": emb,
        "face_descriptor": emb,
        "descriptor_kind": "embedding",
        "image_w": 320,
        "image_h": 240,
    }
    desc = emb
    assert is_embedding_vector(desc)
    thr = 0.40
    profiles: list = [{"id": 1, "name": "小明", "descriptor": list(desc), "descriptor_kind": "embedding"}]
    tracker = FaceTracker(identity_similarity_threshold=thr, max_dist_px=90.0)
    tracker._profiles = profiles
    tagged = tracker.assign_ids([dict(face)])
    assert tagged[0].get("id") == 1
    assert tagged[0].get("person_name") == "小明"
    # 鼻尖大幅移动仍应同一 face_id
    moved_face = {
        "landmarks": _sample_landmarks(nose=(80.0, 108.0)),
        "embedding": emb,
        "face_descriptor": emb,
        "descriptor_kind": "embedding",
        "image_w": 320,
        "image_h": 240,
    }
    id1 = tagged[0]["face_id"]
    tagged2 = tracker.assign_ids([moved_face])
    assert tagged2[0]["face_id"] == id1
    assert tagged2[0].get("id") == 1


def test_face_tracker_assigns_stable_ids():
    import math

    landmarks = _sample_landmarks(nose=(120.0, 115.0))
    emb = [1.0 / math.sqrt(512)] * 512  # 模拟外部服务返回的 embedding
    tracker = FaceTracker(max_dist_px=30.0, max_lost_frames=3)
    frame1 = [{"landmarks": landmarks, "embedding": emb, "face_descriptor": emb, "descriptor_kind": "embedding"}]
    frame2 = [
        {
            "landmarks": _sample_landmarks(nose=(105.0, 102.0)),
            "embedding": emb,
            "face_descriptor": emb,
            "descriptor_kind": "embedding",
        }
    ]
    id1 = tracker.assign_ids(frame1)[0]["face_id"]
    id2 = tracker.assign_ids(frame2)[0]["face_id"]
    assert id1 == id2


def test_compute_frontal_angle():
    from deskbot_server.vision.geometry import compute_frontal_angle_deg, compute_is_frontal_by_angle

    assert compute_frontal_angle_deg(10.0, -8.0) == 10.0
    assert compute_is_frontal_by_angle(10.0, 8.0, threshold_deg=15.0) is True
    assert compute_is_frontal_by_angle(20.0, 5.0, threshold_deg=15.0) is False


def test_resolve_descriptor_from_payload_landmarks():
    from deskbot_server.service.application.face_snapshot_cache import resolve_descriptor_from_payload

    landmarks = _sample_landmarks()
    desc = resolve_descriptor_from_payload({"landmarks": landmarks})
    assert desc is not None
    assert len(desc) >= 4


def test_resolve_descriptor_from_payload_vector():
    from deskbot_server.service.application.face_snapshot_cache import resolve_descriptor_from_payload

    emb = [0.01] * 512
    desc = resolve_descriptor_from_payload({"embedding": emb, "descriptor_kind": "embedding"})
    assert desc == emb


def test_face_descriptor_similarity():
    from deskbot_server.vision.face_identity import compute_face_descriptor, descriptor_cosine_similarity

    landmarks = _sample_landmarks()
    d1 = compute_face_descriptor(landmarks)
    d2 = compute_face_descriptor(landmarks)
    assert d1 is not None and d2 is not None
    assert descriptor_cosine_similarity(d1, d2) > 0.99


def test_pick_primary_face_prefers_frontal():
    a = {"is_frontal": False, "frontal_score": 0.9}
    b = {"is_frontal": True, "frontal_score": 0.2}
    assert pick_primary_face([a, b]) is b


def test_build_pb_wire_pairs_empty_segs_raises():

    try:
        build_pb_wire_pairs([], {}, servo_plan=[], sample_rate=24000)
        assert False, "expected ValueError"
    except ValueError:
        pass
