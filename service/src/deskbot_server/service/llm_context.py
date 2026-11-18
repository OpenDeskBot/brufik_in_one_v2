"""LLM 输出解析等纯文本工具，独立于 funasr/torch 等重依赖，
供 ``deskbot_server`` 主服务与 ``web/app.py`` 共享使用。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

import contextlib

from deskbot_server.constants import SERVO_CFG_FILE
from deskbot_server.dao.face_design_store import resolve_face_design_path
from deskbot_server.dao.face_expr_scenes_store import load_face_expr_scenes_file
from deskbot_server.dao.servo_config_store import load_servo_cfg_file
from deskbot_server.pb.servo_pcm import parse_pb_volume
from deskbot_server.service.device_data_paths import resolve_json_path

_LLM_APPENDIX_CACHE: dict[str, tuple[float, str]] = {}


def _face_expr_scene_entries(*, device_id: str | None = None) -> list[dict[str, Any]]:
    try:
        rows = load_face_expr_scenes_file(seed_if_missing=False, device_id=device_id)
    except (OSError, ValueError, json.JSONDecodeError):
        rows = None
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        frames = row.get("frames")
        if name and isinstance(frames, list) and frames:
            out.append(row)
    out.sort(key=lambda r: (str(r.get("name") or "").lower(), str(r.get("name") or "")))
    return out


def estimate_text_tokens(text: str) -> int:
    """本地引擎无 tokenizer 时的粗略 token 估算（宁多勿少）：CJK 约 1.1 token/字，其余按 3.2 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯")
    return int(cjk * 1.1) + int((len(text) - cjk) / 3.2) + 1


def _cached_appendix(cache_key: str, mtime_path: str, build_fn) -> str:
    global _LLM_APPENDIX_CACHE
    try:
        mtime = os.path.getmtime(mtime_path)
    except OSError:
        return ""
    cached = _LLM_APPENDIX_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    text = build_fn()
    _LLM_APPENDIX_CACHE[cache_key] = (mtime, text)
    return text


def list_llm_move_ids(*, device_id: str | None = None) -> list[str]:
    """设备可用舵机动作预设 id（决策模型白名单）。"""
    try:
        cfg = load_servo_cfg_file(device_id=device_id, fallback_to_global=True)
    except (OSError, ValueError):
        return []
    if not cfg:
        return []
    ids: list[str] = []
    for preset in cfg.get("presets") or []:
        if not isinstance(preset, dict):
            continue
        pid = str(preset.get("id") or "").strip()
        if pid and pid not in ids:
            ids.append(pid)
    return ids


