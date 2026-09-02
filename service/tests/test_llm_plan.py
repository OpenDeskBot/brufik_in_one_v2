from __future__ import annotations

import json

from deskbot_server.infrastructure.llm.utils import parse_llm_reply
from deskbot_server.pb.llm_plan import (
    expand_llm_anims,
    expand_llm_moves,
    interleave_tts_segs_with_llm_plan,
    map_anim_frames_to_tts_segs,
    merge_llm_plan_anim_rows,
    preset_default_ms,
)


def test_parse_llm_reply_tool_only_array():
    raw = '[{"tool":"set_camera_follow","mode":"follow"}]'
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["tools"] == [{"tool": "set_camera_follow", "mode": "follow"}]
    assert parsed["reply"] == ""


def test_parse_llm_reply_moves_anims():
    raw = (
        '{"need_reply": true, "tts": "你好", '
        '"moves": [{"move": "nod_head", "ms": 540}], '
        '"anims": [{"anim": "default", "ms": 1500}]}'
    )
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["reply"] == "你好"
    assert parsed["moves"] == [{"move": "nod_head", "ms": 540}]
    assert parsed["anims"] == [{"anim": "default", "ms": 1500}]


def test_expand_llm_moves_scales_preset_steps():
    steps = expand_llm_moves([{"move": "nod_head", "ms": 1080}])
    assert len(steps) == 3
    assert sum(s["ms"] for s in steps) == 1080


def test_parse_llm_reply_moves_anims_string_ids():
    """兼容旧名 moves/anims：直接是预设 id / 场景 name 字符串数组。"""
    raw = '{"need_reply": true, "tts": "你好", "moves": ["nod_head", "center"], "anims": ["happy", "idle"]}'
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["moves"] == ["nod_head", "center"]
    assert parsed["anims"] == ["happy", "idle"]
    # 兼容：dict 项带 ms 仍是对象；不带 ms 归一化为字符串
    mixed = parse_llm_reply(
        '{"moves": ["nod_head", {"move": "shake_head", "ms": 500}, {"move": "look_left"}],'
        ' "anims": [{"anim": "angry", "ms": 600}, "sleep"]}'
    )
    assert mixed["moves"] == ["nod_head", {"move": "shake_head", "ms": 500}, "look_left"]
    assert mixed["anims"] == [{"anim": "angry", "ms": 600}, "sleep"]


def test_parse_llm_reply_gesture_expression_ids():
    """新协议字段名 gesture/expression，解析归一为内部 moves/anims。"""
    raw = '{"need_reply": true, "tts": "好呀", "gesture": ["nod_head", "center"], "expression": ["happy", "idle"]}'
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["moves"] == ["nod_head", "center"]
    assert parsed["anims"] == ["happy", "idle"]
    # 新旧名同时出现：新名优先
    both = parse_llm_reply('{"gesture": [], "moves": ["look_left"], "expression": [], "anims": ["angry"]}')
    assert both["moves"] == []
    assert both["anims"] == []


def test_expand_llm_moves_string_uses_preset_default_ms():
    steps = expand_llm_moves(["nod_head"])
    assert len(steps) == 3  # nod_head 预设共 3 步
    assert sum(s["ms"] for s in steps) == preset_default_ms("nod_head")  # 不缩放，用预设默认时长
    unknown = expand_llm_moves(["__no_such_move__"])
    assert unknown == []


def test_expand_llm_moves_look_is_robot_body_frame():
    """look_* 按机器人自身视角执行（人格）：
    servo.json 预设按屏幕观众录制语义存储，模型侧 look_left 必须对调执行存储的 look_right 预设。"""
    from deskbot_server.pb.llm_plan import _resolve_servo_preset_steps

    left = expand_llm_moves(["look_left"])
    right = expand_llm_moves(["look_right"])
    assert left and right
    # 本体视角：模型 look_left == 存储的 look_right 步骤（swap 在生效）；左右物理不同
    assert left == _resolve_servo_preset_steps("look_right")
    assert right == _resolve_servo_preset_steps("look_left")
    assert left != right
    # 若去掉 swap（不映射），look_left 会错误执行存储的 look_left（反向）——以上断言即回归防线


def test_expand_llm_anims_string_uses_scene_default_ms():
    frames = expand_llm_anims(["default"])
    assert frames
    assert all(f["ms"] >= 40 for f in frames)
    assert isinstance(frames[0].get("elements"), dict)


