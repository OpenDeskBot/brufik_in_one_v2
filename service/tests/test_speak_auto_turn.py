"""说话前自动转向（替代 set_camera_follow）条件矩阵与删除面断言。"""

from __future__ import annotations

import asyncio

import pytest


def _analysis(center: tuple[float, float] = (160, 120)) -> dict:
    nx, ny = center
    return {"landmarks": [{"name": "nose", "x": nx, "y": ny}], "image_w": 320, "image_h": 240}


@pytest.fixture()
def env(monkeypatch):
    """锁定 auto_reply / 跟随模式，并注入新鲜人脸分析。"""
    import deskbot_server.service.application.interaction_feedback as fb

    state = {"auto_reply": True, "servo_mode": ""}

    monkeypatch.setattr(fb, "get_auto_reply", lambda device_id: state["auto_reply"])
    monkeypatch.setattr(fb, "get_camera_servo_auto_mode", lambda device_id: state["servo_mode"])
    fb._speak_turn_last.clear()  # 防抖时间戳不跨用例泄漏

    def _set(analysis=None, *, auto_reply=True, servo_mode=""):
        state["auto_reply"] = auto_reply
        state["servo_mode"] = servo_mode
        if analysis is not None:
            fb.note_face_analysis("dev_turn", analysis)
        else:
            fb.clear_face_analysis("dev_turn")

    return _set


def test_speak_turn_step_returned_when_conditions_met(env):
    from deskbot_server.service.application.interaction_feedback import maybe_speak_face_turn

    env(_analysis(center=(250, 120)))  # 脸在右 → 向右转
    step = maybe_speak_face_turn("dev_turn", parsed_moves=[], now=1_000.0)
    assert step is not None
    assert step["move"] == "__custom__" and step["ms"] > 0
    assert step["x"] > 90  # 右偏 → x 增大（轴约定：x 大 = 物理右）
    # 转向步须能被现有 expand 链路消费（clamp 后仍合法）
    from deskbot_server.pb.llm_plan import expand_llm_moves

    steps = expand_llm_moves([step])
    assert steps and steps[0]["x"] == step["x"]


@pytest.mark.parametrize(
    "auto_reply,servo_mode,analysis,parsed_moves,desc",
    [
        (False, "", _analysis(), [], "自动回复关"),
        (True, "follow", _analysis(), [], "持续跟随激活"),
        (True, "", None, [], "无新鲜人脸"),
        (True, "", _analysis(), ["look_left"], "模型已表达朝向"),
        (True, "", _analysis(), ["__custom__"], "模型已有自定义朝向"),
    ],
)
def test_speak_turn_skipped(env, auto_reply, servo_mode, analysis, parsed_moves, desc):
    from deskbot_server.service.application.interaction_feedback import maybe_speak_face_turn

    env(analysis, auto_reply=auto_reply, servo_mode=servo_mode)
    assert maybe_speak_face_turn("dev_turn", parsed_moves=parsed_moves, now=1_000.0) is None, desc


def test_speak_turn_allows_non_orientation_moves(env):
    from deskbot_server.service.application.interaction_feedback import maybe_speak_face_turn

    env(_analysis())
    step = maybe_speak_face_turn("dev_turn", parsed_moves=["nod_head", "shake_head"], now=1_000.0)
    assert step is not None  # nod/shake 不表达朝向 → 仍自动转向


def test_speak_turn_debounce_per_device(env):
    from deskbot_server.service.application.interaction_feedback import maybe_speak_face_turn

    env(_analysis())
    assert maybe_speak_face_turn("dev_turn", parsed_moves=[], now=1_000.0) is not None
    # 防抖窗口内第二次 → None；超过间隔后可再触发
    assert maybe_speak_face_turn("dev_turn", parsed_moves=[], now=1_000.5) is None
    assert maybe_speak_face_turn("dev_turn", parsed_moves=[], now=1_002.0) is not None
    # 另一设备不受影响（各自防抖）
    from deskbot_server.service.application.interaction_feedback import note_face_analysis

    note_face_analysis("dev_other", _analysis())
    assert maybe_speak_face_turn("dev_other", parsed_moves=[], now=1_000.6) is not None


# ── 删除面 ─────────────────────────────────────────────


def test_tools_appendix_no_longer_advertises_camera_follow():
    from deskbot_server.infrastructure.llm.utils import llm_tools_prompt_appendix

    text = llm_tools_prompt_appendix()
    assert "set_camera_follow" not in text
    assert "camera_follow" not in text


def test_runner_rejects_camera_follow_as_unknown():
    from deskbot_server.service.application.llm_tool_runner import execute_llm_tools

    results = asyncio.run(
        execute_llm_tools(
            [{"tool": "set_camera_follow", "mode": "follow"}, {"tool": "camera_follow", "value": "follow"}],
            device_id="dev_turn",
        )
    )
    assert len(results) == 2
    assert all(not r["ok"] and "未知工具" in r["error"] for r in results)


def test_interim_phrase_falls_back_to_default():
    from deskbot_server.service.application.tool_interim_tts import build_tool_interim_tts, phrase_for_tool

    assert phrase_for_tool("set_camera_follow") == "稍等一下"
    assert "camera_follow" not in build_tool_interim_tts([{"tool": "camera_follow"}])
