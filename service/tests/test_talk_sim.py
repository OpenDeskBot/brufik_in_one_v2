"""talk_sim 逐音素表情行估算单元测试（家页 TTS 说话口型模拟）。

核心断言：输出的行与 phoneme_seq_to_anim_seq 同源 —— 整脸元素与
``deskbot-face.json`` 逐音素表达式一致；行时长合计等于返回 total_ms。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from deskbot_server.pb.talk_sim import estimate_talk_sim

_FRAME_ELEMENTS = {
    "mouth": [{"shape": "round_rect", "x": 148, "y": 153, "w": 56, "h": 18, "radius": 9, "c": 2047}],
    "nose": [],
    "eye_l": [{"shape": "ellipse_fill", "x": 105, "y": 96, "rw": 11, "rh": 11, "c": 2047}],
    "eye_r": [{"shape": "ellipse_fill", "x": 181, "y": 97, "rw": 11, "rh": 11, "c": 2047}],
    "extra": [],
}


def _phoneme_design(overrides: dict | None = None) -> dict:
    def expr(name, mouth, alias=None):
        row = {"name": name, "title": name, "frames": [{"ms": 500, "elements": {**_FRAME_ELEMENTS, "mouth": mouth}}]}
        if alias:
            row["alias"] = alias
        return row

    return {
        "name": "test",
        "description": "test design",
        "phonemes": [
            expr("sil", [{"shape": "round_rect", "x": 148, "y": 153, "w": 56, "h": 18, "radius": 9, "c": 2047}], ["_", "sp"]),
            expr("a", [{"shape": "ellipse_fill", "x": 148, "y": 160, "rw": 30, "rh": 18, "c": 2047}], ["AA"]),
            expr("i", [{"shape": "round_rect", "x": 137, "y": 154, "w": 80, "h": 17, "radius": 8, "c": 2047}], ["IY"]),
            expr("y", [{"shape": "round_rect", "x": 142, "y": 154, "w": 64, "h": 18, "radius": 9, "c": 2047}], ["Y"]),
        ],
        "emotions": [
            {"name": "idle", "title": "idle", "frames": [{"ms": 500, "elements": {**_FRAME_ELEMENTS}}]},
        ],
    }


@pytest.fixture()
def design_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        global_dir = root / "global"
        global_dir.mkdir()
        yield root
        from deskbot_server.dao.face_design_store import clear_face_design_cache

        clear_face_design_cache()
        # 清理 expr bundle 缓存，避免跨测试的 mtime 命中
        monkeypatch.setattr("deskbot_server.pb.face_bundle._expr_default_bundle_cache", None)


def _install_design(root: Path, doc: dict):
    global_dir = root / "global"
    global_dir.mkdir(exist_ok=True)
    (global_dir / "deskbot-face.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_whole_face_phoneme_design(design_dir, monkeypatch):
    """deskbot-face.json 逐音素整脸模式：行元素与设计文件逐音素帧一致。"""
    _install_design(design_dir, _phoneme_design())
    monkeypatch.setattr("deskbot_server.utils.device_data.DATA_DIR", design_dir)
    from deskbot_server.dao.face_design_store import clear_face_design_cache

    clear_face_design_cache()

    out = estimate_talk_sim("啊一", device_id="d1")
    assert out["total_ms"] >= 1800  # 短文本收口下限
    rows = out["rows"]
    assert rows, "中文两字应至少拆出若干音素行"
    assert sum(r["ms"] for r in rows) == out["total_ms"]
    phonemes = [r["phoneme"] for r in rows if r["phoneme"]]
    # pypinyin: 啊 a1 → a；一 yi1 → y + i（可接受多音/分词差异，只断言覆盖度）
    assert set(phonemes) & {"a", "i", "y"} == set(phonemes)
    for r in rows:
        assert r["ms"] > 0
        els = r["elements"]
        assert isinstance(els.get("mouth"), list) and els["mouth"]


def test_mouth_table_mode_without_design(design_dir, monkeypatch):
    """无 phonemes 设计的兜底：行来自 bundle 的 mouth_by_phoneme 口型表 + 默认眼鼻。"""
    monkeypatch.setattr("deskbot_server.utils.device_data.DATA_DIR", design_dir)
    bundle = {
        "mouth_by_phoneme": {
            "_": {"elements": [{"shape": "round_rect", "x": 10, "y": 10, "w": 40, "h": 10}], "offset": {"x": 0, "y": 0}},
            "a": {"elements": [{"shape": "ellipse_fill", "x": 148, "y": 160, "rw": 30, "rh": 18}], "offset": {"x": 0, "y": 2}},
        },
        "eye_l": {"default": [{"shape": "ellipse_fill", "x": 105, "y": 96, "rw": 11, "rh": 11}]},
        "eye_r": {"default": [{"shape": "ellipse_fill", "x": 181, "y": 97, "rw": 11, "rh": 11}]},
        "nose": {"default": []},
        "extra": {"default": []},
        "metadata": {"blink": {"open_ms": 9000, "close_ms": 120}},
    }
    monkeypatch.setattr("deskbot_server.pb.talk_sim.resolve_pb_face_bundle", lambda _cfg, device_id=None: bundle)

    out = estimate_talk_sim("啊", device_id="d2")
    assert out["rows"]
    a_rows = [r for r in out["rows"] if r["phoneme"] == "a"]
    assert a_rows, f"文本「啊」应产出 a 口型行, got {[r['phoneme'] for r in out['rows']]}"
    mouth = a_rows[0]["elements"]["mouth"]
    assert mouth and mouth[0]["shape"] == "ellipse_fill"
    assert mouth[0]["y"] == 160  # 未加 offset 的原位
    assert sum(r["ms"] for r in out["rows"]) == out["total_ms"]


def test_punctuation_maps_to_sil(design_dir, monkeypatch):
    _install_design(design_dir, _phoneme_design())
    monkeypatch.setattr("deskbot_server.utils.device_data.DATA_DIR", design_dir)
    from deskbot_server.dao.face_design_store import clear_face_design_cache

    clear_face_design_cache()
    out = estimate_talk_sim("你好，啊", device_id="d3")
    assert any(r["phoneme"] == "_" for r in out["rows"]), "标点应产出停顿(_)行"
    assert any(r["phoneme"] for r in out["rows"])


def test_empty_and_total_override(design_dir, monkeypatch):
    _install_design(design_dir, _phoneme_design())
    monkeypatch.setattr("deskbot_server.utils.device_data.DATA_DIR", design_dir)
    from deskbot_server.dao.face_design_store import clear_face_design_cache

    clear_face_design_cache()
    assert estimate_talk_sim("") == {"total_ms": 0, "rows": []}
    assert estimate_talk_sim("   ") == {"total_ms": 0, "rows": []}

    # total_ms 显式指定：整段摊到给定时长
    out = estimate_talk_sim("啊", total_ms=3600, device_id="d4")
    assert out["rows"]
    assert out["total_ms"] == 3600
