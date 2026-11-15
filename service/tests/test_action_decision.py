"""动作/表情决策模型测试：白名单过滤、调用参数、失败降级。"""

from __future__ import annotations

import asyncio
import json

import pytest

from deskbot_server.client.llm.runtime import ResolvedLlmConfig
from deskbot_server.service.application import action_decision as ad


@pytest.fixture()
def catalog(monkeypatch):
    """隔离真实的 servo/face 数据文件，固定决策白名单。

    默认走通用 chat JSON 通道（provider=qwen）；unee/systemone 用例在测试内覆盖 provider。
    """
    monkeypatch.setattr(ad, "list_llm_move_ids", lambda device_id=None: ["nod_head", "look_left"])
    monkeypatch.setattr(ad, "list_llm_expression_names", lambda device_id=None: ["happy", "idle"])
    monkeypatch.setattr(
        ad, "list_llm_expression_titles", lambda device_id=None: {"happy": "开心", "idle": "日常待机"}
    )
    monkeypatch.setattr(ad, "resolve_device_decision_provider", lambda device_id: "qwen")


def _cfg() -> ResolvedLlmConfig:
    return ResolvedLlmConfig(
        model="unee",
        api_key="",
        api_base="http://127.0.0.1:9107/v1",
        protocol="openai",
        source="device",
        display_name="本地 unee（unee）",
    )


def test_parse_action_decision_filters_unknown_and_caps(catalog):
    raw = json.dumps(
        {
            "gesture": ["NOD_HEAD", "__no_such_move__", "look_left"],
            "expression": ["HAPPY", "idle"],
        },
        ensure_ascii=False,
    )
    moves, anims = ad.parse_action_decision(raw, device_id="dev")
    assert moves == ["nod_head"]  # 大小写归一 + 最多 1 个
    assert anims == ["happy"]  # 最多 1 个


def test_parse_action_decision_accepts_legacy_dict_items(catalog):
    raw = json.dumps(
        {"gesture": [{"move": "look_left", "ms": 500}], "expression": [{"anim": "idle", "ms": 700}]}
    )
    moves, anims = ad.parse_action_decision(raw, device_id="dev")
    assert moves == [{"move": "look_left", "ms": 500}]
    assert anims == [{"anim": "idle", "ms": 700}]


def test_parse_action_decision_plain_text_returns_empty(catalog):
    assert ad.parse_action_decision("我有点开心", device_id="dev") == ([], [])


def test_decide_turn_actions_calls_decision_model(catalog, monkeypatch):
    captured: dict = {}

    async def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return json.dumps({"gesture": ["nod_head"], "expression": ["happy"]}), {"model": "unee"}

    monkeypatch.setattr(ad, "resolve_decision_llm_config", lambda device_id: _cfg())
    monkeypatch.setattr(
        ad, "build_action_decision_prompt", lambda device_id=None, judge_input=False: "SYSTEM"
    )
    monkeypatch.setattr(ad, "chat_acompletion", fake_chat)

    out = asyncio.run(ad.decide_turn_actions("dev", "你好", "你好呀", request_id="r1"))

    assert out["moves"] == ["nod_head"]
    assert out["anims"] == ["happy"]
    assert out["error"] is None
    assert out["model"] == "unee"
    assert captured["kwargs"]["json_mode"] is True
    assert captured["kwargs"]["config"].model == "unee"
    assert captured["kwargs"]["temperature"] == 0.2
    assert captured["messages"][0] == {"role": "system", "content": "SYSTEM"}
    user_content = captured["messages"][1]["content"]
    assert "用户说：你好" in user_content
    assert "机器人回答：你好呀" in user_content


def test_decide_turn_actions_silent_round_marks_no_reply(catalog, monkeypatch):
    captured: dict = {}

    async def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        return '{"gesture": [], "expression": ["idle"]}', {}

    monkeypatch.setattr(ad, "resolve_decision_llm_config", lambda device_id: _cfg())
    monkeypatch.setattr(
        ad, "build_action_decision_prompt", lambda device_id=None, judge_input=False: "SYSTEM"
    )
    monkeypatch.setattr(ad, "chat_acompletion", fake_chat)

    out = asyncio.run(ad.decide_turn_actions("dev", "（周围人在聊天）", ""))
    assert out["anims"] == ["idle"]
    assert "机器人本轮不开口" in captured["messages"][1]["content"]