def list_llm_expression_names(*, device_id: str | None = None) -> list[str]:
    """设备可用表情场景名（决策模型白名单）。"""
    names: list[str] = []
    for row in _face_expr_scene_entries(device_id=device_id):
        name = str(row.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def list_llm_expression_titles(*, device_id: str | None = None) -> dict[str, str]:
    """表情场景名 → 中文标题（unee systemone 决策选项描述用）。"""
    out: dict[str, str] = {}
    for row in _face_expr_scene_entries(device_id=device_id):
        name = str(row.get("name") or "").strip()
        if not name or name in out:
            continue
        out[name] = str(row.get("title") or "").strip() or name
    return out


def llm_pb_moves_prompt_appendix(*, device_id: str | None = None) -> str:
    """供决策模型 prompt 追加：合法 ``gesture`` 动作 id 白名单。"""

    def _build() -> str:
        ids = list_llm_move_ids(device_id=device_id)
        if not ids:
            return ""
        body = ", ".join(ids)
        return (
            '  - gesture: 数组，元素从可用动作中选 id，如 ["nod_head", "center"]；'
            "按默认时长执行，不需要时写 []。\n"
            "    look_* 类以机器人自身视角理解：look_left 是机器人自己向左看，像你转头一样。\n"
            f"    可用动作：{body}\n"
        )

    mtime_path = resolve_json_path(SERVO_CFG_FILE, device_id)
    if device_id and not os.path.isfile(mtime_path):
        mtime_path = SERVO_CFG_FILE
    cache_key = f"moves:{device_id or ''}"
    return _cached_appendix(cache_key, mtime_path, _build)


def llm_pb_anims_prompt_appendix(*, device_id: str | None = None) -> str:
    """供决策模型 prompt 追加：合法 ``expression`` 表情名白名单。"""

    def _build() -> str:
        names = list_llm_expression_names(device_id=device_id)
        if not names:
            return ""
        body = ", ".join(names)
        return (
            '  - expression: 数组，元素从可用表情中选，如 ["happy", "idle"]；'
            "按默认时长执行，不需要时写 []。\n"
            f"    未知名会回退 default / idle，仍无则跳过。可用表情：{body}\n"
        )

    def _face_anim_mtime_path() -> str:
        return resolve_face_design_path(device_id=device_id)

    mtime_path = _face_anim_mtime_path()
    cache_key = f"anims:{device_id or ''}"
    return _cached_appendix(cache_key, mtime_path, _build)


def llm_pb_plan_prompt_appendix(*, device_id: str | None = None) -> str:
    """moves + anims 附录合并（替代旧 ``scenes`` / ``servo`` 直写说明）。"""
    parts = [llm_pb_moves_prompt_appendix(device_id=device_id), llm_pb_anims_prompt_appendix(device_id=device_id)]
    return "".join(p for p in parts if p)


def llm_pb_scenes_prompt_appendix(*, device_id: str | None = None) -> str:
    """兼容旧调用名；返回 moves/anims 计划附录。"""
    return llm_pb_plan_prompt_appendix(device_id=device_id)


def build_action_decision_prompt(
    *, device_id: str | None = None, judge_input: bool = False
) -> str:
    """组装「动作/表情决策模型」的 system prompt（与对话模型完全分离）。

    ``judge_input=True`` 时额外要求判定 ``valid``（是否为正常输入）与
    ``respond``（是否需要开口回应）——用于语音轮在 LLM/TTS 之前的前置门控。
    只包含白名单与输出契约；对话内容由调用方放在 user 消息里。
    无任何可选动作/表情时返回空串（调用方跳过决策调用）。
    """
    move_ids = list_llm_move_ids(device_id=device_id)
    expr_names = list_llm_expression_names(device_id=device_id)
    if not move_ids and not expr_names:
        return ""
    lines = [
        "你是机器人动作决策器。根据用户的话和机器人准备说出的回答，"
        "为机器人选择本轮的舵机动作和面部表情。",
        "只输出一个 JSON 对象，不要解释、不要 markdown、不要代码围栏：",
    ]
    if judge_input:
        lines.extend(
            [
                "- valid：这段文本是否是正常对机器人说的话（不是环境噪声、旁人闲聊、"
                "ASR 误识别或无意义碎片）；",
                "- respond：机器人此刻是否需要开口回应（用户在跟旁边的人说话，如"
                "「我跟你说」「你听我说」「你看」、只是语气词/背景音、或明确要求安静时为 false）。",
                '输出格式：{"valid": true, "respond": true, '
                '"gesture": ["动作id"], "expression": ["表情名"]}',
            ]
        )
    else:
        lines.append('{"gesture": ["动作id"], "expression": ["表情名"]}')
    if move_ids:
        lines.append("可用动作（gesture）：" + ", ".join(move_ids))
        lines.append("look_* 类以机器人自身视角理解：look_left 是机器人自己向左看。")
    if expr_names:
        lines.append("可用表情（expression）：" + ", ".join(expr_names))
    lines.append("规则：动作最多选 1 个、表情最多选 1 个；不需要或不确定就输出空数组。")
    if judge_input:
        lines.append("如果 valid 或 respond 为 false，gesture/expression 选空数组或轻微动作。")
        lines.append('示例：{"valid": true, "respond": true, "gesture": [], "expression": []}')
    else:
        lines.append('示例：{"gesture": [], "expression": []}')
    return "\n".join(lines)


def build_action_decision_user_message(
    user_text: str, reply_text: str | None = None, *, context: str = ""
) -> str:
    """决策模型单轮输入：用户原话 + （可选）机器人回复 + 参考上下文。

    ``reply_text=None`` 表示决策发生在对话模型之前（前置门控），不写机器人回复行；
    传入空串表示本轮不开口。
    """
    lines = [f"用户说：{str(user_text or '').strip()[:800]}"]
    if reply_text is not None:
        spoken = str(reply_text or "").strip()
        lines.append(f"机器人回答：{spoken[:800]}" if spoken else "机器人本轮不开口。")
    if context:
        lines.append("参考上下文：\n" + str(context).strip()[:1200])
    return "\n".join(lines)


def llm_memory_prompt_appendix(device_id: str | None = None) -> str:
    """长期记忆列表，注入 system prompt。"""
    from deskbot_server.service.memory_service import list_memory_for_device

    rows = list_memory_for_device(device_id, limit=30) if str(device_id or "").strip() else []
    if not rows:
        return "长期记忆：暂无。"
    lines: list[str] = []
    for e in rows:
        eid = str(e.get("id") or "")
        text = str(e.get("text") or "").strip()
        if text:
            lines.append(f"  - [{eid}] {text}")
    return "长期记忆（可用 memory_delete 删除，id 见方括号）：\n" + "\n".join(lines)


def llm_quest_tasks_prompt_appendix(*, device_id: str | None = None) -> str:
    """「当前剧情任务」附录：绑定剧本（devices.quest_id）的 running 任务列表。

    未绑定 / 剧本缺失 / 无进行中任务 → 空串（不注入）。
    只列优先级最高的 3 个进行中任务（once → long_term → daily，quest_service 排序），
    不展示状态机细节，附 per-type 完成判定指引（依据下方该用户的
    done_list / user_info 记录判断何时调用 complete_task）。
    """
    if not device_id:
        return ""
    from deskbot_server.service.quest_service import TYPE_LABELS, QuestService

    tasks = QuestService().get_current_tasks(str(device_id))
    if not tasks:
        return ""
    lines: list[str] = ["当前剧情任务（进行中，最多列 3 个）："]
    for t in tasks[:3]:
        ttype = str(t.get("type") or "once")
        lines.append(f"  - [{t['task_id']}] {TYPE_LABELS.get(ttype, ttype)}：{t.get('prompt') or ''}")
    lines.append("完成判定与调用指引（工具 complete_task）：")
    lines.append("  - 一次性任务：本次对话已达成其描述的目标才调用一次，reason 写达成内容，完成后自动接续其后继任务；")
    lines.append("  - 日常任务：对该用户今天完成了一次就调用一次并记入其今日记录（见下方）；"
                 "其今日记录里已有该任务的完成行就不要重复调用；")
    lines.append("  - 长期任务：只在取得实质新进展时调用（进展记录见下方该用户资料），可对同一或不同用户分多次累计；")
    lines.append("  - user 参数填当前对话用户（日常/长期必填，一次性可空）；没有可完成/可记录的事项时不要调用。")
    return "\n".join(lines)


# 单轮 system prompt 最多注入的已知用户数 / 整段社交附录总字符上限
USER_SOCIAL_MAX_USERS = 3
USER_SOCIAL_TOTAL_CHAR_CAP = 2000


def recognized_known_users(device_id: str | None) -> list[str]:
    """当前识别出的已知用户：声纹 found 说话人优先，人脸按置信度降序补足。

    去重后最多 ``USER_SOCIAL_MAX_USERS`` 人；无人脸/声纹判定或非法 device → []。
    进程内快照在 LLM 轮内读取（与 user 消息识别行同源）。
    """
    dev = str(device_id or "").strip()
    if not dev:
        return []
    out: list[str] = []
    try:
        from deskbot_server.service.application.voice_snapshot_cache import STATE_FOUND, get_voice_snapshot

        snap = get_voice_snapshot(dev)
        if snap is not None and snap.get("state") == STATE_FOUND:
            name = str(snap.get("name") or "").strip()
            if name and name not in out:
                out.append(name)
    except Exception:
        pass  # 声纹判定异常不影响人脸路径
    try:
        from deskbot_server.service.application.face_snapshot_cache import list_recognized_faces

        for row in list_recognized_faces(dev, limit=5) or []:
            name = str(row.get("person_name") or "").strip()
            if name and name not in out:
                out.append(name)
    except Exception:
        pass  # 人脸快照异常按无人脸处理
    return out[:USER_SOCIAL_MAX_USERS]


def llm_user_social_context_prompt_appendix(*, device_id: str | None = None) -> str:
    """已识别已知用户时的按人情境附录：user_info 资料 + 今日 done_list。

    无 device_id / 无已识别已知用户 / 文件缺失 → 空串（不注入，旧行为不变）。
    直读不缓存：单文件 ≤ 数 KB，且多文件 mtime 会让单键缓存误判失效。
    """
    if not device_id:
        return ""
    names = recognized_known_users(device_id)
    if not names:
        return ""
    from deskbot_server.dao.user_social_store import read_done_list_block, read_user_info_block

    blocks: list[str] = []
    used = 0
    for name in names:
        info = read_user_info_block(str(device_id), name)
        done = read_done_list_block(str(device_id), name)
        info_txt = info if info else "（暂无已记录的自我介绍，不要编造用户资料）"
        done_txt = done if done else "（今日暂无主动互动记录）"
        block = (
            f"{name} 的资料（update_user_info 归档，时间旧→新；[任务id] 开头行为长期剧情任务的进展记录）：\n  {info_txt}\n"
            f"{name} 今日已完成记录（问候/关心记账，以及 [任务id] 开头行的剧情日常任务完成）：\n  {done_txt}"
        )
        cost = len(block)
        if blocks and used + cost > USER_SOCIAL_TOTAL_CHAR_CAP:
            break
        blocks.append(block)
        used += cost
    if not blocks:
        return ""
    head = "已识别到认识的人，以下是按人归档的情境（对话时称呼其名；资料冲突以时间更新者为准）："
    return head + "\n\n" + "\n\n".join(blocks)


def llm_social_active_tasks_prompt_appendix(*, device_id: str | None = None) -> str:
    """「你的当前任务」段：识别到认识的人时的主动社交规则。

    无已知用户 → 空串。规则含记账（update_daily_task）与重复抑制指引；
    明确静默退出语义（不输出任何文字，不硬聊）。
    """
    if not device_id or not recognized_known_users(device_id):
        return ""
    return (
        "你的当前任务（见到认识的人时的主动社交，用于机器人主动发起的对话轮）：\n"
        "- 早上(约 5:00-11:00)/中午(约 11:00-14:00)/晚上(约 17:00-23:00)该时段**第一次**见到"
        "认识的人，主动问候；问候开口前先调用 update_daily_task 记账一次，再开口说问候语；"
        "同一时段问候过（今日记录里已有）就不重复问候。\n"
        "- 早/中/晚饭时间(约 7:00-9:00/11:00-13:00/17:00-19:00)，可以自然地问对方吃饭了没、"
        "打算吃什么或已经吃了什么；同一餐问过并记账后不要重复问。\n"
        "- 几分钟没有交谈、或用户一直在镜头前，只表示暂时冷场，不表示用户离开后又回来；"
        "系统主动问候轮不得据此说「又见到你」「回来啦」「好久不见」或表达想念，也不得为这类意图记账。"
        "只有用户明确说自己回来了，或当前输入明确提供了用户真实离开较长时间后重新出现的事实，"
        "才能自然回应重逢；不要自行推断。\n"
        "- 语气自然口语化、像真人聊天，禁止「已问候/已汇报」式的汇报腔；"
        "用户正在提问、剧情任务轮或定时提醒轮进行中时，以当前事务为先，不要插话问候。\n"
        "- 若此刻没有任何需要主动表达的情形（如刚问候过、刚聊过不久且不在饭点），"
        "本轮无需开口：输出完全空白。不要输出括号说明、规则复述或判断过程。"
    )


def llm_user_last_talk_prompt_appendix(*, device_id: str | None = None) -> str:
    """「与{name}上一次对话的时间是 …」行（拼在当前时间之后，每人一行）。

    仅对已识别已知用户且有服务端打点记录的人输出；无记录/无已知用户 → 空串。
    """
    if not device_id:
        return ""
    names = recognized_known_users(device_id)
    if not names:
        return ""
    from deskbot_server.dao.user_social_store import read_user_last_talk

    lines: list[str] = []
    for name in names:
        ts = read_user_last_talk(str(device_id), name)
        if ts:
            lines.append(f"与{name}上一次对话的时间是{ts}")
    return "\n".join(lines)


def _format_face_line(face: dict[str, Any]) -> str:
    fid = face.get("face_id")
    parts: list[str] = [f"faceid={fid if fid is not None else '?'}"]
    face_score = face.get("face_score")
    if face_score is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(f"人脸置信度={float(face_score):.2f}")
    name = str(face.get("person_name") or "").strip() or "未知"
    parts.append(f"name={name}")
    identity_score = face.get("identity_score")
    if identity_score is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(f"人物识别置信度={float(identity_score):.2f}")
    return ", ".join(parts)


def _sorted_faces_for_message(device_id: str) -> list[dict[str, Any]]:
    from deskbot_server.service.application.face_snapshot_cache import list_device_faces

    faces = list_device_faces(device_id)
    rows: list[dict[str, Any]] = []
    for fid, face in faces.items():
        if not isinstance(face, dict):
            continue
        row = dict(face)
        row.setdefault("face_id", int(fid))
        rows.append(row)
    rows.sort(key=lambda r: (-(float(r.get("identity_score") or 0.0)), int(r.get("face_id") or 0)))
    return rows


def format_voice_speaker_note(device_id: str | None) -> str | None:
    """当前设备「最近一次 VAD」声纹判定的说话人注记（与喂给 LLM 的正文括号同源）。

    found → ``声纹判定：{name}, 说话人识别置信度=…``；unknown（确实陌生）→
    ``声纹判定：陌生人``；identifying → ``声纹判定中``；degraded → ``声纹识别不可用``；
    无快照（引擎关闭/从未判定）/无设备 → None。
    供实验台用户气泡展示本轮「听到谁在说话」，同时是 user 正文括号注记的唯一来源——
    识别中与引擎降级≠陌生人，明示状态而不编造身份。
    """
    dev = str(device_id or "").strip()
    if not dev:
        return None
    from deskbot_server.service.application.voice_snapshot_cache import (
        STATE_DEGRADED,
        STATE_FOUND,
        STATE_UNKNOWN,
        get_voice_snapshot,
    )

    snap = get_voice_snapshot(dev)
    if not snap:
        return None
    state = snap.get("state")
    if state == STATE_FOUND:
        name = str(snap.get("name") or "").strip() or "未知"
        score = snap.get("score")
        try:
            score_text = f", 说话人识别置信度={float(score):.2f}" if score is not None else ""
        except (TypeError, ValueError):
            score_text = ""
        return f"声纹判定：{name}{score_text}"
    if state == STATE_UNKNOWN:
        return "声纹判定：陌生人"
    if state == STATE_DEGRADED:
        return "声纹识别不可用"
    return "声纹判定中"  # identifying：判定进行中，尚不知身份


def build_llm_user_message(
    user_text: str,
    *,
    device_id: str | None = None,
    device_context: str | None = None,
    from_asr: bool = False,
) -> str:
    """按约定格式组装 LLM ``user`` 消息正文（图像识别块 + 用户正文行）。

    - ``[图像识别:…]`` 块列出画面中的人脸（说话人以用户正文括号注记为准，
      画面人物≠说话人）；
    - ``from_asr=True``（语音轮）：用户正文由 ASR 转写而来而非直接文字，
      正文行括号内固定标注来源，并把**声纹判定身份**绑到同一行
      （例：``用户正文（语音转写，声纹判定：小明, 说话人识别置信度=0.87）：你好``），
      让 agent 以声纹身份（真正说话人）优先于图像识别身份；判定中/引擎
      不可用同样明示，不编造「陌生人」；
    - ``from_asr=False``（网页文字/定时等直接文本）：不标语音转写、不带
      声纹注记——上一条语音的判定结果不能错配到本轮直接输入的文字。

    ``device_context``（pb_ack 舵机角度）已不再注入：对话上下文只保留
    视觉/声纹两个感知段，不再给机器人传感器读数（为保持调用签名兼容仍接收）。
    """
    lines: list[str] = ["[图像识别:"]
    dev = str(device_id or "").strip()
    face_rows: list[dict[str, Any]] = []
    if dev:
        face_rows = _sorted_faces_for_message(dev)
        if face_rows:
            for row in face_rows:
                lines.append(f"   {_format_face_line(row)}")
        else:
            lines.append("   (未检测到人脸)")
    else:
        lines.append("   (无设备)")
    lines.append("]")
    body = (user_text or "").strip()
    if not body:
        body = "[未说话]"
    elif dev and not face_rows:
        lines.append("")
        lines.append(
            "（图像识别未检测到人脸；用户已说话时须正常回答用户正文，"
            "勿编造「正在看你」或仅回复「看不到人」而忽略用户问题。）"
        )
    lines.append("")
    if from_asr:
        # 语音轮：正文行括号标注「ASR 转写来源 + 声纹说话人身份」（与实验台同源）
        note = format_voice_speaker_note(dev) if dev else None
        note_text = "语音转写" + (f"，{note}" if note else "")
        # 身份没认出来时补一句限定：模型会把「声纹判定：陌生人 / 判定中」读成
        # 「不是在跟我说话」而整轮静默（实测占静默轮次的近三成）。是否该回应已由
        # 服务端注意力门控（asr_attention_gate）决定，这里只提醒它别再自行否决。
        # 限定语单独成行（同上方「未检测到人脸」的写法），不改「用户正文（…）」
        # 那一行的格式——那是被测试钉住的契约，也是与实验台展示同源的格式。
        if note and ("陌生人" in note or "判定中" in note or "不可用" in note):
            lines.append("")
            lines.append(
                "（声纹身份未确定只影响怎么称呼——用「你」即可；"
                "是否该回应已由服务端判定，本轮照常回答用户正文。）"
            )
        lines.append(f"用户正文（{note_text}）：{body}")
    else:
        lines.append(f"用户正文: {body}")
    return "\n".join(lines)


def beijing_time_str() -> str:
    if ZoneInfo is not None:
        now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    else:
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return now.strftime("%Y-%m-%d %H:%M:%S") + " " + weekdays[now.weekday()]


def llm_native_tools_directive(tool_names: list[str]) -> str:
    """原生 function calling 模式下的工具短指令（替代文本广告段）。"""
    names = "、".join(str(n) for n in tool_names)
    return (
        f"可用工具（原生 function calling）：本轮提供 {names}。"
        "需要时请直接发起函数调用；不需要或已完成时，直接输出要说给主人听的"
        "口语纯文本（不要 JSON、不要字段名、不要动作/表情字段）。"
        "不要自行把工具调用写进正文。"
    )


def build_llm_system_prompt(base_prompt: str, *, device_id: str | None = None, native_tool_names: list[str] | None = None, include_clock: bool = True) -> str:
    """组装对话模型的最终 system prompt：基础人设 + 长期记忆 + 原生工具 directive
    + 剧情任务情境 + 用户社交情境 + 当前时间（文末）。

    动作/表情白名单不再注入对话模型——它们由独立的「决策模型」负责
    （见 ``build_action_decision_prompt``）。

    工具一律走原生 function calling（契约在 API ``tools`` 参数/response 里），
    不再向模型广告「tools 数组文本」写法；legacy 文本工具段已整体移除。

    ``native_tool_names``：
    - ``None`` → 按设备解析当前启用工具名单并给一行 directive（默认/预览语义）；
    - 空列表 ``[]`` → 本轮 API 未提供 tools（文本收口/无工具轮），prompt 完全不再
      提及任何工具，防止模型在收口轮重复输出函数调用文本；
    - 非空列表 → 按名单给 directive。
    """
    base = str(base_prompt or "")
    if native_tool_names is None:
        from deskbot_server.service.llm_tools_schema import build_native_tool_schemas

        native_tool_names = [s["function"]["name"] for s in build_native_tool_schemas(device_id=device_id)]
    fx = llm_memory_prompt_appendix(device_id)
    if fx:
        base += "\n\n" + fx
    if native_tool_names:
        base += "\n\n" + llm_native_tools_directive(native_tool_names)
    qx = llm_quest_tasks_prompt_appendix(device_id=device_id)
    if qx:
        base += "\n\n" + qx
    # 剧情工具契约由 API tools schema 承载（complete_task 动态注入任务 id/类型
    # description），不再注入文本契约段。
    # 用户社交情境（识别到已知用户时才有内容；无人识别 → 空串保持旧行为）
    sx = llm_user_social_context_prompt_appendix(device_id=device_id)
    if sx:
        base += "\n\n" + sx
    tx = llm_social_active_tasks_prompt_appendix(device_id=device_id)
    if tx:
        base += "\n\n" + tx
    if not include_clock:
        return base
    # 当前时间放全文末尾（其它规则段在前，时间戳始终最新可见）；
    # 识别到认识的人时紧随其后拼「上一次对话时间」行（build 时读取服务端打点）
    time_tail = f"当前时间是: {beijing_time_str()}（北京时间，东八区）"
    lt = llm_user_last_talk_prompt_appendix(device_id=device_id)
    if lt:
        time_tail += "\n" + lt
    base += "\n\n" + time_tail
    return base


_LLM_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)


