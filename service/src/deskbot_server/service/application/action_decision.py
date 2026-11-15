"""每轮舵机动作/表情决策：独立「决策模型」（默认 unee-0.8B）。

对话模型只输出纯文本口语；动作/表情由这里的小模型单独决定。决策失败（服务未启动、
超时、输出无法解析）时返回空动作，不阻塞对话，也不回落对话模型。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from typing import Any

from deskbot_server.client.llm.runtime import ResolvedLlmConfig, chat_acompletion
from deskbot_server.service.llm_context import (
    build_action_decision_prompt,
    build_action_decision_user_message,
    list_llm_expression_names,
    list_llm_expression_titles,
    list_llm_move_ids,
    parse_llm_reply,
)
from deskbot_server.service.llm_routing import resolve_decision_llm_config, resolve_device_decision_provider

logger = logging.getLogger("deskbot-server")

# unee systemone 实测 0.3–0.5s；3s 上限避免前置门控在故障时把对话卡住
DECISION_TIMEOUT_SECONDS = 3.0
# 每轮最多一个动作 + 一个表情：0.8B 决策器输出多动作时只取第一个，画面更可控
DECISION_MAX_MOVES = 1
DECISION_MAX_ANIMS = 1
# systemone noul 概率阈值：valid 偏低会误杀正常输入，respond 偏低会过度搭话
DECISION_VALID_MIN = 0.5
DECISION_RESPOND_MIN = 0.5

# unee 的 /v1/systemone 是专门为 choice/noul/score 决策训练的接口：
# 0.8B 在 OpenAI chat 通道里几乎不遵守「只输出 JSON」，直接用 systemone 的
# 选项概率分布更可靠（实测 22 动作 + 17 表情全量候选约 380ms）。
_UNEE_PROVIDER = "unee"
_MOVE_HINTS: dict[str, str] = {
    "center": "回到正中，恢复自然姿态",
    "look_left": "机器人自己向左看",
    "look_right": "机器人自己向右看",
    "look_up": "机器人自己向上看",
    "look_down": "机器人自己向下看",
    "look_upper_left": "机器人自己向左上方看",
    "look_upper_right": "机器人自己向右上方看",
    "look_lower_left": "机器人自己向左下方看",
    "look_lower_right": "机器人自己向右下方看",
    "nod_head": "点头，表示同意、回应或打招呼",
    "nod_head_fast": "快速点头，表示强烈赞同或兴奋",
    "nod_head_slow": "缓慢点头，表示理解或思考",
    "shake_head": "摇头，表示否定或无奈",
    "shake_head_fast": "快速摇头，表示拒绝或着急",
    "shake_head_slow": "缓慢摇头，表示遗憾或温柔否定",
    "clockwise": "顺时针转一圈，表示灵动或调皮",
    "counterclockwise": "逆时针转一圈，表示灵动或调皮",
    "dj": "像打碟一样摇摆，表示兴奋或玩闹",
    "go_play": "向前探身，表示准备行动或出发",
    "tilt_curious": "歪头，表示好奇或没听懂",
    "perk_up": "精神一振，表示被吸引或来了兴致",
    "double_take": "猛地回头再看，表示惊讶或难以置信",
}


def _move_hint(move_id: str) -> str:
    return _MOVE_HINTS.get(move_id, f"动作 {move_id}")


def _systemone_url(api_base: str | None) -> str:
    return str(api_base or "").rstrip("/") + "/systemone"


def build_systemone_payload(
    *,
    device_id: str | None,
    user_text: str,
    reply_text: str | None = None,
    context: str = "",
    judge_input: bool = False,
) -> dict[str, Any] | None:
    """构造 unee ``/v1/systemone`` 决策请求；无任何候选时返回 None。

    ``judge_input=True`` 时追加 ``valid`` / ``respond`` 两个 noul 问句：
    语音轮可在对话模型/TTS 之前先判定「是否正常输入」与「是否需要开口」。
    """
    move_ids = list_llm_move_ids(device_id=device_id)
    titles = list_llm_expression_titles(device_id=device_id)
    if not move_ids and not titles:
        return None
    questions: dict[str, Any] = {}
    if judge_input:
        questions["valid"] = {
            "type": "noul",
            "instructions": (
                "结合 state 里的用户原话与上下文判断：这段文本是正常对机器人说的话吗？"
                "环境噪声、电视/旁人闲聊、ASR 误识别、无意义碎片应判 false。"
            ),
            "criteria": {
                "true": "正常的人类对机器人说的话",
                "false": "环境噪声、旁人闲聊、ASR 误识别或无意义碎片",
            },
        }
        questions["respond"] = {
            "type": "noul",
            "instructions": (
                "结合 state 判断：机器人此刻应该开口回应吗？"
                "用户在跟旁边的人说话（如「我跟你说」「你听我说」「你看」）、"
                "只是语气词/背景音、或明确要求安静时应判 false。"
            ),
            "criteria": {
                "true": "应该开口回应",
                "false": "不需要开口，保持安静",
            },
        }
    if move_ids:
        criteria = {"none": "不需要动作，保持当前姿态"}
        criteria.update({mid: _move_hint(mid) for mid in move_ids})
        questions["gesture"] = {
            "type": "choice",
            "instructions": (
                "根据用户的话和机器人准备说出的回答，为机器人选择本轮最适合的一个舵机动作。"
                "只在确实需要身体动作时才选；不确定或不需要时选 none。"
            ),
            "criteria": criteria,
        }
    if titles:
        criteria = {"none": "保持当前表情"}
        criteria.update(dict(titles))
        questions["expression"] = {
            "type": "choice",
            "instructions": (
                "根据用户的话和机器人准备说出的回答，为机器人选择本轮最适合的一个面部表情。"
                "不确定或不需要时选 none（保持当前表情）。"
            ),
            "criteria": criteria,
        }
    return {
        "state": build_action_decision_user_message(user_text, reply_text, context=context),
        "questions": questions,
    }


def _post_json_sync(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


async def _decide_with_systemone(
    cfg: ResolvedLlmConfig,
    *,
    device_id: str,
    user_text: str,
    reply_text: str | None = None,
    context: str = "",
    judge_input: bool = False,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """unee 原生决策接口：返回 ``(moves, anims, meta)``。

    ``judge_input=True`` 时 meta 里额外带 ``valid`` / ``respond`` 及概率。
    """
    payload = build_systemone_payload(
        device_id=device_id,
        user_text=user_text,
        reply_text=reply_text,
        context=context,
        judge_input=judge_input,
    )
    if payload is None:
        return [], [], {
            "model": cfg.model,
            "raw": "",
            "confidence": {},
            "valid": True,
            "respond": True,
            "valid_prob": None,
            "respond_prob": None,
        }
    data = await asyncio.wait_for(
        asyncio.to_thread(
            _post_json_sync, _systemone_url(cfg.api_base), payload, DECISION_TIMEOUT_SECONDS
        ),
        timeout=DECISION_TIMEOUT_SECONDS,
    )
    move_valid = {x.lower(): x for x in list_llm_move_ids(device_id=device_id)}
    anim_valid = {x.lower(): x for x in list_llm_expression_titles(device_id=device_id)}
    answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    confidence: dict[str, Any] = {}

    def _choice(key: str, valid: dict[str, str]) -> Any | None:
        ans = answers.get(key) if isinstance(answers, dict) else None
        if not isinstance(ans, dict):
            return None
        if ans.get("confidence") is not None:
            confidence[key] = ans.get("confidence")
        name = str(ans.get("choice") or "").strip()
        if not name or name.lower() == "none":
            return None
        return valid.get(name.lower())

    def _noul(key: str, threshold: float) -> tuple[bool, float | None]:
        ans = answers.get(key) if isinstance(answers, dict) else None
        if not isinstance(ans, dict):
            return True, None
        try:
            prob = float(ans.get("noul"))
        except (TypeError, ValueError):
            return True, None
        return prob >= threshold, prob

    valid, valid_prob = _noul("valid", DECISION_VALID_MIN) if judge_input else (True, None)
    respond, respond_prob = _noul("respond", DECISION_RESPOND_MIN) if judge_input else (True, None)
    gesture = _choice("gesture", move_valid)
    expression = _choice("expression", anim_valid)
    moves = [gesture] if gesture else []
    anims = [expression] if expression else []
    return moves, anims, {
        "model": str(data.get("model") or cfg.model),
        "raw": json.dumps(data, ensure_ascii=False),
        "confidence": confidence,
        "valid": valid,
        "respond": respond,
        "valid_prob": valid_prob,
        "respond_prob": respond_prob,
    }


def _canonical(value: str, valid: dict[str, str]) -> str | None:
    """按小写匹配白名单，返回配置里的规范 id/name；未知 → None。"""
    key = str(value or "").strip()
    return valid.get(key.lower()) if key else None


def _move_entry(item: Any, valid: dict[str, str]) -> Any | None:
    if isinstance(item, str):
        return _canonical(item, valid)
    if not isinstance(item, dict):
        return None
    move_id = _canonical(str(item.get("move") or ""), valid)
    if not move_id:
        return None
    try:
        ms = int(item.get("ms", 0))
    except (TypeError, ValueError):
        ms = 0
    return {"move": move_id, "ms": ms} if ms > 0 else move_id


def _anim_entry(item: Any, valid: dict[str, str]) -> Any | None:
    if isinstance(item, str):
        return _canonical(item, valid)
    if not isinstance(item, dict):
        return None
    anim_name = _canonical(str(item.get("anim") or ""), valid)
    if not anim_name:
        return None
    try:
        ms = int(item.get("ms", 0))
    except (TypeError, ValueError):
        ms = 0
    return {"anim": anim_name, "ms": ms} if ms > 0 else anim_name


def parse_action_decision(raw: str, *, device_id: str | None = None) -> tuple[list[Any], list[Any]]:
    """解析决策模型输出并按设备白名单过滤（大小写不敏感，最多 1 动作 + 1 表情）。"""
    parsed = parse_llm_reply(raw)
    move_valid = {x.lower(): x for x in list_llm_move_ids(device_id=device_id)}
    anim_valid = {x.lower(): x for x in list_llm_expression_names(device_id=device_id)}

    moves: list[Any] = []
    raw_moves = parsed.get("moves") or []
    if isinstance(raw_moves, dict):
        raw_moves = [raw_moves]
    for item in raw_moves:
        entry = _move_entry(item, move_valid)
        if entry is not None and entry not in moves:
            moves.append(entry)
        if len(moves) >= DECISION_MAX_MOVES:
            break

    anims: list[Any] = []
    raw_anims = parsed.get("anims") or []
    if isinstance(raw_anims, dict):
        raw_anims = [raw_anims]
    for item in raw_anims:
        entry = _anim_entry(item, anim_valid)
        if entry is not None and entry not in anims:
            anims.append(entry)
        if len(anims) >= DECISION_MAX_ANIMS:
            break
    return moves, anims


async def decide_turn_actions(
    device_id: str | None,
    user_text: str,
    reply_text: str | None = None,
    *,
    request_id: str | None = None,
    judge_input: bool = False,
    context: str = "",
) -> dict[str, Any]:
    """调用决策模型，返回 ``{moves, anims, valid, respond, model, raw, ...}``。

    ``judge_input=True``：在对话模型之前判定输入是否正常（valid）与是否需要
    开口（respond），供语音轮做前置门控。
    该协程从不向调用方抛异常：任何失败都退化为空动作 + valid/respond=True
    （不因决策模型故障把对话变哑）。
    """
    t0 = time.monotonic()
    out: dict[str, Any] = {
        "moves": [],
        "anims": [],
        "valid": True,
        "respond": True,
        "valid_prob": None,
        "respond_prob": None,
        "model": "",
        "raw": "",
        "elapsed_ms": 0,
        "error": None,
    }
    did = str(device_id or "").strip()
    if not did:
        out["error"] = "no device"
        return out
    out["confidence"] = {}

    try:
        provider = resolve_device_decision_provider(did)
        cfg = resolve_decision_llm_config(did)
        out["model"] = cfg.model
        if provider == _UNEE_PROVIDER:
            # unee 用原生 /v1/systemone：chat 通道下 0.8B 不遵守 JSON 指令
            moves, anims, meta = await _decide_with_systemone(
                cfg,
                device_id=did,
                user_text=user_text,
                reply_text=reply_text,
                context=context,
                judge_input=judge_input,
            )
            out["moves"], out["anims"] = moves, anims
            out["raw"] = str(meta.get("raw") or "")
            out["model"] = str(meta.get("model") or out["model"])
            out["confidence"] = meta.get("confidence") or {}
            out["valid"] = bool(meta.get("valid", True))
            out["respond"] = bool(meta.get("respond", True))
            out["valid_prob"] = meta.get("valid_prob")
            out["respond_prob"] = meta.get("respond_prob")
        else:
            system_prompt = build_action_decision_prompt(device_id=did, judge_input=judge_input)
            if not system_prompt:
                out["error"] = "no action/expression catalog"
                return out
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": build_action_decision_user_message(
                        user_text, reply_text, context=context
                    ),
                },
            ]
            content, _meta = await asyncio.wait_for(
                chat_acompletion(
                    messages,
                    device_id=did,
                    config=cfg,
                    temperature=0.2,
                    # 非 unee 通道用 response_format 强制 JSON（unee 已走 systemone）
                    json_mode=True,
                ),
                timeout=DECISION_TIMEOUT_SECONDS,
            )
            out["raw"] = str(content or "")
            parsed = parse_llm_reply(out["raw"])
            out["moves"], out["anims"] = parse_action_decision(out["raw"], device_id=did)
            if judge_input:
                out["valid"] = bool(parsed.get("valid", True))
                out["respond"] = bool(parsed.get("respond", True))
        logger.info(
            "[decision] device_id=%s req=%s model=%s valid=%s respond=%s "
            "moves=%s anims=%s raw=%r",
            did,
            request_id,
            out["model"],
            out["valid"],
            out["respond"],
            out["moves"],
            out["anims"],
            out["raw"][:200],
        )
    except TimeoutError:
        out["error"] = f"timeout({DECISION_TIMEOUT_SECONDS:.0f}s)"
        logger.warning(
            "[decision] 决策模型超时，本轮跳过动作/表情 device_id=%s req=%s", did, request_id
        )
    except Exception as exc:  # 服务未启动 / 云端缺配置 / 输出异常等一律降级
        out["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[decision] 决策模型调用失败，本轮跳过动作/表情 device_id=%s req=%s err=%s",
            did,
            request_id,
            exc,
        )
    finally:
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return out


__all__ = [
    "DECISION_TIMEOUT_SECONDS",
    "build_systemone_payload",
    "decide_turn_actions",
    "parse_action_decision",
]
