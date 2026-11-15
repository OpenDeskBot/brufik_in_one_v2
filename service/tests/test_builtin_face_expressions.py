"""内置表情库的角色特征与真机协议约束。"""

from __future__ import annotations

import json
from pathlib import Path

FACE_DESIGN_PATH = Path(__file__).parents[1] / "data" / "global" / "deskbot-face.json"
LAYERS = {"eye_l", "eye_r", "nose", "mouth", "extra"}
SUPPORTED_SHAPES = {
    "circle",
    "circle_outline",
    "ellipse",
    "ellipse_fill",
    "hline",
    "line",
    "pixel",
    "rect",
    "rect_outline",
    "round_rect",
    "round_rect_outline",
    "text",
    "triangle",
    "triangle_fill",
    "vline",
}
LIVELY_SCENES = {
    "idle": 8,
    "happy": 5,
    "surprised": 5,
    "thinking": 6,
    "listening": 6,
    "greeting": 6,
    "curious": 4,
    "got_it": 5,
    "oops": 5,
    "wink": 5,
}


def _load_scenes() -> dict[str, dict]:
    doc = json.loads(FACE_DESIGN_PATH.read_text(encoding="utf-8"))
    return {scene["name"]: scene for scene in doc["emotions"]}


def test_lively_expression_catalog_and_timing() -> None:
    scenes = _load_scenes()

    assert LIVELY_SCENES.keys() <= scenes.keys()
    for name, expected_frames in LIVELY_SCENES.items():
        frames = scenes[name]["frames"]
        assert len(frames) == expected_frames
        assert min(frame["ms"] for frame in frames) <= 180
        assert all(40 <= frame["ms"] <= 30_000 for frame in frames)


def test_lively_expressions_fit_device_vector_limits() -> None:
    scenes = _load_scenes()

    for name in LIVELY_SCENES:
        for frame in scenes[name]["frames"]:
            elements = frame["elements"]
            assert set(elements) == LAYERS
            for layer in LAYERS:
                primitives = elements[layer]
                assert primitives, f"{name}.{layer} must explicitly replace stale device layers"
                assert len(primitives) <= 16
                for primitive in primitives:
                    assert primitive["shape"] in SUPPORTED_SHAPES
                    assert not ({"rotation", "angle", "rot_cx", "rot_cy"} & primitive.keys())


def test_xiaowai_crooked_mouth_is_preserved() -> None:
    scenes = _load_scenes()

    for name in LIVELY_SCENES:
        for frame in scenes[name]["frames"]:
            elements = frame["elements"]
            left_eye_x = elements["eye_l"][0]["x"]
            right_eye_x = elements["eye_r"][0]["x"]
            eye_center_x = (left_eye_x + right_eye_x) / 2
            outer_mouth = elements["mouth"][0]
            mouth_center_x = outer_mouth["x"] + outer_mouth["w"] / 2

            assert mouth_center_x - eye_center_x >= 20, f"{name} lost 小歪's signature crooked mouth"


def test_every_scene_uses_firmware_shapes_and_explicit_layers() -> None:
    # Match pb_model.cpp, rather than the broader browser-only shape vocabulary.
    firmware_shapes = {
        'rect', 'rect_outline', 'circle', 'circle_outline', 'line',
        'ellipse', 'ellipse_fill', 'round_rect', 'round_rect_outline', 'text',
    }
    for scene in _load_scenes().values():
        topology = None
        for frame in scene['frames']:
            assert 40 <= frame['ms'] <= 30_000
            assert set(frame['elements']) == LAYERS
            current = {}
            for layer, primitives in frame['elements'].items():
                assert 0 < len(primitives) <= 16
                assert all(p['shape'] in firmware_shapes for p in primitives)
                current[layer] = [p['shape'] for p in primitives]
            if topology is not None:
                assert current == topology, 'Morphing must retain primitive identity'
            topology = current


def test_transient_emotions_return_to_neutral() -> None:
    scenes = _load_scenes()
    neutral = scenes['idle']['frames'][0]['elements']
    for name in ('happy', 'shy', 'angry', 'surprised', 'sad', 'wake',
                 'greeting', 'curious', 'got_it', 'oops', 'wink'):
        assert scenes[name]['frames'][-1]['elements'] == neutral, name