def _parse_need_reply_value(v: Any) -> bool:
    """JSON 里 ``need_reply`` 的宽松解析；缺省由调用方视为需要回复。"""
    if v is False or v == 0:
        return False
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("false", "0", "no", "否", "不需要", "不用", "none"):
            return False
        if s in ("true", "1", "yes", "是", "需要"):
            return True
        return bool(s)
    return bool(v)


def _parsed_json_need_reply(parsed: dict) -> bool:
    if "need_reply" not in parsed:
        return True
    return _parse_need_reply_value(parsed.get("need_reply"))


def _parse_bool_flag(v: Any, default: bool = True) -> bool:
    """JSON 里 ``valid`` / ``respond`` 的宽松解析；缺省/无法识别 → ``default``。"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("false", "0", "no", "否", "不需要", "不用", "none"):
        return False
    if s in ("true", "1", "yes", "是", "需要"):
        return True
    return default


def parse_servo_plan_item(obj: Any) -> dict[str, Any] | None:
    """解析 ``servo`` 数组单条：延时 ``hold_ms`` / ``hold``+``ms``，或标准 ``xm``…``ms``。"""
    if not isinstance(obj, dict):
        return None
    if obj.get("hold") is True or obj.get("hold") == 1:
        try:
            h = int(obj.get("ms", obj.get("hold_ms", 0)))
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            return {"_hold_ms": min(h, 30_000)}
    if "hold_ms" in obj:
        try:
            h = int(obj["hold_ms"])
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            return {"_hold_ms": min(h, 30_000)}
    return normalize_pb_servo_dict(obj)


def normalize_pb_servo_dict(obj: Any) -> dict[str, int] | None:
    """校验并归一化单条 pb 舵机指令（``xm``/``ym``/``x``/``y``/``ms``），非法则 ``None``。"""
    if not isinstance(obj, dict):
        return None
    try:
        xm = int(obj.get("xm", 0))
        ym = int(obj.get("ym", 0))
        x = int(obj.get("x", 0))
        y = int(obj.get("y", 0))
        ms = int(obj.get("ms", 0))
    except (TypeError, ValueError):
        return None
    if xm not in (0, 1, 2) or ym not in (0, 1, 2):
        return None
    if ms <= 0:
        return None
    return {"xm": xm, "ym": ym, "x": x, "y": y, "ms": ms}


def coerce_pb_v2_downlink_payload(payload: Any) -> dict[str, Any]:
    """pb v2 下行：``servo`` 须为数组；兼容误写成单对象的历史调用。"""
    if not isinstance(payload, dict):
        return {}
    servo = payload.get("servo")
    if not isinstance(servo, dict):
        return payload
    norm = normalize_pb_servo_dict(servo)
    out = dict(payload)
    if norm:
        out["servo"] = [norm]
    else:
        out.pop("servo", None)
    return out


def _parse_llm_move_items(raw: Any) -> list[Any]:
    """moves 条目规范化：字符串 = 预设 id（新协议，默认时长）；
    dict = 兼容旧 ``{move, ms}``（有合法 ms 保留对象，无 ms 按新协议转字符串）。
    模型把数组误写成单个字符串/对象时也按单元素处理。"""
    out: list[Any] = []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if isinstance(item, str):
            move_id = item.strip()
            if move_id:
                out.append(move_id)
            continue
        if not isinstance(item, dict):
            continue
        move_id = str(item.get("move") or "").strip()
        try:
            ms = int(item.get("ms", 0))
        except (TypeError, ValueError):
            ms = 0
        if not move_id:
            continue
        if ms > 0:
            out.append({"move": move_id, "ms": ms})
        else:
            out.append(move_id)
    return out


def _parse_llm_anim_items(raw: Any) -> list[Any]:
    """anims 条目规范化：字符串 = 场景 name（新协议，默认时长）；
    dict = 兼容旧 ``{anim, ms}``（有合法 ms 保留对象，无 ms 按新协议转字符串）。
    模型把数组误写成单个字符串/对象时也按单元素处理。"""
    out: list[Any] = []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if isinstance(item, str):
            anim_name = item.strip()
            if anim_name:
                out.append(anim_name)
            continue
        if not isinstance(item, dict):
            continue
        anim_name = str(item.get("anim") or "").strip()
        try:
            ms = int(item.get("ms", 0))
        except (TypeError, ValueError):
            ms = 0
        if not anim_name:
            continue
        if ms > 0:
            out.append({"anim": anim_name, "ms": ms})
        else:
            out.append(anim_name)
    return out


# 「规则检查/内部思考」而非说给主人听的话。纯文本协议下模型偶尔会把
# system prompt 里的规则复述成一段说明（含（…）或「系统已识别/按规则」等词），
# 必须识别出来按静默处理，否则会被 TTS 直接朗读并写进历史，下一轮形成自我强化。
_META_REASONING_MARKERS = (
    "系统已识别",
    "系统已确认",
    "系统提示",
    "按规则",
    "规则判断",
    "规则中",
    "属于规则",
    "不重复问候",
    "无需重复问候",
    "无需重复",
    "本轮不输出",
    "本轮保持安静",
    "本轮无需开口",
    "不输出任何文字",
    "不主动输出",
    "直接留空",
    "need_reply",
    "tts 留空",
    "输出任何文字",
    "判断过程",
    "今日该时段已问候过",
)
# 这些词几乎只出现在内部说明里，单独命中即可判定；其余标记要求命中 ≥2 个，
# 避免「按规则来说，今天该休息了」这类正常口语被误杀。
_META_STRONG_MARKERS = (
    "系统已识别",
    "系统已确认",
    "need_reply",
    "本轮不输出",
    "本轮保持安静",
    "本轮无需开口",
    "不输出任何文字",
    "不重复问候",
    "无需重复问候",
)


def looks_like_meta_reasoning(text: str) -> bool:
    """判断一段输出是不是「内部思考/规则说明」，而不是对主人说的话。

    保守判断：整段被全角/半角括号包裹（口语不会整段包在括号里），
    或命中足够具体的规则/系统措辞（强标记单词命中，普通标记需 ≥2 个）。
    """
    t = str(text or "").strip()
    if not t:
        return False
    if (t.startswith("（") and t.endswith("）")) or (t.startswith("(") and t.endswith(")")):
        return True
    if any(marker in t for marker in _META_STRONG_MARKERS):
        return True
    return sum(1 for marker in _META_REASONING_MARKERS if marker in t) >= 2


def strip_llm_preamble(raw: str) -> str:
    """从模型原始输出里取出「该说给主人听的那句话」。

    模型经常先写一段中文推理再给 JSON envelope。若直接把原始输出当作口语，
    会话历史会被自己的思维链污染（模型下一轮读到推理样例 → 自我强化沉默），
    也可能被 TTS 念出来。这里统一清洗：

    - 含 JSON envelope → 取其 ``tts``（兼容旧名 ``reply``），为空则返回空串
    - 纯文本 → 取最后一段**非内部思考/规则说明**的非空行
    - 无有效内容 → 空串

    供会话归档、历史注入与纯文本兜底包装共用。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    parsed = parse_llm_reply(text)
    if parsed.get("json_ok"):
        # tts 为空说明模型本轮选择静默（need_reply=false），不要拿整段 JSON 充数
        spoken = str(parsed.get("reply") or "").strip()
        return "" if looks_like_meta_reasoning(spoken) else spoken
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped and not looks_like_meta_reasoning(stripped):
            return stripped
    return ""