def test_expand_llm_anims_bg_color():
    frames = expand_llm_anims([{"anim": "default", "ms": 200, "bg": "#000000", "color": "yellow"}])
    assert frames
    bg = frames[0]["elements"].get("bg") or []
    assert bg and bg[0]["shape"] == "rect"
    assert bg[0].get("color") == "#000000"


def test_expand_llm_anims_fallback_default():
    frames = expand_llm_anims([{"anim": "__no_such_anim__", "ms": 800}])
    assert frames
    assert sum(f["ms"] for f in frames) == 800
    assert isinstance(frames[0].get("elements"), dict)


def test_interleave_tts_with_llm_plan_parallel():
    segs = [{"phoneme": "n", "ms": 100, "pcm": b"\x00" * 4800}]
    move_steps = [{"xm": 1, "ym": 1, "x": 0, "y": 10, "ms": 200}]
    anim_frames = [{"ms": 150, "elements": {"mouth": [], "eye_l": [], "eye_r": [], "nose": [], "extra": []}}]
    out, servo, anim = interleave_tts_segs_with_llm_plan(segs, move_steps, anim_frames, 24000)
    assert len(out) == 1
    assert out[0]["ms"] == 100
    assert servo[0]["ms"] == 200
    assert anim[0] is not None


def test_map_anim_frames_covers_all_tts_segs_not_index_only():
    """多音素分片时，anims 应按时间轴覆盖，而非只贴前 N 帧。"""
    segs = [{"phoneme": "a", "ms": 200}, {"phoneme": "b", "ms": 200}, {"phoneme": "c", "ms": 200}]
    anim_frames = [
        {"ms": 300, "elements": {"extra": [{"shape": "circle", "x": 1, "y": 2, "r": 3}]}},
        {"ms": 300, "elements": {"extra": [{"shape": "circle", "x": 9, "y": 9, "r": 9}]}},
    ]
    parallel = map_anim_frames_to_tts_segs(segs, anim_frames)
    assert len(parallel) == 3
    assert all(p is not None for p in parallel)
    assert parallel[0]["extra"][0]["x"] == 1
    assert parallel[2]["extra"][0]["x"] == 9


def test_merge_llm_plan_anim_rows_keeps_emotion_mouth_on_silence():
    """纯表情 pb 包（静音承载时长）应保留情绪口型，不被默认嘴型覆盖。"""
    segs = [{"phoneme": "", "ms": 2000, "pcm": b"\x00" * 96000}]
    phoneme_rows = [
        {
            "idx": 0,
            "chunk_ms": 2000,
            "anim": [
                {
                    "elements": {
                        "mouth": [{"shape": "round_rect", "x": 148, "y": 153, "w": 56, "h": 18}],
                        "eye_l": [],
                        "eye_r": [],
                        "nose": [],
                        "extra": [],
                    },
                    "ms": 2000,
                }
            ],
        }
    ]
    plan_el = {
        "mouth": [{"shape": "round_rect", "x": 163, "y": 147, "w": 28, "h": 32}],
        "eye_l": [{"shape": "ellipse_fill", "x": 105, "y": 96, "rw": 15, "rh": 15}],
        "eye_r": [],
        "nose": [],
        "extra": [],
    }
    merged = merge_llm_plan_anim_rows(segs, phoneme_rows, [plan_el])
    mouth = merged[0]["anim"][0]["elements"]["mouth"]
    assert mouth == plan_el["mouth"]


def test_merge_llm_plan_anim_rows_keeps_phoneme_mouth():
    segs = [{"phoneme": "a", "ms": 100, "pcm": b"\x00" * 4800}]
    phoneme_rows = [
        {
            "idx": 0,
            "chunk_ms": 100,
            "anim": [
                {
                    "elements": {
                        "mouth": [{"shape": "rect", "x": 1, "y": 2, "w": 3, "h": 4}],
                        "eye_l": [],
                        "eye_r": [],
                        "nose": [],
                        "extra": [],
                    },
                    "ms": 100,
                    "phoneme": "a",
                }
            ],
        }
    ]
    plan_el = {
        "mouth": [{"shape": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1}],
        "eye_l": [{"shape": "circle", "x": 1, "y": 2, "r": 3}],
        "eye_r": [],
        "nose": [],
        "extra": [],
    }
    merged = merge_llm_plan_anim_rows(segs, phoneme_rows, [plan_el])
    mouth = merged[0]["anim"][0]["elements"]["mouth"]
    assert mouth == phoneme_rows[0]["anim"][0]["elements"]["mouth"]
    assert merged[0]["anim"][0]["elements"]["eye_l"] == plan_el["eye_l"]


