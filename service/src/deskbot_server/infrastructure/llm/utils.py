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

from deskbot_server.constants import SERVO_CFG_FILE
from deskbot_server.dao.face_design_store import resolve_face_design_path
from deskbot_server.dao.face_expr_scenes_store import load_face_expr_scenes_file
from deskbot_server.dao.servo_config_store import load_servo_cfg_file
from deskbot_server.pb.servo_pcm import parse_pb_volume
from deskbot_server.utils.device_data import resolve_json_path

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


def llm_pb_moves_prompt_appendix(*, device_id: str | None = None) -> str:
    """供 system prompt 追加：合法 ``gesture`` 动作 id 白名单（LLM 侧字段名 gesture）。"""

    def _build() -> str:
        try:
            cfg = load_servo_cfg_file(device_id=device_id)
        except (OSError, ValueError):
            return ""
        if not cfg:
            return ""
        ids: list[str] = []
        for preset in cfg.get("presets") or []:
            if not isinstance(preset, dict):
                continue
            pid = str(preset.get("id") or "").strip()
            if pid and pid not in ids:
                ids.append(pid)
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
    cache_key = f"moves:{device_id or ''}"
    return _cached_appendix(cache_key, mtime_path, _build)


def llm_pb_anims_prompt_appendix(*, device_id: str | None = None) -> str:
    """供 system prompt 追加：合法 ``expression`` 表情名白名单（LLM 侧字段名 expression）。"""

    def _build() -> str:
        rows = _face_expr_scene_entries(device_id=device_id)
        if not rows:
            return ""
        names: list[str] = []
        for row in rows:
            name = str(row.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
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


def llm_memory_prompt_appendix(device_id: str | None = None) -> str:
    """长期记忆列表，注入 system prompt。"""
    from deskbot_server.dao.device_memory_mapper import list_memory_for_device

    rows = list_memory_for_device(device_id, limit=30)
    if not rows:
        return "长期记忆：暂无。"
    lines: list[str] = []
    for e in rows:
        eid = str(e.get("id") or "")
        text = str(e.get("text") or "").strip()
        if text:
            lines.append(f"  - [{eid}] {text}")
    return "长期记忆（可用 memory_delete 删除，id 见方括号）：\n" + "\n".join(lines)


def llm_tools_prompt_appendix() -> str:
    """LLM 可返回的 tools 数组说明。"""
    return (
        "可用工具（可选 ``tools`` 数组；需要工具时 ``tools`` 非空、``tts`` 可留空，"
        "服务端执行后会再次调用你；最终回复时 ``tools`` 写 [] 并填写 ``tts``。"
        "用户已说话时优先在 ``tts`` 里正常回答；不要只返回 tools 而省略完整 JSON 对象）：\n"
        '  - register_face: {"tool":"register_face","name":"姓名","face_id":1} 把当前画面 face_id 的人脸注册/更新到档案。'
        "face_id 见每轮 user 消息「图像识别」；仅一张脸时可省略；多人须指定 face_id 或先向用户澄清。\n"
        '  - register_voiceprint: {"tool":"register_voiceprint","name":"姓名"} 记住刚说话的人的声音（注册样本来自最近一次对话语音）。'
        "用户说「记住我的声音/我叫xx」时必须调用；若返回样本不足的提示，请引导用户先对机器人说一句完整的话再重新注册。\n"
        '  - memory_add: {"tool":"memory_add","text":"要记住的内容"} 新增长期记忆；'
        'memory_delete: {"tool":"memory_delete","id":"记忆id"} 删除（id 见 system 中长期记忆方括号）。\n'
        "  - schedule_task: cron 定时任务增删改查（北京时间东八区）。**用户要求定时/提醒时必须调用，禁止仅口头答应。**\n"
        "    创建：{\"tool\":\"schedule_task\",\"action\":\"create\",\"task\":\"提醒喝水\","
        "\"cron\":\"0 8 * * *\",\"task_kind\":\"recurring\"}\n"
        "    · cron 为「分 时 日 月 周」五段：明天9点 → \"0 9 <明日日期> <明日月份> *\"；每天8点 → \"0 8 * * *\"\n"
        "    · task_kind: once 一次性 / recurring 周期性；相对延迟用 delay_minutes 数字（「两分钟」→ 2），与 cron 二选一\n"
        "    · 查询：{\"action\":\"list\"}；读取：{\"action\":\"get\",\"id\":\"…\"}；"
        "修改：{\"action\":\"update\",\"id\":\"…\",\"cron\":\"…\",\"task\":\"…\",\"enabled\":true}；"
        "删除：{\"action\":\"delete\",\"id\":\"…\"}。创建无需 session_id（自动绑定）。\n"
        "    成功示例：第一轮 {\"tools\":[...创建...],\"tts\":\"\"} → 第二轮 {\"tools\":[],\"tts\":\"好，两分钟后提醒你喝水。\"}\n"
        '  - webfetch: {"tool":"webfetch","url":"https://…"} 抓取网页文本；'
        'websearch: {"tool":"websearch","query":"搜索词"} 网络搜索摘要\n'
        '  - read: {"tool":"read","path":"notes.txt"} / write: {"tool":"write","path":"notes.txt","content":"…"} '
        "读写本设备 tmp 目录（路径仅限 data/device/{device_id}/tmp/，禁止 .. 与绝对路径）。\n"
        "  - session: 查询当前与最近对话 session（10 分钟无对话自动开新 session）\n"
        '    当前：{"action":"current"}；列表：{"action":"list","limit":10}；详情：{"action":"get","session_id":"…"}（省略 id 读当前）\n'
        '  - update_user_info: {"tool":"update_user_info","user_name":"姓名","chat_message":"新披露的事实短句"} '
        "用户当面告知姓名/性别/年龄/家庭/住址/爱好等个人信息时调用，按人归档；随口闲聊不要存。\n"
        '  - update_daily_task: {"tool":"update_daily_task","user_name":"姓名","message":"我跟小明说了早上好"} '
        "主动问候/关心/表达思念开口前先记账到当日任务记录；message 用第一人称一句话，不用自带时间，服务端自动补。\n"
    )


def llm_quest_tasks_prompt_appendix(*, device_id: str | None = None) -> str:
    """「当前剧情任务」附录：绑定剧本（devices.quest_id）的 running 任务列表。

    未绑定 / 剧本缺失 / 无进行中任务 → 空串（不注入）。
    只列优先级最高的 3 个进行中任务，不展示分数进度（避免模型以分数为目标）。
    """
    if not device_id:
        return ""
    from deskbot_server.service.quest_service import QuestService

    tasks = QuestService().get_current_tasks(str(device_id))
    if not tasks:
        return ""
    lines: list[str] = ["当前剧情任务（进行中，最多列 3 个）："]
    for t in tasks[:3]:
        lines.append(f"  - [{t['task_id']}] {t['title']}")
        lines.append(f"    目标：{t['goal']}")
        lines.append(f"    策略：{t['strategy']}")
        lines.append(f"    成功条件：{t['success_condition']}｜失败条件：{t['failure_condition']}")
    return "\n".join(lines)


def llm_quest_tools_prompt_appendix(*, device_id: str | None = None) -> str:
    """「剧情任务工具」附录：get_tool_calls 契约格式化。

    无可用任务 id（未绑定 / 剧本无 running）→ 空串：
    不向 LLM 广告不可用工具，避免诱导编造 task_id。
    """
    if not device_id:
        return ""
    from deskbot_server.service.quest_service import QuestService

    calls = QuestService().get_tool_calls(str(device_id))
    if not calls or not any(c.get("available_task_ids") for c in calls):
        return ""
    lines = [f"剧情任务工具（可用任务 id：{', '.join(calls[0]['available_task_ids'])}）："]
    for c in calls:
        lines.append(f"  - {c['name']}：{c['description']}")
        for pname, pdesc in (c.get("parameters") or {}).items():
            lines.append(f"      {pname}：{pdesc}")
    return "\n".join(lines)


def llm_static_context_prompt_appendix(device_id: str | None = None) -> str:
    """长期记忆 + 工具说明（图像/声音识别见每轮 user 消息）。"""
    parts = [llm_memory_prompt_appendix(device_id), llm_tools_prompt_appendix()]
    return "\n\n".join(p for p in parts if p)


# ───────────────────── 用户社交情境（识别到已知的人时按人注入） ─────────────────────

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
        from deskbot_server.service.application.voice_snapshot_cache import (
            STATE_FOUND,
            get_voice_snapshot,
        )

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
    from deskbot_server.dao.user_social_store import (
        read_done_list_block,
        read_user_info_block,
    )

    blocks: list[str] = []
    used = 0
    for name in names:
        info = read_user_info_block(str(device_id), name)
        done = read_done_list_block(str(device_id), name)
        info_txt = info if info else "（暂无已记录的自我介绍，不要编造用户资料）"
        done_txt = done if done else "（今日暂无主动互动记录）"
        block = (
            f"{name} 的资料（update_user_info 归档，时间旧→新）：\n  {info_txt}\n"
            f"{name} 今日已完成的主动问候/关心（update_daily_task 记账）：\n  {done_txt}"
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
    明确静默退出语义（need_reply=false 不硬聊）。
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
        "- 距与该用户上一次对话时间超过 5 分钟再次见到时，可表达思念/又见到你的亲近之情；"
        "开口前先 update_daily_task 记账，同一意图 30 分钟内不重复。\n"
        "- 语气自然口语化、像真人聊天，禁止「已问候/已汇报」式的汇报腔；"
        "用户正在提问、剧情任务轮或定时提醒轮进行中时，以当前事务为先，不要插话问候。\n"
        "- 若此刻没有任何需要主动表达的情形（如刚问候过、刚聊过不久且不在饭点），"
        "本轮无需开口：need_reply=false、tts 留空，只输出 JSON。"
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


def _nose_xy(face: dict[str, Any]) -> tuple[float, float, int, int] | None:
    w = int(face.get("image_w") or 0) or 320
    h = int(face.get("image_h") or 0) or 240
    for p in face.get("landmarks") or []:
        if not isinstance(p, dict) or p.get("name") != "nose":
            continue
        try:
            return float(p["x"]), float(p["y"]), w, h
        except (TypeError, ValueError, KeyError):
            continue
    return None


def _format_face_line(face: dict[str, Any]) -> str:
    fid = face.get("face_id")
    parts: list[str] = [f"faceid={fid if fid is not None else '?'}"]
    face_score = face.get("face_score")
    if face_score is not None:
        try:
            parts.append(f"人脸置信度={float(face_score):.2f}")
        except (TypeError, ValueError):
            pass
    name = str(face.get("person_name") or "").strip() or "未知"
    parts.append(f"name={name}")
    identity_score = face.get("identity_score")
    if identity_score is not None:
        try:
            parts.append(f"人物识别置信度={float(identity_score):.2f}")
        except (TypeError, ValueError):
            pass
    nose = _nose_xy(face)
    if nose is not None:
        nx, ny = int(round(nose[0])), int(round(nose[1]))
        parts.append(f"脸中心位置=({nx},{ny})")
    else:
        parts.append("脸中心位置=未知")
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


def _format_voice_line(name: str, score: float | None) -> str:
    parts: list[str] = [f"name={name}"]
    if score is not None:
        try:
            parts.append(f"说话人识别置信度={float(score):.2f}")
        except (TypeError, ValueError):
            pass
    return ", ".join(parts)


def format_sight_voice_text(device_id: str | None) -> str | None:
    """当前设备「最近一次 VAD」声纹判定的调试文本（与喂给 LLM 的识别行一致）。

    found → ``声音识别:\\n  name=…, 说话人识别置信度=…``；unknown →
    ``声音识别:\\n  (未识别出已知说话人)``；识别中/引擎降级/未开启/无设备 → None。
    供实验台用户气泡展示本轮「听到谁在说话」的识别结果。
    """
    dev = str(device_id or "").strip()
    if not dev:
        return None
    from deskbot_server.service.application.voice_snapshot_cache import (
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
        return "声音识别:\n  " + _format_voice_line(name, snap.get("score"))
    if state == STATE_UNKNOWN:
        return "声音识别:\n  (未识别出已知说话人)"
    return None  # identifying / degraded：本轮不给结论


def build_llm_user_message(user_text: str, *, device_id: str | None = None, device_context: str | None = None) -> str:
    """按约定格式组装 LLM ``user`` 消息正文（图像识别 + 声音识别 + 用户正文）。

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
    voice_text = format_sight_voice_text(dev) if dev else None
    if voice_text:
        head, _, rest = voice_text.partition("\n")
        lines.append(head)
        if rest:
            lines.append("   " + rest.lstrip())
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
        "需要时请直接发起函数调用；不需要或已完成时，仍按上方模板输出最终 JSON"
        "（need_reply/tts/gesture/expression，tools 写 []）。不要再把工具写成 tools 数组文本。"
    )


def build_llm_system_prompt(base_prompt: str, *, device_id: str | None = None, native_tool_names: list[str] | None = None) -> str:
    """组装最终 system prompt：基础人设 + 动作/表情 + 长期记忆 + 原生工具 directive
    + 剧情任务情境 + 用户社交情境 + 当前时间（文末）。

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
        from deskbot_server.infrastructure.llm.tool_schema import build_native_tool_schemas

        native_tool_names = [s["function"]["name"] for s in build_native_tool_schemas(device_id=device_id)]
    px = llm_pb_scenes_prompt_appendix(device_id=device_id)
    if px:
        base += "\n" + px
    fx = llm_memory_prompt_appendix(device_id)
    if fx:
        base += "\n\n" + fx
    if native_tool_names:
        base += "\n\n" + llm_native_tools_directive(native_tool_names)
    qx = llm_quest_tasks_prompt_appendix(device_id=device_id)
    if qx:
        base += "\n\n" + qx
    # 剧情工具契约由 API tools schema 承载（update_task_result 等动态 id 注入
    # description），不再注入文本契约段。
    # 用户社交情境（识别到已知用户时才有内容；无人识别 → 空串保持旧行为）
    sx = llm_user_social_context_prompt_appendix(device_id=device_id)
    if sx:
        base += "\n\n" + sx
    tx = llm_social_active_tasks_prompt_appendix(device_id=device_id)
    if tx:
        base += "\n\n" + tx
    # 当前时间放全文末尾（其它规则段在前，时间戳始终最新可见）；
    # 识别到认识的人时紧随其后拼「上一次对话时间」行（build 时读取服务端打点）
    time_tail = f"当前时间是: {beijing_time_str()}（北京时间，东八区）"
    lt = llm_user_last_talk_prompt_appendix(device_id=device_id)
    if lt:
        time_tail += "\n" + lt
    base += "\n\n" + time_tail
    return base


def llm_face_context_prompt_appendix(device_id: str | None = None) -> str:
    """兼容旧调用名；人脸已移至 user 消息，此处仅记忆与工具。"""
    return llm_static_context_prompt_appendix(device_id)


def llm_recognized_faces_prompt_appendix(device_id: str | None = None) -> str:
    """兼容旧调用名。"""
    return llm_static_context_prompt_appendix(device_id)


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
    dict = 兼容旧 ``{move, ms}``（有合法 ms 保留对象，无 ms 按新协议转字符串）。"""
    out: list[Any] = []
    if isinstance(raw, dict):
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
    dict = 兼容旧 ``{anim, ms}``（有合法 ms 保留对象，无 ms 按新协议转字符串）。"""
    out: list[Any] = []
    if isinstance(raw, dict):
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


def _parse_llm_tool_items(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        # OpenAI 嵌套 function-call 形状：{"type":"function","function":{"name","arguments"}}。
        # 部分模型在文本 JSON 通道里也会输出这种形状——解包并把 arguments 并入平铺键，
        # 与执行器（execute_llm_tools 读平铺键）对齐；arguments 为 JSON 字符串时解析。
        fn = item.get("function")
        if isinstance(fn, dict):
            row = dict(item)
            row.pop("function", None)
            row.pop("type", None)
            row.pop("id", None)
            tool = str(fn.get("name") or "").strip()
            if not tool:
                continue
            row["tool"] = tool
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    args = None
            if isinstance(args, dict):
                for k, v in args.items():
                    row.setdefault(str(k), v)
            out.append(row)
            continue
        tool = str(item.get("tool") or item.get("name") or "").strip()
        if not tool:
            continue
        row = dict(item)
        row["tool"] = tool
        out.append(row)
    return out


def _coerce_llm_reply_object(obj: Any) -> dict[str, Any] | None:
    """把 LLM 误输出的「仅 tools 数组 / 单条 tool 对象」规范为完整 JSON 对象。"""
    if isinstance(obj, list):
        tools = _parse_llm_tool_items(obj)
        if tools:
            return {"tools": tools, "tts": "", "need_reply": True}
        return None
    if not isinstance(obj, dict):
        return None
    if (obj.get("tool") or obj.get("name")) and "tools" not in obj:
        tools = _parse_llm_tool_items([obj])
        if tools:
            out: dict[str, Any] = {"tools": tools}
            for key in (
                "need_reply",
                "tts",
                "reply",
                "moves",
                "anims",
                "gesture",
                "expression",
                "volume",
                "scenes",
                "servo",
            ):
                if key in obj:
                    out[key] = obj[key]
            out.setdefault("tts", "")
            return out
    return obj


def parse_llm_reply(raw: str) -> dict:
    """把 LLM 输出尝试解析为约定 JSON。

    格式 ``{"need_reply", "tts", "volume?", "gesture", "expression", "tools": [...]}``；
    兼容旧名 ``moves`` / ``anims`` 与旧版 ``servo`` / ``scenes`` / ``reply`` 字段
    （新名存在时优先）。解析结果统一归一为内部键 ``moves`` / ``anims``。

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

        try:
            i = text.index("[")
            j = text.rindex("]")
            if j > i:
                candidates.append(text[i : j + 1])
        except ValueError:
            pass

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (TypeError, ValueError):
            continue
        coerced = _coerce_llm_reply_object(obj)
        if isinstance(coerced, dict):
            parsed = coerced
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
        tools_out = _parse_llm_tool_items(parsed.get("tools"))
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
        return {
            "reply": reply,
            "moves": moves_out,
            "anims": anims_out,
            "tools": tools_out,
            "scenes": scenes_out,
            "servo": servo_out,
            "volume": vol,
            "need_reply": _parsed_json_need_reply(parsed),
            "json_ok": True,
            "raw": text,
        }

    return {
        "reply": text,
        "moves": [],
        "anims": [],
        "tools": [],
        "scenes": [],
        "servo": [],
        "volume": None,
        "need_reply": True,
        "json_ok": False,
        "raw": text,
    }


__all__ = [
    "beijing_time_str",
    "build_llm_system_prompt",
    "estimate_text_tokens",
    "build_llm_user_message",
    "llm_face_context_prompt_appendix",
    "llm_memory_prompt_appendix",
    "llm_pb_anims_prompt_appendix",
    "llm_pb_moves_prompt_appendix",
    "llm_pb_plan_prompt_appendix",
    "llm_pb_scenes_prompt_appendix",
    "llm_quest_tasks_prompt_appendix",
    "llm_quest_tools_prompt_appendix",
    "llm_recognized_faces_prompt_appendix",
    "llm_social_active_tasks_prompt_appendix",
    "llm_static_context_prompt_appendix",
    "llm_tools_prompt_appendix",
    "llm_user_last_talk_prompt_appendix",
    "llm_user_social_context_prompt_appendix",
    "parse_llm_reply",
    "parse_servo_plan_item",
    "coerce_pb_v2_downlink_payload",
    "normalize_pb_servo_dict",
    "recognized_known_users",
]