def test_decide_turn_actions_failure_returns_empty(catalog, monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("llm-unee 未运行")

    monkeypatch.setattr(ad, "resolve_decision_llm_config", lambda device_id: _cfg())
    monkeypatch.setattr(
        ad, "build_action_decision_prompt", lambda device_id=None, judge_input=False: "SYSTEM"
    )
    monkeypatch.setattr(ad, "chat_acompletion", boom)

    out = asyncio.run(ad.decide_turn_actions("dev", "你好", "你好呀"))
    assert out["moves"] == [] and out["anims"] == []
    assert "llm-unee" in (out["error"] or "")


def test_decide_turn_actions_timeout_returns_empty(catalog, monkeypatch):
    async def slow(*_args, **_kwargs):
        await asyncio.sleep(1)
        return "{}", {}

    monkeypatch.setattr(ad, "DECISION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(ad, "resolve_decision_llm_config", lambda device_id: _cfg())
    monkeypatch.setattr(
        ad, "build_action_decision_prompt", lambda device_id=None, judge_input=False: "SYSTEM"
    )
    monkeypatch.setattr(ad, "chat_acompletion", slow)

    out = asyncio.run(ad.decide_turn_actions("dev", "你好", "你好呀"))
    assert out["moves"] == [] and out["anims"] == []
    assert (out["error"] or "").startswith("timeout")


def test_decide_turn_actions_without_device_skips_call(monkeypatch):
    async def should_not_call(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("no device should not call model")

    monkeypatch.setattr(ad, "chat_acompletion", should_not_call)
    out = asyncio.run(ad.decide_turn_actions(None, "你好", "你好呀"))
    assert out["error"] == "no device"


def test_build_action_decision_prompt_contains_whitelists(monkeypatch):
    from deskbot_server.service import llm_context as utils

    monkeypatch.setattr(utils, "list_llm_move_ids", lambda device_id=None: ["nod_head", "look_left"])
    monkeypatch.setattr(utils, "list_llm_expression_names", lambda device_id=None: ["happy", "idle"])
    prompt = utils.build_action_decision_prompt(device_id="dev")
    assert "nod_head" in prompt and "look_left" in prompt
    assert "happy" in prompt and "idle" in prompt
    assert '"gesture"' in prompt and '"expression"' in prompt
    assert "look_left 是机器人自己向左看" in prompt


def test_build_systemone_payload_has_choice_questions(catalog):
    payload = ad.build_systemone_payload(device_id="dev", user_text="你好", reply_text="你好呀")
    assert payload is not None
    assert "用户说：你好" in payload["state"]
    assert "机器人回答：你好呀" in payload["state"]
    gesture = payload["questions"]["gesture"]
    assert gesture["type"] == "choice"
    assert gesture["criteria"]["none"]
    assert "nod_head" in gesture["criteria"] and "点头" in gesture["criteria"]["nod_head"]
    expression = payload["questions"]["expression"]
    assert expression["criteria"]["happy"] == "开心"
    assert expression["criteria"]["none"]


def test_build_systemone_payload_judge_input_adds_valid_respond(catalog):
    payload = ad.build_systemone_payload(
        device_id="dev", user_text="你好", reply_text=None, judge_input=True
    )
    assert payload is not None
    assert payload["questions"]["valid"]["type"] == "noul"
    assert payload["questions"]["respond"]["type"] == "noul"
    assert "用户说：你好" in payload["state"]
    assert "机器人回答" not in payload["state"]  # 前置门控发生在回复生成之前


def test_decide_with_systemone_parses_valid_respond(catalog, monkeypatch):
    def fake_post(url, payload, timeout):
        return {
            "model": "unee",
            "answers": {
                "valid": {"type": "noul", "noul": 0.91},
                "respond": {"type": "noul", "noul": 0.2},
                "gesture": {"choice": "none"},
                "expression": {"choice": "none"},
            },
        }

    monkeypatch.setattr(ad, "_post_json_sync", fake_post)
    moves, anims, meta = asyncio.run(
        ad._decide_with_systemone(
            _cfg(), device_id="dev", user_text="第3。", reply_text=None, judge_input=True
        )
    )
    assert moves == [] and anims == []
    assert meta["valid"] is True
    assert meta["respond"] is False
    assert meta["valid_prob"] == 0.91
    assert meta["respond_prob"] == 0.2


def test_decide_turn_actions_judge_input_chat_path(catalog, monkeypatch):
    async def fake_chat(messages, **kwargs):
        return json.dumps(
            {"valid": False, "respond": True, "gesture": [], "expression": []}
        ), {}

    monkeypatch.setattr(ad, "resolve_decision_llm_config", lambda device_id: _cfg())
    monkeypatch.setattr(
        ad, "build_action_decision_prompt", lambda device_id=None, judge_input=False: "SYSTEM"
    )
    monkeypatch.setattr(ad, "chat_acompletion", fake_chat)

    out = asyncio.run(
        ad.decide_turn_actions("dev", "第3。", None, judge_input=True)
    )
    assert out["valid"] is False
    assert out["respond"] is True


def test_decide_with_systemone_maps_choice(catalog, monkeypatch):
    captured: dict = {}

    def fake_post(url, payload, timeout):
        captured["url"] = url
        captured["payload"] = payload
        return {
            "model": "unee",
            "answers": {
                "gesture": {"choice": "nod_head", "confidence": 0.9},
                "expression": {"choice": "happy", "confidence": 0.8},
            },
        }

    monkeypatch.setattr(ad, "_post_json_sync", fake_post)
    moves, anims, meta = asyncio.run(
        ad._decide_with_systemone(_cfg(), device_id="dev", user_text="你好", reply_text="你好呀")
    )
    assert captured["url"] == "http://127.0.0.1:9107/v1/systemone"
    assert "questions" in captured["payload"]
    assert moves == ["nod_head"] and anims == ["happy"]
    assert meta["confidence"] == {"gesture": 0.9, "expression": 0.8}


def test_decide_with_systemone_none_means_no_action(catalog, monkeypatch):
    def fake_post(url, payload, timeout):
        return {
            "model": "unee",
            "answers": {
                "gesture": {"choice": "none", "confidence": 0.9},
                "expression": {"choice": "none", "confidence": 0.9},
            },
        }

    monkeypatch.setattr(ad, "_post_json_sync", fake_post)
    moves, anims, _meta = asyncio.run(
        ad._decide_with_systemone(_cfg(), device_id="dev", user_text="你好", reply_text="你好呀")
    )
    assert moves == [] and anims == []


def test_decide_turn_actions_routes_unee_to_systemone(catalog, monkeypatch):
    async def fake_systemone(cfg, *, device_id, user_text, reply_text=None, context="", judge_input=False):
        return ["look_left"], ["idle"], {"model": "unee", "raw": '{"ok":1}', "confidence": {}}

    monkeypatch.setattr(ad, "resolve_device_decision_provider", lambda device_id: "unee")
    monkeypatch.setattr(ad, "resolve_decision_llm_config", lambda device_id: _cfg())
    monkeypatch.setattr(ad, "_decide_with_systemone", fake_systemone)
    out = asyncio.run(ad.decide_turn_actions("dev", "你好", "你好呀"))
    assert out["moves"] == ["look_left"]
    assert out["anims"] == ["idle"]
    assert out["model"] == "unee"
    assert out["error"] is None


def test_run_pb_playback_uses_decision_and_ignores_chat_actions(monkeypatch):
    """对话模型即使输出旧 gesture/expression，也必须被决策模型结果覆盖。"""
    from types import SimpleNamespace

    from deskbot_server.model.chat import ChatTurnResult
    from deskbot_server.service.application import chat_flow as cf

    captured: dict = {}

    class FakeChat:
        tts_cfg = {"sample_rate": 16000}
        settings = SimpleNamespace(pb_random_servo_cfg=lambda: None)

        async def tts_phoneme_segments(self, text, *, device_id=None):
            return 16000, [{"pcm": b"\x00\x00" * 160, "ms": 100, "phoneme": "a"}]

    def fake_build(segs, tts_cfg, **kwargs):
        captured.update(kwargs)
        return [], "req", 0, 16000

    async def fake_send(**_kwargs):
        return False, "completed", None

    monkeypatch.setattr(cf, "build_pb_wire_pairs", fake_build)
    monkeypatch.setattr(cf, "_send_pb_pairs", fake_send)

    async def _run():
        async def _decision():
            return {"moves": ["nod_head"], "anims": ["happy"], "error": None}

        result = ChatTurnResult()
        parsed = {
            "moves": ["shake_head"],  # 对话模型的旧字段：必须被忽略
            "anims": ["idle"],
            "servo": [],
            "scenes": [],
            "need_reply": True,
        }
        task = asyncio.create_task(_decision())
        await cf._run_pb_playback(
            FakeChat(),
            reply_text="你好呀",
            parsed=parsed,
            llm_scenes=[],
            request_id="r1",
            device_id=None,
            result=result,
            t_asr_start=None,
            prefetch_tts=None,
            decision_task=task,
        )
        return result

    result = asyncio.run(_run())
    assert captured["moves"] == ["nod_head"]
    assert captured["anims"] == ["happy"]
    assert result.moves == ["nod_head"]
    assert result.anims == ["happy"]