def test_llm_face_context_prompt_appendix():
    from deskbot_server.infrastructure.llm.utils import llm_static_context_prompt_appendix

    text = llm_static_context_prompt_appendix("test_device_faces_prompt")
    assert "register_face" in text
    assert "长期记忆" in text
    assert "face_id=" not in text


def test_build_llm_user_message():
    from deskbot_server.infrastructure.llm.utils import build_llm_user_message
    from deskbot_server.service.application.face_snapshot_cache import update_device_faces

    dev = "test_device_user_msg"
    update_device_faces(
        dev,
        [
            {
                "face_id": 1,
                "person_name": "小明",
                "identity_score": 0.82,
                "face_score": 0.95,
                "id": 1,
                "image_w": 320,
                "image_h": 240,
                "landmarks": [{"name": "nose", "x": 200, "y": 140}],
            }
        ],
    )
    ack = '{"type":"pb_ack","req":"abc","idx":9,"ack_type":"pb_end","space":40}'
    msg = build_llm_user_message("你好", device_id=dev, device_context=ack)
    # user 消息只含图像识别/声音识别段：不再注入舵机角度与跟随模式
    assert "水平舵机角度" not in msg
    assert "垂直舵机角度" not in msg
    assert "摄像头跟随模式" not in msg
    assert "机器人传感器信息" not in msg
    assert msg.startswith("[图像识别:")
    assert "faceid=1" in msg
    assert "name=小明" in msg
    assert "脸中心位置=(200,140)" in msg
    assert "用户正文: 你好" in msg

    silent = build_llm_user_message("", device_id=dev, device_context=ack)
    assert "用户正文: [未说话]" in silent


def test_parse_llm_tools():
    raw = '{"tts":"好","tools":[{"tool":"memory_add","text":"喜欢猫"}]}'
    parsed = parse_llm_reply(raw)
    assert parsed["tools"] == [{"tool": "memory_add", "text": "喜欢猫"}]


def test_parse_llm_reply_volume():
    raw = '{"tts":"好","volume":75,"moves":[],"anims":[]}'
    parsed = parse_llm_reply(raw)
    assert parsed["volume"] == 75


def test_parse_llm_reply_ignores_images():
    import base64

    b64 = base64.standard_b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 8).decode()
    raw = json.dumps({"tts": "看", "images": [{"b64": b64, "x": 0, "y": 0, "w": 100, "h": 80}]}, ensure_ascii=False)
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert "images" not in parsed


def test_device_volume_persist(tmp_path, monkeypatch):
    from deskbot_server.dao import device_volume_store as dvs

    vol_file = tmp_path / "device_volume.json"

    def _resolve(path, device_id=None):
        return str(vol_file)

    monkeypatch.setattr(dvs, "resolve_json_path", _resolve)
    monkeypatch.setattr(dvs, "DEVICE_VOLUME_FILE", str(vol_file))
    assert dvs.persist_device_volume(55, device_id="dev1") == 55
    assert dvs.get_device_volume("dev1") == 55
    assert dvs.persist_device_volume(90, device_id="dev1") == 90
    assert dvs.get_device_volume("dev1") == 90
    raw = '{"tts":"好","volume":75,"moves":[],"anims":[]}'
    parsed = parse_llm_reply(raw)
    assert parsed["volume"] == 75
    omit = parse_llm_reply('{"tts":"好","moves":[],"anims":[]}')
    assert omit["volume"] is None


def test_parse_llm_reply_empty_tts_not_raw_json():
    raw = '{"need_reply": true, "tts": "", "moves": [{"move": "shake_head", "ms": 1280}], "anims": []}'
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["reply"] == ""
    assert parsed["moves"] == [{"move": "shake_head", "ms": 1280}]


def test_memory_store_roundtrip():
    from deskbot_server.dao.device_memory_mapper import add_memory, delete_memory, get_memory, list_memory_for_device

    e1 = add_memory("主人喜欢猫", device_id="test_dev_plan")
    assert e1["text"] == "主人喜欢猫"
    rows = list_memory_for_device("test_dev_plan")
    assert len(rows) >= 1
    assert delete_memory(e1["id"], device_id="test_dev_plan")
    assert get_memory(e1["id"], device_id="test_dev_plan") is None