def parse_llm_reply(raw: str) -> dict:
    """把 LLM 输出尝试解析为约定 JSON。

    格式 ``{"need_reply", "tts", "dialogue_act?", "volume?", "gesture", "expression"}``；
    兼容旧名 ``moves`` / ``anims`` 与旧版 ``servo`` / ``scenes`` / ``reply`` 字段
    （新名存在时优先）。解析结果统一归一为内部键 ``moves`` / ``anims``。
    工具调用只走原生 function calling（API tools 参数），envelope 内不再有 ``tools`` 键。

    失败时把整段文本当作 ``reply`` 返回，**不抛异常**。
    """
    text = (raw or "").strip()
    parsed: dict | None = None

    candidates = []
    if text:
        candidates.append(text)
        m = _LLM_JSON_FENCE_RE.search(text)
        if m:
            candidates.append(m.group(1))
        try:
            i = text.index("{")
            j = text.rindex("}")
            if j > i:
                candidates.append(text[i : j + 1])
        except ValueError:
            pass

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            parsed = obj
            break

    servo_out: list[Any] = []
    moves_out: list[dict[str, Any]] = []
    anims_out: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        raw_servo = parsed.get("servo")
        if isinstance(raw_servo, dict):
            raw_servo = [raw_servo]
        if isinstance(raw_servo, (list, tuple)):
            for item in raw_servo:
                ent = parse_servo_plan_item(item)
                if ent:
                    servo_out.append(ent)
        # LLM 侧字段名：gesture / expression；兼容旧名 moves / anims（同时出现时旧名忽略）
        moves_out = _parse_llm_move_items(parsed.get("gesture", parsed.get("moves")))
        anims_out = _parse_llm_anim_items(parsed.get("expression", parsed.get("anims")))
        reply_tts = parsed.get("tts")
        reply_legacy = parsed.get("reply")
        reply: str
        if isinstance(reply_tts, str) and reply_tts.strip():
            reply = reply_tts.strip()
        elif isinstance(reply_legacy, str) and reply_legacy.strip():
            reply = reply_legacy.strip()
        else:
            # 合法 JSON 但 tts/reply 均为空：勿把整段 JSON 当朗读文本
            reply = ""
        scenes_out: list[str] = []
        raw_scenes = parsed.get("scenes")
        if isinstance(raw_scenes, str):
            raw_scenes = [raw_scenes]
        if isinstance(raw_scenes, (list, tuple)):
            for x in raw_scenes:
                if isinstance(x, str):
                    v = x.strip()
                    if v:
                        scenes_out.append(v)
        vol = parse_pb_volume(parsed.get("volume"))
        dialogue_act = str(parsed.get("dialogue_act") or "").strip().lower()
        if dialogue_act not in {"answer", "empathize", "follow_up", "playful", "callback", "silent"}:
            dialogue_act = "silent" if not _parsed_json_need_reply(parsed) else "answer"
        return {
            "reply": reply,
            "moves": moves_out,
            "anims": anims_out,
            "scenes": scenes_out,
            "servo": servo_out,
            "volume": vol,
            "need_reply": _parsed_json_need_reply(parsed),
            # 决策模型前置门控字段；普通对话 envelope 没有这两键 → 默认放行/需要响应
            "valid": _parse_bool_flag(parsed.get("valid"), True),
            "respond": _parse_bool_flag(parsed.get("respond"), True),
            "dialogue_act": dialogue_act,
            "json_ok": True,
            "raw": text,
        }

    return {
        "reply": text,
        "moves": [],
        "anims": [],
        "scenes": [],
        "servo": [],
        "volume": None,
        "valid": True,
        "respond": True,
        "need_reply": True,
        "dialogue_act": "answer",
        "json_ok": False,
        "raw": text,
    }


__all__ = [
    "beijing_time_str",
    "build_action_decision_prompt",
    "build_action_decision_user_message",
    "build_llm_system_prompt",
    "estimate_text_tokens",
    "build_llm_user_message",
    "list_llm_expression_names",
    "list_llm_expression_titles",
    "list_llm_move_ids",
    "looks_like_meta_reasoning",
    "llm_memory_prompt_appendix",
    "llm_pb_anims_prompt_appendix",
    "llm_pb_moves_prompt_appendix",
    "llm_pb_plan_prompt_appendix",
    "llm_pb_scenes_prompt_appendix",
    "llm_quest_tasks_prompt_appendix",
    "llm_social_active_tasks_prompt_appendix",
    "llm_user_last_talk_prompt_appendix",
    "llm_user_social_context_prompt_appendix",
    "parse_llm_reply",
    "parse_servo_plan_item",
    "coerce_pb_v2_downlink_payload",
    "normalize_pb_servo_dict",
    "recognized_known_users",
]
