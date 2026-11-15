from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from deskbot_server.dao import device_mapper
from deskbot_server.dao.device_mapper import get_auto_reply
from deskbot_server.model.chat import ChatTurnResult, LlmTurnResult
from deskbot_server.pb.scenes import _pb_scene_entry_by_name, _prepare_pb_scene_chain_frames
from deskbot_server.pb.shapes import PB_ACTION_APPEND, PB_LEVEL_DEBUG, PB_LEVEL_TASK
from deskbot_server.pb.wire import build_pb_wire_pairs
from deskbot_server.ports.downlink import DownlinkPort, PipelineEventsPort
from deskbot_server.service.application.capability_labels import asr_model_label, llm_model_label, tts_model_label
from deskbot_server.service.application.convo_audio_store import ConvoAudioStore
from deskbot_server.service.application.face_snapshot_cache import face_snapshot_detect_ms
from deskbot_server.service.application.interaction_arbiter import InteractionKind, interaction_arbiter
from deskbot_server.service.application.llm_error_fallback import (
    build_llm_error_fallback_plan,
    start_llm_error_motion_feedback,
    stop_llm_error_motion_feedback,
)
from deskbot_server.service.application.llm_tool_runner import execute_llm_tools
from deskbot_server.service.application.tool_interim_tts import resolve_interim_tts
from deskbot_server.service.application.voice_snapshot_cache import get_voice_snapshot
from deskbot_server.service.llm_context import (
    build_llm_user_message,
    format_voice_speaker_note,
    looks_like_meta_reasoning,
    parse_llm_reply,
    recognized_known_users,
    strip_llm_preamble,
)
from deskbot_server.utils.text_split import split_tts_by_punctuation, split_tts_for_latency
from deskbot_server.utils.util import _ms_between

if TYPE_CHECKING:
    from deskbot_server.service.application.chat_service import ChatService
    from deskbot_server.service.device_ws_service import DeviceWsService

logger = logging.getLogger("deskbot-server")

_SCHEDULED_TASK_PREFIX = "[系统定时任务]"
_QUEST_PROACTIVE_PREFIX = "[系统剧情推进]"
# 社交/剧情主动轮：不进 _SYSTEM_INITIATED_PREFIXES —— 不强制开口，
# LLM 判定此刻无话可说时不输出任何文字（静默退出，区别于定时轮必须口播）
# （剧情任务重构后日常/长期任务永续 running，是否开口须由模型结合今日记录判断）
_SOCIAL_PROACTIVE_PREFIX = "[系统主动问候]"
_SYSTEM_INITIATED_PREFIXES = (_SCHEDULED_TASK_PREFIX, _QUEST_PROACTIVE_PREFIX)
_ALL_SYSTEM_PREFIXES = _SYSTEM_INITIATED_PREFIXES + (_SOCIAL_PROACTIVE_PREFIX,)

_EXPLICIT_SILENCE_PATTERNS = (
    "别说话", "不要说话", "不用回答", "不用回复", "安静一下", "保持安静", "你安静", "请安静",
    "闭嘴", "先别聊", "停止对话",
)
_BACKCHANNEL_ONLY = frozenset({"嗯", "哦", "啊", "好", "好的", "行", "知道了", "谢谢", "明白了"})
_RECENT_QUESTION_LOOKBACK = 4


def _user_explicitly_requests_silence(user_text: str) -> bool:
    """用户明确要求停止口播时，允许正常用户轮静默结束。"""
    compact = re.sub(r"[\s，。！？、,.!?]", "", str(user_text or "")).lower()
    return compact in {"安静", "停", "停一下", "等一下", "先停一下", "暂停", "听一下", "别说了"} or any(
        pattern in compact for pattern in _EXPLICIT_SILENCE_PATTERNS
    )


def _is_backchannel_only(user_text: str) -> bool:
    compact = re.sub(r"[\s，。！？、,.!?~～]", "", str(user_text or ""))
    return compact in _BACKCHANNEL_ONLY


def _history_ends_with_assistant_question(history_messages: list[dict[str, str]] | None) -> bool:
    for message in reversed(history_messages or []):
        if message.get("role") != "assistant":
            continue
        raw = str(message.get("content") or "").strip()
        # 兼容清洗前的历史：旧版本可能把整个 JSON envelope 存进 assistant content。
        text = strip_llm_preamble(raw) or raw
        text = re.sub(r"[\s。.!！…]+$", "", text)
        return text.endswith(("?", "？", "吗", "呢", "么"))
    return False


def _question_clauses(text: str) -> list[tuple[str, str]]:
    """返回 ``(原句, 归一化问题)``；只用于短窗口防重复，不做语义推理。"""
    spoken = strip_llm_preamble(str(text or "")) or str(text or "").strip()
    out: list[tuple[str, str]] = []
    for clause in re.findall(r"[^。！？!?]+(?:[。！？!?]+|$)", spoken):
        raw = clause.strip()
        if not raw:
            continue
        bare = re.sub(r"[\s。！？!?]+$", "", raw)
        is_question = raw.endswith(("?", "？")) or bare.endswith(("吗", "呢", "么"))
        if not is_question:
            continue
        normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", bare.lower())
        normalized = re.sub(r"^(?:那|那么|所以|对了|话说|顺便问一下)+", "", normalized)
        normalized = re.sub(r"(?:吗|呢|么|呀|啊|嘛)+$", "", normalized)
        if normalized:
            out.append((raw, normalized))
    return out


def _same_recent_question(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < 4:
        return False
    # 只容忍人称、语气和礼貌用语差异；时间、动作、对象等实义字一旦变化，就按
    # 新问题处理。这样不会把“今天去哪”和“明天去哪”因编辑距离很近而误删。
    non_semantic_chars = frozenset("你我咱们了的呀啊呢吗么嘛请再还想问说能否可以一下告诉")
    matcher = SequenceMatcher(None, left, right)
    changed = "".join(
        left[a0:a1] + right[b0:b1]
        for tag, a0, a1, b0, b1 in matcher.get_opcodes()
        if tag != "equal"
    )
    return bool(changed) and all(char in non_semantic_chars for char in changed)


def _suppress_recent_duplicate_questions(
    reply_text: str, history_messages: list[dict[str, str]] | None
) -> tuple[str, int]:
    """删掉最近四条助手回复中已经问过的近似问题，返回新文本和删除数量。"""
    recent: list[str] = []
    assistant_count = 0
    for message in reversed(history_messages or []):
        if message.get("role") != "assistant":
            continue
        assistant_count += 1
        recent.extend(normalized for _raw, normalized in _question_clauses(message.get("content") or ""))
        if assistant_count >= _RECENT_QUESTION_LOOKBACK:
            break
    kept: list[str] = []
    removed = 0
    for clause in re.findall(r"[^。！？!?]+(?:[。！？!?]+|$)", str(reply_text or "").strip()):
        questions = _question_clauses(clause)
        if questions:
            normalized = questions[0][1]
            if any(_same_recent_question(normalized, old) for old in recent):
                removed += 1
                continue
            # 同一条回复里连续问两遍也属于短时重复。
            recent.append(normalized)
        kept.append(clause.strip())
    if not removed:
        return reply_text, 0
    cleaned = "".join(kept).strip()
    return cleaned or "嗯，我记住了。", removed


def _convo_watching_sync(device_id: str | None) -> bool:
    """实时对话采集门控：是否有后台订阅者在查看该设备的实时对话。

    无人查看时不写媒体留存（采集按需开启）；BusService 为进程内单例，此处同步读
    订阅者表（单线程事件循环内安全），失败按无订阅者处理。
    """
    if not device_id:
        return False
    try:
        from deskbot_server.service.bus_service import BusService

        return BusService().has_subscribers_sync(device_id)
    except Exception:
        return False


class _TtsPrefetch:
    """LLM 流式输出中 ``tts`` 字段闭合后提前启动 TTS 合成（按设备解析 provider）。"""

    def __init__(self, chat: ChatService, *, device_id: str | None = None) -> None:
        self._chat = chat
        self._device_id = device_id
        self.task: asyncio.Task | None = None

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    def detach_task(self) -> asyncio.Task | None:
        task = self.task
        self.task = None
        return task

    async def on_ready(self, tts: str) -> None:
        text = (tts or "").strip()
        if not text:
            return
        self.cancel()
        self.task = asyncio.create_task(self._chat.tts_phoneme_segments(text, device_id=self._device_id))
        logger.info("[LLM] 流式 tts 就绪，提前启动 TTS prefetch text=%r", text[:80])


# 后台过渡语任务：持强引用防 GC，播完自清
_INTERIM_TASKS: set[asyncio.Task] = set()


def _spawn_interim_tts(
    downlink: DownlinkPort,
    chat: ChatService,
    text: str,
    prefetch: _TtsPrefetch,
    *,
    request_id: str | None,
    device_id: str | None,
    round_idx: int,
    device_ws: Any | None = None,
) -> asyncio.Task | None:
    """过渡语「边干活边说」：后台任务播放，立即返回不当阻塞点。

    调用方（工具轮）不等它——工具执行与 TTS 合成并行推进；过渡语与最终回复都在
    同一设备的 pb 通道按序下发，后到的最终回复自然接管画面，无需额外协调。
    """
    playback = (text or "").strip()
    if not playback:
        return None
    # 工具轮里 prefetch 可能已拿本轮 envelope 的 tts 起过合成——此处文本才是要播的，
    # 丢弃旧的避免误播；正式回复的 tts 会在收尾轮重新触发 prefetch。
    prefetch.cancel()

    async def _guarded() -> None:
        try:
            await _play_interim_tts(
                downlink, chat, playback, prefetch, request_id=request_id, device_id=device_id,
                round_idx=round_idx, device_ws=device_ws,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # 过渡语是锦上添花：播不出来也不能影响本轮正式回复
            logger.warning(
                "[LLM] 过渡 TTS 播报失败 device_id=%s req=%s round=%d", device_id, request_id, round_idx,
                exc_info=True,
            )

    task = asyncio.create_task(_guarded())
    _INTERIM_TASKS.add(task)
    task.add_done_callback(_INTERIM_TASKS.discard)
    return task


async def _play_interim_tts(
    downlink: DownlinkPort,
    chat: ChatService,
    text: str,
    prefetch: _TtsPrefetch,
    *,
    request_id: str | None,
    device_id: str | None,
    round_idx: int,
    device_ws: Any | None = None,
) -> None:
    """工具轮过渡语：复用流式 prefetch 任务，与工具执行并行下发 pb。"""
    playback = (text or "").strip()
    if not playback:
        return
    task = prefetch.detach_task()
    if task is None:
        task = asyncio.create_task(chat.tts_phoneme_segments(playback, device_id=device_id))
    interim_result = ChatTurnResult()
    parsed = {
        "reply": playback,
        "servo": [],
        "moves": [],
        "anims": [],
        "json_ok": True,
        "need_reply": True,
        "raw": playback,
    }
    interim_rid = f"{request_id}_interim_{round_idx}" if request_id else None
    logger.info(
        "[LLM] 工具轮过渡 TTS device_id=%s req=%s round=%d text=%r", device_id, request_id, round_idx, playback[:80]
    )
    await downlink.emit_stage(
        "tts_start",
        request_id=interim_rid,
        send_client=False,
        event_fields={"tts_text": playback, "source": "llm_tool_interim", "stage": f"llm_tool_{round_idx}"},
    )
    await _run_pb_playback(
        chat,
        reply_text=playback,
        parsed=parsed,
        llm_scenes=[],
        request_id=interim_rid,
        device_id=device_id,
        result=interim_result,
        t_asr_start=None,
        auto_face_turn=True,
        prefetch_tts=task,
        device_ws=device_ws,
        interaction_request_id=request_id,
    )


async def _play_llm_error_fallback(
    downlink: DownlinkPort,
    chat: ChatService,
    *,
    request_id: str | None,
    device_id: str | None,
    result: ChatTurnResult,
    device_ws: Any | None,
    t_asr_start: float | None,
    llm_exc: Exception,
) -> None:
    """LLM 调用失败：口播道歉 + 连续 idle 舵机，避免点头停后长时间无反馈。"""
    if not get_auto_reply(device_id):
        return
    plan = build_llm_error_fallback_plan()
    playback = plan["tts"]
    parsed = plan["parsed"]
    fallback_rid = f"{request_id}_llm_err" if request_id else None

    motion_done, motion_task = start_llm_error_motion_feedback(device_ws, device_id)
    logger.warning(
        "[LLM] 调用失败，启动兜底 TTS device_id=%s req=%s err=%s tts=%r", device_id, request_id, llm_exc, playback
    )
    try:
        result.llm_text = playback
        result.llm_raw = ""
        result.need_reply = True
        result.t_llm_end = time.monotonic()
        await downlink.emit_stage(
            "llm_error_fallback",
            request_id=fallback_rid,
            send_client=False,
            event_fields={
                "llm_text": playback,
                "error": str(llm_exc),
                "source": "asr" if t_asr_start is not None else "text",
            },
        )
        await downlink.emit_stage(
            "tts_start",
            request_id=fallback_rid,
            send_client=False,
            event_fields={"tts_text": playback, "source": "llm_error_fallback"},
        )
        await _run_pb_playback(
            chat,
            reply_text=playback,
            parsed=parsed,
            llm_scenes=[],
            request_id=fallback_rid,
            device_id=device_id,
            result=result,
            t_asr_start=t_asr_start,
            device_ws=device_ws,
        )
    finally:
        await stop_llm_error_motion_feedback(motion_done, motion_task)


def _is_scheduled_task_user_text(user_text: str) -> bool:
    return str(user_text or "").strip().startswith(_SCHEDULED_TASK_PREFIX)


def _is_system_initiated_user_text(user_text: str) -> bool:
    """系统发起的对话轮（定时任务 / 剧情主动推进）：强制开口并做 meta 汇报兜底。"""
    return str(user_text or "").strip().startswith(_SYSTEM_INITIATED_PREFIXES)


def _scheduled_task_description(user_text: str) -> str:
    text = str(user_text or "").strip().split("\n", 1)[0]
    m = re.search(r"请(?:向主人朗声提醒并)?执行以下任务(?:并向主人汇报结果)?[:：](.+)$", text)
    if m:
        return m.group(1).strip()
    return text.replace(_SCHEDULED_TASK_PREFIX, "").strip()


def _looks_like_plain_reply(text: str) -> bool:
    """判断收尾轮输出是不是「没走 envelope 格式的正经口语」。

    排除两类：以 ``{``/``[`` 开头的（那是 JSON 意图，哪怕解析失败也该重发），以及
    出现 ``"need_reply"`` 字样的（模型复述 schema / 半截 JSON）。剩下的按口语采纳。
    """
    plain = str(text or "").strip()
    if not plain:
        return False
    if plain.startswith("{") or plain.startswith("["):
        return False
    return '"need_reply"' not in plain


def _only_meta_reasoning(text: str) -> bool:
    """整段输出都是内部思考/规则说明（每一行都像），没有任何要对主人说的话。"""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return bool(lines) and all(looks_like_meta_reasoning(line) for line in lines)


_ECHO_ALLOWLIST = frozenset(
    {"你好", "您好", "谢谢", "好的", "好", "嗯", "哦", "明白了", "知道了", "没事", "再见"}
)


def _spoken_norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~～…：:；;\"'“”‘’（）()【】\[\]]+", "", str(text or "")).lower()


def _avoid_verbatim_echo(reply_text: str, user_text: str) -> str:
    """用户正文很短时模型常原样复述（如「第3。」→「第 3。」）；换成自然接话。

    只拦截「规范化后完全相同」的短回声；问候/应答词保留（「你好」→「你好」自然）。
    """
    reply = str(reply_text or "").strip()
    norm_reply = _spoken_norm(reply)
    norm_user = _spoken_norm(user_text)
    if (
        norm_reply
        and norm_reply == norm_user
        and len(norm_reply) <= 12
        and norm_reply not in _ECHO_ALLOWLIST
    ):
        return "嗯，我在听，你接着说。"
    return reply


_SESSION_LABEL_MAX = 24  # 系统轮进历史时保留的标签上限（任务类型/人物名）


def _cap_session_line(text: str) -> str:
    """取首行并截断——系统轮已压缩形态/未知形态的兜底保留。"""
    return str(text or "").strip().split("\n", 1)[0].strip()[:_SESSION_LABEL_MAX]


def _session_user_text(user_text: str, *, is_system_round: bool) -> str:
    """系统轮归档进会话历史时的紧凑形式。

    整段系统指令以 ``role=user`` 进历史会污染模型上下文——它会把「剧情任务 prompt
    原文」当成主人说过的话，也会让主动轮的指令在后续每轮被反复喂回。这里只留可
    辨识的最小标识（任务号/人物），用户真实语音轮原样返回。

    **幂等**：对已压缩过的文本再调用必须返回原值。清洗脚本会反复跑，非幂等会让
    每跑一次就丢一点信息（任务号/人名被逐次吃掉），永远收敛不到稳定状态。
    """
    text = str(user_text or "").strip()
    if not is_system_round:
        return text
    if text.startswith(_SCHEDULED_TASK_PREFIX):
        return f"{_SCHEDULED_TASK_PREFIX} {_scheduled_task_description(text)}".strip()
    if text.startswith(_QUEST_PROACTIVE_PREFIX):
        # 原始形态含任务号：重构前 ``[g_city] 位置``、重构后 ``[g_task3]（长期）``。
        # id 限定 ASCII——``\w`` 会匹配中文，把前缀 ``[系统剧情推进]`` 自己吞掉。
        rest = text[len(_QUEST_PROACTIVE_PREFIX):].strip()
        m = re.search(r"\[([A-Za-z0-9_]+)\]\s*([^\n]*)", rest)
        if m:
            label = m.group(2).strip()
            inner = re.match(r"^（([^）]+)）", label)
            label = inner.group(1) if inner else label.split("：")[0].split(":")[0].strip()
            return f"{_QUEST_PROACTIVE_PREFIX} {m.group(1)} {label[:_SESSION_LABEL_MAX]}".strip()
        # 没有任务号：已是紧凑形式（如 ``g_city 位置``）或未知形态，按首行截断保留
        return f"{_QUEST_PROACTIVE_PREFIX} {_cap_session_line(rest)}".strip()
    if text.startswith(_SOCIAL_PROACTIVE_PREFIX):
        rest = text[len(_SOCIAL_PROACTIVE_PREFIX):].strip()
        m = re.search(r"认识的人（([^）]+)）", rest)
        return f"{_SOCIAL_PROACTIVE_PREFIX} {m.group(1) if m else _cap_session_line(rest)}".strip()
    return text


def _scheduled_reminder_tts(description: str) -> str:
    desc = str(description or "").strip()
    if not desc:
        return "主人，提醒时间到了。"
    if desc.startswith("提醒"):
        body = desc[2:].strip() or "一下"
        return f"主人，该{body}啦。"
    return f"主人，{desc}。"


def _scheduled_tts_looks_like_meta_report(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    meta_markers = ("已发送", "已提醒", "已完成", "已执行", "汇报", "任务完成", "提醒过了")
    return any(m in t for m in meta_markers)


_SOCIAL_META_MARKERS = ("已向", "已问候", "已表达", "已关心", "已主动", "问候完成", "关心完了")


def _social_tts_looks_like_meta_report(text: str) -> bool:
    """社交主动轮判定是否为「记账式汇报」文案（应静默而非照字朗读）。

    模型在问候类主动轮里易把已调用的 update_daily_task 内容复述成
    「已向小明问好」类句子——这种汇报腔不是对用户说的话，一律按静默处理。
    """
    t = str(text or "").strip()
    if not t:
        return True
    return any(m in t for m in _SOCIAL_META_MARKERS) or _scheduled_tts_looks_like_meta_report(t)


def _voice_was_played(result: ChatTurnResult) -> bool:
    if result.voice_auto_reply_off or result.error or result.status != "ok":
        return False
    if result.playback_completed is False:
        return False
    if result.t_tts_synth_end is None or result.t_llm_end is None:
        return False
    return result.t_tts_synth_end > result.t_llm_end + 0.05


def _extract_face_sight_lines(user_message: str) -> str | None:
    """从装配好的 user 消息中抽取「图像识别」人脸行（无 faceid 行 → None）。

    prompt 与实验台气泡共用同一次装配，保证两边识别内容同源。
    """
    lines = (user_message or "").splitlines()
    out = [line for line in lines if line.strip().startswith("faceid=")]
    return "\n".join(out) if out else None


# voice_sight（实验台声纹气泡）不再从文本反抽：声纹段已并入用户正文行括号注记，
# 由 run_chat_turn 在装配同刻调用 format_voice_speaker_note 取得（气泡与 prompt 同源）


MAX_LLM_TOOL_ROUNDS = 8

# 会话上下文策略（run_chat_turn 组装 history_messages）：
# - 相邻轮次间隔超过该秒数 → 停止向上追溯（丢弃更旧段）
_HISTORY_MAX_GAP_SECONDS = 5 * 60
# - 历史消息累计 token 达到当前 LLM 上下文一半 → 停止追溯。
#   窗口取设备 llm_param.context_window；未配置时回退该默认
#   （本地引擎 llm-qwen/llm-minicpm/llm-unee 的每 slot n_ctx 为 8192 → 一半 4096）
_DEFAULT_LLM_CTX_TOKENS = 8192


# llm_calls[].raw 序列化上限（字符）：引擎原始返回仅供实验台调试查看，过长截断防事件体膨胀
_LLM_CALL_RAW_MAX_CHARS = 30000


def _llm_raw_text(raw: Any) -> tuple[str | None, bool]:
    """把引擎原始返回体序列化为 compact JSON 文本；非 dict/序列化失败 → (None, False)。"""
    if not isinstance(raw, dict):
        return None, False
    try:
        text = json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return None, False
    truncated = len(text) > _LLM_CALL_RAW_MAX_CHARS
    if truncated:
        text = text[:_LLM_CALL_RAW_MAX_CHARS]
    return text, truncated


def _history_token_budget(device_id: str | None) -> int:
    """当前 LLM 上下文窗口一半作为历史 token 预算（保近弃远）。"""
    cw: int | None = None
    did = str(device_id or "").strip()
    if did:
        try:
            from deskbot_server.service.llm_routing import resolve_llm_config

            cw = resolve_llm_config(did).context_window
        except Exception:
            cw = None
    if not cw:
        cw = _DEFAULT_LLM_CTX_TOKENS
    return max(1024, int(cw) // 2)

_TOOL_RESULT_STRIP_KEYS = frozenset({"jpeg_base64", "image_display"})


def _tool_result_for_llm(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    for key in _TOOL_RESULT_STRIP_KEYS:
        if key not in out:
            continue
        val = out.pop(key)
        if isinstance(val, str) and val:
            out[f"{key}_len"] = len(val)
        elif isinstance(val, dict) and val:
            out[f"{key}_ok"] = True
    return out


def build_history_messages(
    rows: list[dict[str, Any]], *, token_budget: int | None = None
) -> list[dict[str, str]]:
    """会话上下文窗口 → LLM history_messages（保近弃远）。

    ``rows`` 来自 ``session_context_window``（已按轮次间隔截断），按时间正序；
    此处再做 token 预算裁剪：从最新往回保留，累计估算 token 超过预算即停止。
    预算缺省 = 默认上下文（8192）的一半。
    """
    if not rows:
        return []
    if token_budget is None:
        token_budget = _DEFAULT_LLM_CTX_TOKENS // 2
    from deskbot_server.service.llm_context import estimate_text_tokens

    keep: list[dict[str, Any]] = []
    total = 0
    for row in reversed(rows):
        role = str(row.get("role") or "")
        content = str(row.get("content") or "")
        # 历史里的 assistant 行只保留「说给主人听的那句话」：
        # 旧 JSON envelope 取 tts；纯文本里的内部思考/规则说明直接丢弃，
        # 防止模型读到自己的推理样例后继续复述规则。
        if role == "assistant":
            content = strip_llm_preamble(content)
            if not content:
                continue
        cost = estimate_text_tokens(content) + 4
        if keep and total + cost > token_budget:
            break
        keep.append({"role": role, "content": content})
        total += cost
    keep.reverse()
    return keep


async def _execute_tools_round(
    tools: list[dict[str, Any]],
    *,
    device_id: str,
    session_id: str | None,
    device_ws: DeviceWsService | None,
) -> list[dict[str, Any]]:
    """执行一轮工具（相机帧率由固件默认策略决定，无 cam_fps 下发/提升）。"""
    return await execute_llm_tools(tools, device_id=device_id, session_id=session_id, device_ws=device_ws)


async def complete_llm_with_tool_loop(
    chat: ChatService,
    user_text: str,
    *,
    device_id: str | None = None,
    session_id: str | None = None,
    device_context: str | None = None,
    history_messages: list[dict[str, str]] | None = None,
    request_id: str | None = None,
    pipeline_source: str | None = None,
    device_ws: DeviceWsService | None = None,
    tts_prefetch: _TtsPrefetch | None = None,
    on_interim_tts_play: Callable[[str, int], Awaitable[None]] | None = None,
    bus_service: Any | None = None,
    user_message_override: str | None = None,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
    on_round_end: Callable[[], Awaitable[bool]] | None = None,
) -> LlmTurnResult:
    """唯一 LLM 对话通道：原生 function calling 多轮，每轮 tools=原生 schema，结果以 role=tool 回灌。

    （legacy 文本 tools 通道已移除，envelope 不再含 ``tools`` 字段。）

    收尾语义：模型某轮无 tool_calls → content 即最终 JSON envelope，直接结束；
    content 非 JSON/为空 → 走文本 JSON 收口一次（自带重试/纯文本包装兜底）。
    """
    from deskbot_server.service.llm_tools_schema import build_native_tool_schemas
    if device_id and pipeline_source == "asr":
        from deskbot_server.service.application.local_clock_reply import local_clock_reply
        from deskbot_server.service.llm_routing import resolve_device_llm_provider
        quick_reply = local_clock_reply(user_text) if resolve_device_llm_provider(device_id) == "realtime" else None
        if quick_reply:
            if on_delta is not None:
                await on_delta(quick_reply)
            streamed = bool(await on_round_end()) if on_round_end is not None else False
            return LlmTurnResult(parsed=parse_llm_reply(quick_reply), answer=quick_reply, reply_streamed=streamed,
                                 llm_calls=[{"n": 1, "model": "local_clock", "ms": 0, "text": quick_reply}])

    extra_messages: list[dict[str, Any]] = []
    all_tools: list[dict[str, Any]] = []
    all_tool_results: list[dict[str, Any]] = []
    llm_calls: list[dict[str, Any]] = []
    answer = ""
    parsed: dict[str, Any] = parse_llm_reply("")
    interim_spoken = False  # 本次请求是否已说过过渡语（整轮只说一次）
    system_prompt: str | None = None
    captured_system_prompt = False
    final_streamed = False
    llm_model = llm_model_label(device_id)
    # say 过渡语工具只在首轮工具轮提供：后续轮模型看不到它自然不会调用，
    # 免掉「每轮都插一句」的观感问题（无需服务端额外拦截）。
    tool_schemas_first_round = build_native_tool_schemas(device_id=device_id, is_tool_round=True)
    tool_schemas_later_round = build_native_tool_schemas(device_id=device_id)

    def _on_system_prompt(content: str) -> None:
        nonlocal system_prompt, captured_system_prompt
        if not captured_system_prompt:
            system_prompt = content
            captured_system_prompt = True

    def _base_kwargs(round_idx: int) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "device_context": device_context if round_idx == 0 else None,
            "device_id": device_id,
            "history_messages": history_messages if round_idx == 0 else None,
            "extra_messages": extra_messages or None,
            "on_system_prompt": _on_system_prompt,
        }
        if user_message_override:
            kw["user_message_override"] = user_message_override
        return kw

    async def _legacy_final_round() -> str:
        """模型收尾失败时用文本路径收口（完整 JSON envelope + 内置重试/包装）。"""
        kw = _base_kwargs(0)
        kw.pop("extra_messages", None)
        kw["extra_messages"] = extra_messages or None
        return str(await chat.llm(user_text, **kw) or "")

    for round_idx in range(MAX_LLM_TOOL_ROUNDS):
        round_t0 = time.monotonic()
        try:
            stream_kwargs = {"on_delta": on_delta} if on_delta is not None else {}
            result = await chat.llm_tool_round(
                user_text,
                tools=tool_schemas_first_round if round_idx == 0 else tool_schemas_later_round,
                tool_choice="auto",
                **_base_kwargs(round_idx),
                **stream_kwargs,
            )
        except Exception as llm_exc:
            llm_calls.append({
                "n": round_idx + 1, "model": llm_model,
                "ms": int((time.monotonic() - round_t0) * 1000),
                "text": f"[调用失败] {llm_exc}", "truncated": False,
                "raw": None, "raw_truncated": False,
            })
            raise
        content = str(result.content or "")
        calls = list(result.tool_calls or [])
        if on_round_end is not None:
            streamed_round = await on_round_end()
            if calls and streamed_round:
                interim_spoken = True
            if not calls:
                final_streamed = streamed_round
        summary = content or (
            f"[tool_calls: {', '.join(str(c.get('name') or '') for c in calls)}]" if calls else ""
        )
        _meta = result.meta if isinstance(result.meta, dict) else None
        raw_text, raw_truncated = _llm_raw_text(_meta.get("raw_response") if _meta else None)
        llm_calls.append({
            "n": round_idx + 1, "model": llm_model,
            "ms": int((time.monotonic() - round_t0) * 1000),
            "text": summary[:3000], "truncated": len(summary) > 3000,
            "raw": raw_text, "raw_truncated": raw_truncated,
        })

        if calls:
            native_tools: list[dict[str, Any]] = []
            for c in calls:
                row: dict[str, Any] = {"tool": str(c.get("name") or "")}
                try:
                    args = json.loads(c.get("arguments") or "{}")
                    if isinstance(args, dict):
                        row.update(args)
                except (TypeError, ValueError):
                    pass
                native_tools.append(row)
            all_tools.extend(native_tools)
            envelope_reply = ""
            if content:
                maybe = parse_llm_reply(content)
                if maybe.get("json_ok") and maybe.get("reply"):
                    # 模型同时给最终回复与工具调用：执行后结束（与 legacy 特例一致）
                    tool_results = await _execute_tools_round(
                        native_tools, device_id=str(device_id), session_id=session_id, device_ws=device_ws
                    )
                    all_tool_results.extend(tool_results)
                    answer = content
                    parsed = maybe
                    break
                # 工具轮 envelope：tts 非空即过渡语（本模块「模型自述」路径），
                # 空 tts + 只有动作字段是正常形态，不算过渡语。
                # 纯文本工具轮只取非内部思考的最后一段口语。
                envelope_reply = (
                    str(maybe.get("reply") or "")
                    if maybe.get("json_ok")
                    else strip_llm_preamble(content)
                )
                if envelope_reply and looks_like_meta_reasoning(envelope_reply):
                    envelope_reply = ""
            tool_results = await _execute_tools_round(
                native_tools, device_id=str(device_id), session_id=session_id, device_ws=device_ws
            )
            all_tool_results.extend(tool_results)
            # 「边干活边说」：过渡语后台播报，工具执行与 TTS 合成并行推进，
            # 不占用本轮的端到端延迟；每轮最多一句，round 2+ 模型已看不见 say。
            if on_interim_tts_play is not None and not interim_spoken:
                say_text = next(
                    (str(r.get("reply") or "") for r in tool_results
                     if str(r.get("tool") or "") == "say" and r.get("ok")),
                    "",
                )
                interim_text = resolve_interim_tts(
                    say_text=say_text, model_text=envelope_reply, tools=native_tools
                )
                if interim_text:
                    interim_spoken = True
                    await on_interim_tts_play(interim_text, round_idx + 1)
            logger.info(
                "[LLM] native tool round=%d device_id=%s req=%s tools=%s results=%s",
                round_idx + 1, device_id, request_id,
                native_tools, [_tool_result_for_llm(r) for r in tool_results],
            )
            if bus_service is not None and device_id and request_id:
                tool_names = [str(t.get("tool") or "") for t in native_tools if str(t.get("tool") or "")]
                await bus_service.pub(device_id, {
                    "request_id": request_id,
                    "source": pipeline_source or "asr",
                    "asr_text": user_text,
                    "stage": f"llm_tool_{round_idx + 1}",
                    "status": "running",
                    "llm_text": (f"执行工具: {', '.join(tool_names)}" if tool_names else "执行工具"),
                })
            # 原生 trail：assistant(tool_calls) + 逐条 role=tool
            extra_messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": str(c.get("id") or ""),
                        "type": "function",
                        "function": {"name": str(c.get("name") or ""), "arguments": str(c.get("arguments") or "")},
                    }
                    for c in calls
                ],
            })
            for i, c in enumerate(calls):
                out_row = tool_results[i] if i < len(tool_results) else {
                    "tool": c.get("name"), "ok": False, "error": "结果缺失"
                }
                extra_messages.append({
                    "role": "tool",
                    "tool_call_id": str(c.get("id") or ""),
                    "content": json.dumps(out_row, ensure_ascii=False),
                })
            continue

        # 模型收尾：无工具调用。
        # 新协议：对话模型直接输出纯文本口语（不再要求 JSON envelope）。
        # 兼容历史/其它调用方仍返回合法 envelope 的情况，照旧解析。
        if content:
            parsed = parse_llm_reply(content)
            if parsed.get("json_ok"):
                answer = content
                break
            # 整段都是「系统已识别/按规则/保持安静」类内部说明：
            # 绝不能当口语朗读（实测会被 TTS 念出来并写进历史，形成自我强化）。
            # 只对「纯文本」判定；坏 JSON 里含 need_reply 字样不能误判成说明。
            if _looks_like_plain_reply(content) and _only_meta_reasoning(content):
                logger.warning(
                    "[LLM] 收尾轮只有内部思考/规则说明，按静默处理 device_id=%s req=%s preview=%r",
                    device_id, request_id, content[:120],
                )
                parsed = parse_llm_reply("")
                parsed["need_reply"] = False
                answer = ""
                break
            # 纯文本收尾：直接采纳。模型若还残留推理前言，strip_llm_preamble 会取最后一段口语。
            plain = strip_llm_preamble(content) if _looks_like_plain_reply(content) else ""
            if plain:
                logger.info(
                    "[LLM] native 收尾轮纯文本，直接采纳 device_id=%s req=%s preview=%r",
                    device_id, request_id, plain[:120],
                )
                parsed = parse_llm_reply(plain)
                parsed["need_reply"] = True
                answer = plain
                break
            # 疑似坏 JSON / 半截 envelope：做一次纯文本收口，避免把 JSON 念出来
            answer = content
            logger.warning(
                "[LLM] native 收尾轮疑似坏 JSON，走文本收口 device_id=%s req=%s preview=%r",
                device_id, request_id, content[:120],
            )
            fallback = str(await _legacy_final_round() or "")
            if _looks_like_plain_reply(fallback) and _only_meta_reasoning(fallback):
                logger.warning(
                    "[LLM] 文本收口仍只有内部思考/规则说明，按静默处理 device_id=%s req=%s preview=%r",
                    device_id, request_id, fallback[:120],
                )
                parsed = parse_llm_reply("")
                parsed["need_reply"] = False
                answer = ""
            elif fallback.strip():
                answer = fallback
                parsed = parse_llm_reply(fallback)
                if not parsed.get("json_ok") and _looks_like_plain_reply(fallback):
                    plain = strip_llm_preamble(fallback)
                    if plain:
                        parsed = parse_llm_reply(plain)
                        parsed["need_reply"] = True
                        answer = plain
            break

        # 空内容 = 本轮静默（纯文本协议下模型选择不输出任何文字）。
        # 不再像 JSON envelope 时代那样重发一轮——重发会把「主动轮判断不宜开口」
        # 硬改成一句多余的话。用户轮/定时轮由调用方按既有兜底补齐。
        logger.info("[LLM] native 收尾轮空内容，按静默处理 device_id=%s req=%s", device_id, request_id)
        parsed = parse_llm_reply("")
        parsed["need_reply"] = False
        answer = ""
        break
    else:
        logger.warning("[LLM] native tool 循环达到上限 %d device_id=%s req=%s", MAX_LLM_TOOL_ROUNDS, device_id, request_id)

    return LlmTurnResult(
        parsed=parsed,
        tools=all_tools,
        tool_results=all_tool_results,
        answer=answer,
        system_prompt=system_prompt,
        llm_calls=llm_calls,
        reply_streamed=final_streamed,
    )


def _interaction_kind(user_text: str) -> InteractionKind:
    text = str(user_text or "").strip()
    if text.startswith(_SCHEDULED_TASK_PREFIX):
        return InteractionKind.SCHEDULED
    if text.startswith(_QUEST_PROACTIVE_PREFIX):
        return InteractionKind.QUEST
    if text.startswith(_SOCIAL_PROACTIVE_PREFIX):
        return InteractionKind.SOCIAL
    return InteractionKind.USER


async def run_chat_turn(
    downlink: DownlinkPort,
    chat: ChatService,
    user_text: str,
    *,
    request_id: str | None = None,
    device_id: str | None = None,
    device_ws: DeviceWsService | None = None,
    t_asr_start: float | None = None,
    t_asr_text: float | None = None,
    force_voice: bool = False,
    reuse_session_id: str | None = None,
    on_llm_error: Any | None = None,
    bus_service: Any | None = None,
) -> ChatTurnResult:
    """Arbitrate one device's complete LLM -> TTS -> playback lifecycle."""
    from deskbot_server.service.conversation_service import conversation_provider
    if device_id and conversation_provider(device_id) in {"moss-live", "flecket"}:
        return ChatTurnResult(need_reply=False, dialogue_act="silence", status="skipped")
    if not device_id:
        return await _run_chat_turn_unarbitrated(
            downlink,
            chat,
            user_text,
            request_id=request_id,
            device_id=device_id,
            device_ws=device_ws,
            t_asr_start=t_asr_start,
            t_asr_text=t_asr_text,
            force_voice=force_voice,
            reuse_session_id=reuse_session_id,
            on_llm_error=on_llm_error,
            bus_service=bus_service,
        )

    kind = _interaction_kind(user_text)
    lease = await interaction_arbiter.acquire(device_id, kind=kind, request_id=request_id)
    if lease is None:
        now_m = time.monotonic()
        return ChatTurnResult(
            need_reply=False,
            dialogue_act="silence",
            t_llm_end=now_m,
            t_tts_synth_end=now_m,
            t_tts_end=now_m,
            status="skipped",
        )

    async with lease:
        # The arbiter has already cancelled a lower-priority application turn.
        # Clear any PB it managed to enqueue before cancellation so the user's
        # answer never waits behind stale proactive/reminder audio.
        if kind == InteractionKind.USER and device_ws is not None:
            interrupt = getattr(device_ws, "interrupt_current_playback", None)
            if callable(interrupt):
                await interrupt(device_id, reason=f"user_turn:{request_id or ''}")
        try:
            return await _run_chat_turn_unarbitrated(
                downlink,
                chat,
                user_text,
                request_id=request_id,
                device_id=device_id,
                device_ws=device_ws,
                t_asr_start=t_asr_start,
                t_asr_text=t_asr_text,
                force_voice=force_voice,
                reuse_session_id=reuse_session_id,
                on_llm_error=on_llm_error,
                bus_service=bus_service,
            )
        except asyncio.CancelledError:
            # Do this before releasing the lease: old cleanup must not clear
            # audio already submitted by the next owner.
            if device_ws is not None:
                interrupt = getattr(device_ws, "interrupt_current_playback", None)
                if callable(interrupt):
                    await interrupt(device_id, reason=f"turn_cancelled:{request_id or ''}")
            raise


async def _run_chat_turn_unarbitrated(
    downlink: DownlinkPort,
    chat: ChatService,
    user_text: str,
    *,
    request_id: str | None = None,
    device_id: str | None = None,
    device_ws: DeviceWsService | None = None,
    t_asr_start: float | None = None,
    t_asr_text: float | None = None,
    force_voice: bool = False,
    reuse_session_id: str | None = None,
    on_llm_error: Any | None = None,
    bus_service: Any | None = None,
) -> ChatTurnResult:
    """在已有用户侧文本后执行 LLM + TTS/pb 管道（应用层，不依赖 WebSocket 类型）。"""
    result = ChatTurnResult()
    decision_task: asyncio.Task | None = None
    speech = None
    speech_stream = None
    tts_prefetch = None
    interim_tasks: list[asyncio.Task] = []
    is_scheduled = _is_scheduled_task_user_text(user_text)
    is_quest_proactive = str(user_text or "").strip().startswith(_QUEST_PROACTIVE_PREFIX)
    is_social_proactive = str(user_text or "").strip().startswith(_SOCIAL_PROACTIVE_PREFIX)
    is_system_round = is_scheduled or is_quest_proactive or is_social_proactive
    sched_desc = _scheduled_task_description(user_text) if is_scheduled else ""

    # 语音轮在 asr 完成后「一次性装配」user 消息（含该时刻的人脸识别）：
    # 同一份文本既作为整轮 LLM 输入（user_message_override），又提取出
    # face_sight 展示到实验台用户气泡——气泡与 prompt 始终同源、同刻。
    voice_user_message: str | None = None
    ack_ctx: str | None = None
    if device_ws is not None and device_id:
        ack_ctx = await device_ws.pb_ack_llm_context(device_id)
    if t_asr_start is not None and device_id:
        try:
            voice_user_message = build_llm_user_message(
                user_text, device_id=device_id, device_context=ack_ctx, from_asr=True
            )
            result.face_sight = _extract_face_sight_lines(voice_user_message)
            # 声纹身份已并入正文行括号注记：展示直接取注记本身（与 prompt 同刻同快照）
            result.voice_sight = format_voice_speaker_note(str(device_id))
            # 识别耗时与识别文本同刻（同一同步块、无 await，快照不会被并发帧/句抢写）采样：
            # face_ms 取装配所用帧的检测耗时；voice_ms 取本次声纹识别终态的 elapsed_ms
            if result.face_sight:
                result.face_ms = face_snapshot_detect_ms(device_id)
            if result.voice_sight:
                _vpr_snap = get_voice_snapshot(device_id)
                if _vpr_snap:
                    result.voice_ms = _vpr_snap.get("elapsed_ms")
        except Exception:
            logger.debug("[LLM] 语音轮 user 消息装配失败 device_id=%s", device_id, exc_info=True)

    # 捕获本轮在场的已知用户（与 user 消息识别同刻；供对话结束打点 last_talk）
    recognized_users: list[str] = []
    if device_id and not is_system_round:
        try:
            recognized_users = recognized_known_users(device_id)
        except Exception:
            logger.debug("[LLM] 捕获已知用户失败 device_id=%s", device_id, exc_info=True)

    try:
        if not force_voice and not get_auto_reply(device_id):
            now_m = time.monotonic()
            result.t_llm_end = now_m
            result.t_tts_synth_end = now_m
            result.t_tts_end = now_m
            result.voice_auto_reply_off = True
            logger.info(
                "[asr] 自动应答已关闭，跳过 LLM/TTS device_id=%s req=%s user=%r",
                device_id,
                request_id,
                (user_text or "")[:120],
            )
            await downlink.emit_stage(
                "voice_auto_reply_off",
                request_id=request_id,
                send_client=False,
                event_fields={
                    "asr_text": user_text,
                    "asr_ms": _ms_between(t_asr_start, t_asr_text),
                    "source": "asr" if t_asr_start is not None else "text",
                    "status": "ok",
                },
            )
            return result

        # 会话上下文：同一 session 向上追溯历史，两条截断策略——
        # ① 轮次间隔 > _HISTORY_MAX_GAP_SECONDS 即停；② 历史累计 token ≥ _HISTORY_BUDGET_TOKENS 即停。
        session_id: str | None = None
        history_messages: list[dict[str, str]] | None = None
        if device_id:
            from deskbot_server.service.session_service import ensure_active_session, session_context_window
            from deskbot_server.utils.async_helpers import run_blocking

            if reuse_session_id:
                session_id = str(reuse_session_id).strip() or None
            if not session_id:
                active = await run_blocking(ensure_active_session, device_id, user_text=user_text)
                session_id = str(active.get("session_id") or "") or None
            if session_id:
                rows = await run_blocking(
                    session_context_window, device_id, session_id, max_gap_seconds=_HISTORY_MAX_GAP_SECONDS
                )
                history_messages = (
                    build_history_messages(rows, token_budget=_history_token_budget(device_id)) if rows else None
                )

        # ── 决策模型前置门控（语音轮）──
        # 对话模型之前先判定：输入是否正常（valid）、是否需要开口（respond），
        # 同时选好动作/表情并立即 PB 下发；invalid/不需回应则直接跳过 LLM/TTS。
        pre_decision: dict[str, Any] | None = None
        pre_moves: list[Any] = []
        pre_anims: list[Any] = []
        fast_voice = False
        if device_id and t_asr_start is not None and not is_system_round:
            from deskbot_server.service.llm_routing import resolve_device_llm_provider
            fast_voice = resolve_device_llm_provider(device_id) == "realtime" and not _user_explicitly_requests_silence(user_text)
        if fast_voice:
            from deskbot_server.service.application.action_decision import decide_turn_actions
            # ASR validation + attention already passed. Gestures are optional
            # and run beside generation, rather than adding a serial model hop.
            decision_task = asyncio.create_task(decide_turn_actions(
                device_id, user_text, None, request_id=request_id,
                context=_build_decision_context(history_messages, result),
            ))
        if device_id and t_asr_start is not None and not is_system_round and not fast_voice:
            from deskbot_server.service.application.action_decision import decide_turn_actions

            pre_decision = await decide_turn_actions(
                device_id,
                user_text,
                None,
                request_id=request_id,
                judge_input=True,
                context=_build_decision_context(history_messages, result),
            )
            pre_moves = list(pre_decision.get("moves") or [])
            pre_anims = list(pre_decision.get("anims") or [])
            result.decision_valid = pre_decision.get("valid")
            result.decision_respond = pre_decision.get("respond")
            result.decision_model = pre_decision.get("model")
            result.decision_ms = pre_decision.get("elapsed_ms")
            result.decision_error = pre_decision.get("error")
            result.moves = pre_moves
            result.anims = pre_anims
            # ASR text validation and the face/voice attention gate already ran.
            # In real-device logs the small classifier frequently returns the
            # contradictory valid=False/respond=True for clear questions such
            # as “现在几点了？” and “在吗？”. Treat validity as diagnostic here;
            # the explicit response decision owns whether we should speak.
            # A clear question that passed ASR/attention must not be silenced by
            # an uncalibrated small classifier (real "现在几点啦？" scored .4476).
            # Explicit requests for silence still take precedence.
            direct_question = (
                str(user_text or "").rstrip().endswith(("?", "？"))
                and not _user_explicitly_requests_silence(user_text)
            )
            question_overridden = direct_question and not pre_decision.get("respond", True)
            skip_llm = not pre_decision.get("respond", True) and not direct_question
            validity_overridden = not pre_decision.get("valid", True) and not skip_llm
            if validity_overridden:
                logger.warning(
                    "[decision] 输入有效性与回应判定冲突，按 respond 放行 device_id=%s req=%s "
                    "valid_prob=%s respond_prob=%s",
                    device_id, request_id, pre_decision.get("valid_prob"), pre_decision.get("respond_prob"),
                )
            if question_overridden:
                logger.warning(
                    "[decision] 明确提问优先回应 device_id=%s req=%s respond_prob=%s",
                    device_id, request_id, pre_decision.get("respond_prob"),
                )
            await downlink.emit_stage(
                "decision_gate",
                request_id=request_id,
                send_client=False,
                event_fields={
                    "asr_text": user_text,
                    "valid": bool(pre_decision.get("valid", True)),
                    "respond": bool(pre_decision.get("respond", True)),
                    "valid_prob": pre_decision.get("valid_prob"),
                    "respond_prob": pre_decision.get("respond_prob"),
                    "moves": pre_moves,
                    "anims": pre_anims,
                    "model": pre_decision.get("model"),
                    "decision_ms": pre_decision.get("elapsed_ms"),
                    "skip_llm": skip_llm,
                    "validity_overridden": validity_overridden,
                    "question_overridden": question_overridden,
                    "source": "asr",
                },
            )
            if skip_llm:
                motion_task = _spawn_decision_motion(
                    chat, moves=pre_moves, anims=pre_anims, request_id=request_id,
                    device_id=device_id, device_ws=device_ws, t_asr_start=t_asr_start,
                )
                if motion_task is not None:
                    await motion_task
                now_m = time.monotonic()
                result.need_reply = False
                result.dialogue_act = "silent"
                result.llm_text = ""
                result.llm_raw = ""
                result.json_ok = True
                result.t_llm_end = now_m
                result.t_tts_synth_end = now_m
                result.t_tts_end = now_m
                logger.info(
                    "[decision] 前置门控跳过 LLM/TTS device_id=%s req=%s valid=%s respond=%s "
                    "valid_prob=%s respond_prob=%s moves=%s anims=%s text=%r",
                    device_id,
                    request_id,
                    pre_decision.get("valid"),
                    pre_decision.get("respond"),
                    pre_decision.get("valid_prob"),
                    pre_decision.get("respond_prob"),
                    pre_moves,
                    pre_anims,
                    (user_text or "")[:80],
                )
                return result

        tts_prefetch = _TtsPrefetch(chat, device_id=device_id)

        speech_audio: list[bytes] = []
        speech_sr = 0
        speech_request_id = request_id or uuid.uuid4().hex[:16]
        if (
            device_id and device_ws is not None and not is_system_round
            and getattr(chat, "supports_streaming", False) is True
            and not _user_explicitly_requests_silence(user_text)
        ):
            from deskbot_server.service.application.streaming_speech import StreamingSpeech

            def _clean_phrase(text: str) -> str:
                # System speech reads emoji names aloud; symbols on their own
                # are not a spoken phrase and must not start another TTS job.
                text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", text)
                if not re.search(r"[\w\u4e00-\u9fff]", text):
                    return ""
                if not _looks_like_plain_reply(text) or looks_like_meta_reasoning(text):
                    return ""
                # If the model only repeats the user's input, let the final
                # response policy replace it instead of emitting an echo early.
                if _spoken_norm(user_text).startswith(_spoken_norm(text)):
                    return ""
                text, _ = _suppress_recent_duplicate_questions(text, history_messages)
                return strip_llm_preamble(text)

            async def _synthesize_phrase(text: str):
                started = time.monotonic()
                audio = await chat.tts_phoneme_segments(text, device_id=device_id)
                result.tts_synth_ms = (result.tts_synth_ms or 0.0) + (time.monotonic() - started) * 1000
                return audio

            async def _play_phrase(text: str, audio: Any, index: int) -> None:
                nonlocal speech_sr, decision_task, speech_stream, pre_moves, pre_anims
                speech_sr, segments = audio
                speech_audio.append(b"".join(s.get("pcm") or b"" for s in segments))
                if index == 0:
                    result.t_tts_first_audio = time.monotonic()
                    logger.info(
                        "[latency] device_id=%s req=%s asr_done_to_first_audio_ready_ms=%s",
                        device_id, request_id, _ms_between(t_asr_text, result.t_tts_first_audio),
                    )
                    await downlink.emit_stage(
                        "tts_start", request_id=request_id, send_client=False,
                        event_fields={"asr_text": user_text, "tts_text": text,
                                      "tts_model": tts_model_label(device_id), "source": "asr" if t_asr_start else "text"},
                    )
                if speech_stream is None and callable(getattr(device_ws, "open_speech_stream", None)):
                    speech_stream = await device_ws.open_speech_stream(
                        device_id, speech_request_id, sr=speech_sr,
                        fmt=str(chat.tts_cfg.get("output_codec") or "s16le"),
                    )
                ready = asyncio.get_running_loop().create_future()
                ready.set_result(audio)
                phrase_motion = {"moves": pre_moves if index == 0 else [],
                                 "anims": pre_anims if index == 0 else [], "servo": []}
                # Text rounds decide after model output. Use a ready decision
                # on the next phrase; optional motion never delays speech.
                if decision_task is not None and decision_task.done():
                    _apply_decision(phrase_motion, result, await _await_decision(decision_task))
                    pre_moves = list(phrase_motion.get("moves") or [])
                    pre_anims = list(phrase_motion.get("anims") or [])
                    decision_task = None
                await _run_pb_playback(
                    chat, reply_text=text,
                    parsed=phrase_motion,
                    llm_scenes=[], request_id=f"{speech_request_id}_s{index}",
                    interaction_request_id=request_id,
                    device_id=device_id, result=result, t_asr_start=t_asr_start,
                    prefetch_tts=ready, device_ws=device_ws, pb_stream=speech_stream,
                )
                if result.playback_completed is False:
                    raise RuntimeError(f"stream playback: {result.playback_status}/{result.playback_reason}")

            speech = StreamingSpeech(_synthesize_phrase, _play_phrase, _clean_phrase)

        async def _on_delta(delta: str) -> None:
            if result.t_llm_first_token is None:
                result.t_llm_first_token = time.monotonic()
            await speech.feed(delta)

        async def _on_interim_tts_play(text: str, round_idx: int) -> None:
            if speech is not None:
                await speech.feed(text)
                await speech.end_round()
                return
            # 派生后台任务即刻返回：工具轮不等过渡语播完（边干活边说）
            task = _spawn_interim_tts(
                downlink, chat, text, tts_prefetch, request_id=request_id, device_id=device_id, round_idx=round_idx,
                device_ws=device_ws,
            )
            if task is not None:
                interim_tasks.append(task)

        llm_turn = await complete_llm_with_tool_loop(
            chat,
            user_text,
            device_id=device_id,
            session_id=session_id,
            device_context=ack_ctx,
            history_messages=history_messages,
            request_id=request_id,
            pipeline_source="asr" if t_asr_start is not None else "text",
            tts_prefetch=tts_prefetch,
            on_interim_tts_play=_on_interim_tts_play,
            device_ws=device_ws,
            bus_service=bus_service,
            user_message_override=voice_user_message,
            on_delta=_on_delta if speech is not None else None,
            on_round_end=speech.end_round if speech is not None else None,
        )
        parsed = llm_turn.parsed
        if llm_turn.reply_streamed:
            # Archive the same cleaned phrases sent to TTS, including multiple
            # spoken lines, rather than the legacy parser's last-line fallback.
            parsed["reply"] = speech.last_round_text
            parsed["need_reply"] = bool(speech.last_round_text)

        reply_text = parsed["reply"]
        # 安全网：任何路径（含旧 JSON envelope / 文本收口）混入的规则说明都不能朗读。
        # 只检查纯文本回复；坏 JSON 原文不在 TTS 路径上。
        if reply_text and _looks_like_plain_reply(reply_text) and looks_like_meta_reasoning(reply_text):
            logger.warning(
                "[LLM] 回复为内部思考/规则说明，已拦截不朗读 device_id=%s req=%s preview=%r",
                device_id, request_id, reply_text[:120],
            )
            reply_text = ""
            parsed["need_reply"] = False
        if reply_text:
            adjusted = _avoid_verbatim_echo(reply_text, user_text)
            if adjusted != reply_text:
                logger.info(
                    "[LLM] 原样复述用户，改为自然接话 device_id=%s req=%s user=%r reply=%r",
                    device_id, request_id, (user_text or "")[:80], reply_text[:80],
                )
                reply_text = adjusted
                parsed["reply"] = adjusted
        # 对话模型只负责口语文本；动作/表情一律由独立决策模型决定（忽略旧 envelope 字段）
        parsed["moves"] = pre_moves
        parsed["anims"] = pre_anims
        parsed["servo"] = []
        parsed["scenes"] = []
        need_reply = bool(parsed.get("need_reply", True))
        dialogue_act = str(parsed.get("dialogue_act") or "answer")

        # 语音轮已在 LLM 前做过决策门控，动作随首段语音并行下发；
        # 文本轮 / 系统主动轮沿用「回复后决策 + 与 TTS 合成并行」。
        if device_id and pre_decision is None and not fast_voice:
            from deskbot_server.service.application.action_decision import decide_turn_actions

            decision_task = asyncio.create_task(
                decide_turn_actions(
                    device_id,
                    _session_user_text(user_text, is_system_round=is_system_round),
                    reply_text,
                    request_id=request_id,
                )
            )
        if is_scheduled:
            need_reply = True  # 定时提醒轮必须开口，禁止静默
        # 剧情/社交主动轮允许静默退出：剧情任务（尤其永续的日常/长期）推进与否由
        # 模型依据 system 里的任务与今日记录判断（不输出任何文字即静默收尾，
        # 下方兜底口播语仅在需要开口且文本为空/汇报腔时启用）。
        # 社交轮额外：meta 汇报语/空文案一律按不开口处理
        # （防「已问候」类汇报语被照字朗读；有动作则走下方静默分支只下发动作）
        if is_social_proactive and _social_tts_looks_like_meta_report(reply_text):
            need_reply = False

        if need_reply and reply_text:
            reply_text, repeated_questions = _suppress_recent_duplicate_questions(
                reply_text, history_messages
            )
            if repeated_questions:
                dialogue_act = "callback"
                logger.info(
                    "[LLM] 已移除近期重复问题 device_id=%s req=%s count=%d",
                    device_id,
                    request_id,
                    repeated_questions,
                )

        # 能进入此处的用户语音已经通过 ASR 文本过滤与注意力门控。模型偶发把用户对
        # 上一问的简短回答误判成无需回复，会造成“问完又装没听见”。普通用户轮除非
        # 明确要求安静，否则强制接住；空回复使用中性确认，不重复追问。
        should_ack_user = (
            not _user_explicitly_requests_silence(user_text)
            and (not _is_backchannel_only(user_text) or _history_ends_with_assistant_question(history_messages))
        )
        if not is_system_round and not need_reply and should_ack_user:
            need_reply = True
            dialogue_act = "empathize"
            if not str(reply_text or "").strip():
                reply_text = "嗯，我听到了。"
            logger.warning(
                "[LLM] 用户有效输入被模型判为静默，已纠正 device_id=%s req=%s user=%r",
                device_id, request_id, (user_text or "")[:80],
            )

        # dialogue_act 是策略观测字段，必须与最终执行决定一致。上面的定时提醒、
        # 社交主动轮过滤和用户输入兜底都可能覆盖模型最初给出的 need_reply。
        if not need_reply:
            dialogue_act = "silent"
        elif dialogue_act == "silent":
            dialogue_act = "answer"

        if parsed.get("volume") is not None and device_id:
            from deskbot_server.pb.servo_pcm import parse_pb_volume

            vol = parse_pb_volume(parsed["volume"])
            if vol is not None:
                device_mapper.update_volume(device_id, vol)

        result.llm_text = reply_text
        result.llm_raw = llm_turn.answer or parsed.get("raw") or ""
        # 语音轮动作/表情随首段口播下发；文本/系统轮由决策任务回填。
        result.scenes = []
        result.moves = pre_moves
        result.anims = pre_anims
        result.tools = llm_turn.tools
        result.tool_results = llm_turn.tool_results
        result.servo = list(parsed.get("servo") or [])
        result.need_reply = need_reply
        result.dialogue_act = dialogue_act
        # 纯文本协议下「有回复」即视为格式正常；空回复才记 false（字段仅为兼容观测保留）
        result.json_ok = bool(parsed.get("json_ok")) or bool(str(reply_text or "").strip())
        result.llm_calls = list(llm_turn.llm_calls or [])
        result.system_prompt = llm_turn.system_prompt
        result.t_llm_end = time.monotonic()

        async def _save_history() -> None:
            if not device_id or not session_id:
                return
            from deskbot_server.service.session_service import append_turn
            from deskbot_server.utils.async_helpers import run_blocking

            # 只归档「说给主人听的那句话」。静默轮（need_reply=false / 纯动作）不写
            # assistant 行：历史里一旦出现沉默样例，模型下一轮读到就会照抄，
            # 形成「读到自己的沉默 → 继续沉默」的自我强化；原始输出里的推理前言
            # 同样必须剥掉，否则历史会被思维链淹没。
            assistant_text = strip_llm_preamble(reply_text or "")
            session_user_text = _session_user_text(user_text, is_system_round=is_system_round)
            try:
                await run_blocking(append_turn, device_id, session_id, session_user_text, assistant_text)
            except Exception:
                logger.exception(
                    "[session] 保存对话失败 device_id=%s session_id=%s req=%s", device_id, session_id, request_id
                )

        if not llm_turn.reply_streamed:
            await _save_history()

        # 用户发起的对话轮识别出已知用户 → 打点该用户「上次对话时间」。
        # 系统前缀轮（定时提醒/剧情推进/主动问候）是机器人单方面开口，不计为对话；
        # LLM 判定无需回复（need_reply=false）的轮次也算对话（用户确实说了话）。
        if device_id and recognized_users and not is_system_round and result.status != "error":
            from deskbot_server.dao.user_social_store import stamp_user_last_talk

            for _name in recognized_users:
                try:
                    stamp_user_last_talk(device_id, _name)
                except Exception:
                    logger.debug(
                        "[LLM] last_talk 打点失败 device_id=%s user=%r", device_id, _name, exc_info=True
                    )

        llm_ms = _ms_between(t_asr_text, result.t_llm_end)
        logger.info(
            "[LLM] 回复 device_id=%s req=%s llm_ms=%s json_ok=%s need_reply=%s json=%s",
            device_id,
            request_id,
            llm_ms,
            parsed["json_ok"],
            need_reply,
            parsed["raw"],
        )
        await downlink.emit_stage(
            "llm_done",
            request_id=request_id,
            send_client=False,
            event_fields={
                "asr_text": user_text,
                "asr_ms": _ms_between(t_asr_start, t_asr_text),
                "llm_text": reply_text,
                "llm_raw": result.llm_raw,
                "llm_ms": llm_ms,
                "llm_calls": result.llm_calls,
                "llm_model": (result.llm_calls[0].get("model") if result.llm_calls else None),
                "system_prompt": result.system_prompt,
                "face_sight": result.face_sight,
                "voice_sight": result.voice_sight,
                "face_ms": result.face_ms,
                "voice_ms": result.voice_ms,
                "source": "asr" if t_asr_start is not None else "text",
            },
        )

        if speech is not None:
            await speech.finish()
            if speech_stream is not None:
                outcome = await speech_stream.finish()
                result.playback_completed = outcome.completed
                result.playback_status = outcome.status.value
                result.playback_reason = outcome.reason
                result.t_tts_end = time.monotonic()
                if not outcome.completed:
                    raise RuntimeError(f"stream playback: {outcome}")
            if llm_turn.reply_streamed:
                await _save_history()
                if speech_audio and request_id and speech_sr and _convo_watching_sync(device_id):
                    ConvoAudioStore().put(device_id, request_id, "tts", b"".join(speech_audio), sample_rate=speech_sr)
                return result
            result.tts_synth_ms = None

        if not parsed["json_ok"] and not str(reply_text or "").strip():
            logger.debug("[LLM] 本轮无口播文本（纯文本协议下的静默）device_id=%s req=%s", device_id, request_id)

        if not need_reply and not is_scheduled:
            # 静默轮也可能有动作/表情：先等决策结果，再决定是否下发 motion-only
            _apply_decision(parsed, result, await _await_decision(decision_task))
            decision_task = None
            has_motion = bool(parsed.get("moves") or parsed.get("anims"))
            if has_motion:
                logger.info(
                    "[LLM] need_reply=false 但有决策动作/表情，下发动作 pb device_id=%s req=%s",
                    device_id,
                    request_id,
                )
                try:
                    await _run_pb_playback(
                        chat,
                        reply_text="",
                        parsed=parsed,
                        llm_scenes=[],
                        request_id=request_id,
                        device_id=device_id,
                        result=result,
                        t_asr_start=t_asr_start,
                        motion_only=True,
                        device_ws=device_ws,
                    )
                except Exception as pb_exc:
                    logger.exception("[LLM] need_reply=false 动作 pb 失败")
                    result.status = "error"
                    result.error = f"motion_pb: {pb_exc}"
                return result
            logger.info("[LLM] need_reply=false，跳过 TTS/pb。device_id=%s req=%s", device_id, request_id)
            result.t_tts_end = time.monotonic()
            return result

        playback_text = (reply_text or "").strip()
        if (is_scheduled or is_quest_proactive) and (
            not playback_text or _scheduled_tts_looks_like_meta_report(playback_text)
        ):
            playback_text = (
                _scheduled_reminder_tts(sched_desc)
                if is_scheduled
                else "主人，来和我一起推进一个小任务好吗？"
            )
            logger.info(
                "[scheduler] 系统轮使用兜底口播语 device_id=%s req=%s tts=%r is_quest=%s",
                device_id, request_id, playback_text, is_quest_proactive,
            )
        if not playback_text:
            # 无口播文本：看决策模型是否给出了动作（有则用占位 TTS 播动作链）
            _apply_decision(parsed, result, await _await_decision(decision_task))
            decision_task = None
            if parsed.get("moves") or parsed.get("anims"):
                playback_text = "。"
                logger.info("[LLM] tts 为空但有决策动作/表情，使用占位 TTS device_id=%s req=%s", device_id, request_id)
            else:
                logger.info("[LLM] tts 为空且无动作/表情，跳过 TTS/pb device_id=%s req=%s", device_id, request_id)
                result.t_tts_end = time.monotonic()
                return result

        await downlink.emit_stage(
            "tts_start",
            request_id=request_id,
            send_client=False,
            event_fields={
                "asr_text": user_text,
                "llm_text": reply_text,
                "tts_text": playback_text,
                "tts_model": tts_model_label(device_id),
                "source": "asr" if t_asr_start is not None else "text",
            },
        )
        try:
            await _run_pb_playback(
                chat,
                reply_text=playback_text,
                parsed=parsed,
                llm_scenes=[],
                request_id=request_id,
                device_id=device_id,
                result=result,
                t_asr_start=t_asr_start,
                auto_face_turn=True,
                prefetch_tts=tts_prefetch.task,
                device_ws=device_ws,
                decision_task=decision_task,
                # 长句按逗号再切短：首段 TTS 更早合成完，开口更快
                tts_chunk_max_chars=16,
            )
            decision_task = None
        except Exception as tts_exc:
            tts_prefetch.cancel()
            logger.exception("TTS 流程失败")
            result.status = "error"
            result.error = f"tts: {tts_exc}"
    except Exception as llm_exc:
        if speech is not None:
            await speech.cancel()
            # Do not replay a fallback over a partially spoken/failed stream.
            if speech.used:
                result.status = "error"
                result.error = f"stream: {llm_exc}"
                result.playback_completed = False
                result.t_tts_end = time.monotonic()
                if device_ws is not None and hasattr(device_ws, "interrupt_current_playback"):
                    await device_ws.interrupt_current_playback(device_id, reason="stream_failed")
                logger.exception("[LLM] 流式口播中断 device_id=%s req=%s", device_id, request_id)
                return result
        logger.exception("LLM 流程失败")
        result.status = "error"
        result.error = f"llm: {llm_exc}"
        # 先停点头再播兜底 TTS，避免点头和摇头重叠
        if on_llm_error is not None:
            try:
                if asyncio.iscoroutinefunction(on_llm_error):
                    await on_llm_error()
                else:
                    on_llm_error()
            except Exception:
                logger.debug("[LLM] on_llm_error 回调异常（忽略）")
        try:
            await _play_llm_error_fallback(
                downlink,
                chat,
                request_id=request_id,
                device_id=device_id,
                result=result,
                device_ws=device_ws,
                t_asr_start=t_asr_start,
                llm_exc=llm_exc,
            )
        except Exception as fallback_exc:
            logger.exception("[LLM] 错误兜底 TTS/pb 失败 device_id=%s req=%s", device_id, request_id)
            result.error = f"llm: {llm_exc}; fallback: {fallback_exc}"
    finally:
        if speech is not None:
            await speech.cancel()
        if tts_prefetch is not None:
            tts_prefetch.cancel()
        for task in interim_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*interim_tasks, return_exceptions=True)
        # 决策任务未被播放路径消费（异常/提前返回）时取消，避免悬挂
        if decision_task is not None and not decision_task.done():
            decision_task.cancel()
            await asyncio.gather(decision_task, return_exceptions=True)

    return result


async def run_device_tts_only(
    downlink: DownlinkPort,
    chat: ChatService,
    text: str,
    *,
    request_id: str | None = None,
    device_id: str | None = None,
    scenes: list | None = None,
    moves: list | None = None,
    anims: list | None = None,
    leading_move_steps: int = 0,
    device_ws: Any | None = None,
    task_level: int = PB_LEVEL_TASK,
) -> ChatTurnResult:
    """跳过 LLM，将给定文本走音素 TTS 并下发 pb；可选在同一条链锁内追加场景 pb 帧。"""
    reply_text = (text or "").strip()
    result = ChatTurnResult()
    result.llm_text = reply_text
    result.t_llm_end = time.monotonic()
    await downlink.emit_stage(
        "tts_start",
        request_id=request_id,
        send_client=False,
        event_fields={"tts_text": reply_text, "tts_model": tts_model_label(device_id), "source": "device_tts"},
    )

    parsed = {
        "reply": reply_text,
        "servo": [],
        "scenes": [],
        "json_ok": True,
        "need_reply": True,
        "raw": reply_text,
        "moves": list(moves or []),
        "anims": list(anims or []),
        "leading_move_steps": max(0, int(leading_move_steps or 0)),
    }
    if not reply_text:
        result.status = "error"
        result.error = "empty text"
        return result
    try:
        scene_list = [str(s).strip() for s in (scenes or []) if isinstance(s, str) and str(s).strip()]
        if parsed["moves"] or parsed["anims"]:
            scene_list = []
        await _run_pb_playback(
            chat,
            reply_text=reply_text,
            parsed=parsed,
            llm_scenes=scene_list,
            request_id=request_id,
            device_id=device_id,
            result=result,
            t_asr_start=result.t_llm_end,
            device_ws=device_ws,
            task_level=task_level,
        )
    except Exception as tts_exc:
        logger.exception("[device_tts] TTS 流程失败 device_id=%s", device_id)
        result.status = "error"
        result.error = f"tts: {tts_exc}"
    return result


async def run_device_playbook(
    downlink: DownlinkPort,
    chat: ChatService,
    playbook: dict,
    *,
    request_id: str | None = None,
    device_id: str | None = None,
    device_ws: Any | None = None,
) -> ChatTurnResult:
    """场景编排：按阶段串行下发（舵机 → 口播前表情 → 口播+并行轨）。"""
    from deskbot_server.service.scene_playbook_runner import playbook_to_phases

    phases = playbook_to_phases(playbook, device_id=device_id)
    if not phases:
        result = ChatTurnResult()
        result.status = "error"
        result.error = "empty playbook"
        return result

    result = ChatTurnResult()
    for pi, phase in enumerate(phases):
        phase_req = f"{request_id}_p{pi}" if request_id and len(phases) > 1 else request_id
        kind = str(phase.get("kind") or "speech")
        if kind == "motion":
            parsed = {
                "reply": "",
                "servo": [],
                "scenes": [],
                "json_ok": True,
                "need_reply": True,
                "raw": "",
                "moves": list(phase.get("moves") or []),
                "anims": list(phase.get("anims") or []),
                "leading_move_steps": 0,
            }
            try:
                await _run_pb_playback(
                    chat,
                    reply_text="",
                    parsed=parsed,
                    llm_scenes=[],
                    request_id=phase_req,
                    device_id=device_id,
                    result=result,
                    t_asr_start=result.t_llm_end or time.monotonic(),
                    motion_only=True,
                    device_ws=device_ws,
                )
            except Exception as exc:
                logger.exception("[scene_playbook] motion phase failed device_id=%s", device_id)
                result.status = "error"
                result.error = f"motion phase: {exc}"
                return result
            continue

        text = str(phase.get("text") or "").strip()
        if not text:
            text = "。"
        turn = await run_device_tts_only(
            downlink,
            chat,
            text,
            request_id=phase_req,
            device_id=device_id,
            scenes=None,
            moves=list(phase.get("moves") or []),
            anims=list(phase.get("anims") or []),
            leading_move_steps=int(phase.get("leading_move_steps") or 0),
            device_ws=device_ws,
        )
        if turn.error:
            result.status = turn.status
            result.error = turn.error
            return result
        result = turn
    return result


async def _send_pb_pairs(
    *,
    pairs: list[tuple[dict, list[bytes]]],
    pb_req: str,
    device_ws: Any,
    device_id: str,
    n_pb: int,
    task_level: int = PB_LEVEL_TASK,
) -> tuple[bool, str | None, str | None]:
    """下发一组 PB 帧，返回 ``(是否中止, 状态, 原因)``。"""
    from deskbot_server.model.pb_seq import PbSeq

    pb_seq = PbSeq.from_wire_pairs(pairs, level=task_level)
    logger.info(
        "[pb TX] enqueue device_id=%s req=%s level=%d blocks=%d",
        device_id, pb_seq.req, pb_seq.level, pb_seq.block_count,
    )
    if hasattr(device_ws, "send_and_wait"):
        outcome = await device_ws.send_and_wait(device_id, pb_seq)
        if not outcome.completed:
            logger.warning(
                "[pb TX] 播放未完成 device_id=%s req=%s status=%s reason=%s",
                device_id, pb_req, outcome.status.value, outcome.reason,
            )
        return not outcome.completed, outcome.status.value, outcome.reason or None
    # 兼容测试桩和第三方 hub：旧接口只能判断是否成功入队。
    success = await device_ws.send(device_id, pb_seq, wait=True)
    if not success:
        logger.error("[pb TX] enqueue 失败 device_id=%s req=%s", device_id, pb_req)
    return not success, "completed" if success else "dropped", None if success else "not_accepted"


def _build_decision_context(
    history_messages: list[dict[str, str]] | None,
    result: ChatTurnResult,
) -> str:
    """决策模型参考上下文：声纹/画面 + 最近几轮对话（各截断，控制 prompt 体积）。"""
    lines: list[str] = []
    if result.voice_sight:
        lines.append(str(result.voice_sight))
    if result.face_sight:
        lines.append(
            str(result.face_sight).replace("\n", "; ")[:300]
        )
    recent: list[str] = []
    for msg in (history_messages or [])[-4:]:
        role = str(msg.get("role") or "")
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        recent.append(f"{'用户' if role == 'user' else '小歪'}：{content[:120]}")
    if recent:
        lines.append("最近对话：\n" + "\n".join(recent))
    return "\n".join(lines)


# 决策动作/表情的 PB 下发任务：持引用防 GC，播完自清（不阻塞 LLM/TTS）
_DECISION_MOTION_TASKS: set[asyncio.Task] = set()


def _spawn_decision_motion(
    chat: ChatService,
    *,
    moves: list[Any],
    anims: list[Any],
    request_id: str | None,
    device_id: str | None,
    device_ws: DeviceWsService | None,
    t_asr_start: float | None,
) -> asyncio.Task | None:
    """决策模型一返回就把动作/表情通过 PB 下发（不等 LLM/TTS）。

    motion-only PB 使用独立 request_id / result，不污染正式语音轮的播放状态；
    先入队可保证设备先做表情/动作，再播随后合成的语音。
    """
    if not moves and not anims:
        return
    parsed = {
        "moves": list(moves),
        "anims": list(anims),
        "servo": [],
        "scenes": [],
        "need_reply": True,
        "raw": "",
    }
    dec_rid = f"{request_id}_dec" if request_id else None

    async def _guarded() -> None:
        try:
            await _run_pb_playback(
                chat,
                reply_text="",
                parsed=parsed,
                llm_scenes=[],
                request_id=dec_rid,
                device_id=device_id,
                result=ChatTurnResult(),
                t_asr_start=t_asr_start,
                motion_only=True,
                device_ws=device_ws,
                interaction_request_id=request_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[decision] 动作/表情 PB 下发失败 device_id=%s req=%s",
                device_id, request_id, exc_info=True,
            )

    task = asyncio.create_task(_guarded())
    _DECISION_MOTION_TASKS.add(task)
    task.add_done_callback(_DECISION_MOTION_TASKS.discard)
    return task


async def _await_decision(task: asyncio.Task | None) -> dict[str, Any] | None:
    """等待决策模型任务；任务异常/取消不阻塞播放（决策失败 = 本轮无显式动作）。"""
    if task is None:
        return None
    try:
        decision = await task
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("[decision] 决策任务异常，本轮跳过动作/表情", exc_info=True)
        return None
    return decision if isinstance(decision, dict) else None


def _apply_decision(
    parsed: dict[str, Any], result: ChatTurnResult, decision: dict[str, Any] | None
) -> None:
    """用决策模型结果覆盖动作/表情；decision=None 表示本轮没有可用决策（不改动）。"""
    if decision is None:
        return
    moves = list(decision.get("moves") or [])
    anims = list(decision.get("anims") or [])
    parsed["moves"] = moves
    parsed["anims"] = anims
    parsed["servo"] = []
    parsed["scenes"] = []
    result.moves = moves
    result.anims = anims


async def _run_pb_playback(
    chat: ChatService,
    *,
    reply_text: str,
    parsed: dict,
    llm_scenes: list,
    request_id: str | None,
    device_id: str | None,
    result: ChatTurnResult,
    t_asr_start: float | None,
    motion_only: bool = False,
    auto_face_turn: bool = False,
    prefetch_tts: asyncio.Task | None = None,
    device_ws: Any | None = None,
    task_level: int = PB_LEVEL_TASK,
    interaction_request_id: str | None = None,
    decision_task: asyncio.Task | None = None,
    tts_chunk_max_chars: int = 0,
    pb_stream: Any | None = None,
) -> None:
    """下发 pb 音频/动作帧。

    ``decision_task``：非 motion 时只采纳首段音频就绪前完成的动作决策，
    较慢的可选动作跳过，不能拖住语音；motion-only 时入口等待动作决策。
    对话模型输出的旧动作字段不会进入这里。
    """
    parsed_eff = parsed
    auto_turn: dict[str, Any] | None = None
    if motion_only and decision_task is not None:
        _apply_decision(parsed, result, await _await_decision(decision_task))
        decision_task = None
    if motion_only:
        sr_pb = int(chat.tts_cfg.get("sample_rate") or 16000)
        segs: list[dict] = []
        from deskbot_server.pb.llm_plan import expand_llm_anims, expand_llm_moves
        from deskbot_server.pb.servo_pcm import _silence_phoneme_seg

        move_steps = expand_llm_moves(list(parsed.get("moves") or []), device_id=device_id)
        anim_frames = expand_llm_anims(list(parsed.get("anims") or []), device_id=device_id)
        if not move_steps and anim_frames:
            total_ms = sum(max(1, int(f.get("ms") or 40)) for f in anim_frames)
            segs = [_silence_phoneme_seg(total_ms, sr_pb)]
        text_chunks = [""]
    else:
        if prefetch_tts is not None:
            text_chunks = [reply_text]
        elif tts_chunk_max_chars > 0:
            # 语音对话：长句按逗号再切短，缩短首段 TTS 合成等待
            text_chunks = split_tts_for_latency(reply_text, max_chars=tts_chunk_max_chars)
        else:
            text_chunks = split_tts_by_punctuation(reply_text)
        if len(text_chunks) > 1:
            logger.info(
                "[TTS] 按标点分 %d 段 device_id=%s req=%s chunks=%s",
                len(text_chunks),
                device_id,
                request_id,
                text_chunks,
            )
        if auto_face_turn:
            # 说话前自动转向：替代已移除的 set_camera_follow LLM 工具——
            # 只要开口说话且画面有人，程序先让机器人转向说话人（同链前导段）。
            from deskbot_server.service.application.interaction_feedback import maybe_speak_face_turn

            turn = maybe_speak_face_turn(device_id, parsed_moves=list(parsed.get("moves") or []))
            if turn is not None:
                auto_turn = turn
                parsed_eff = dict(parsed)
                parsed_eff["moves"] = [turn] + list(parsed.get("moves") or [])
                # Turn toward the speaker while speaking, without a silent
                # leading move segment delaying the first audio.
                parsed_eff["leading_move_steps"] = 0
                logger.info(
                    "[LLM] 说话前自动转向 device_id=%s req=%s step=%s",
                    device_id,
                    request_id,
                    {k: turn.get(k) for k in ("x", "y", "ms")},
                )

    owns_stream = pb_stream is None
    n_scene_pb = 0
    pb_aborted = False
    total_pb = 0
    chunk_is_last = True
    prefetch_tts_task: asyncio.Task | None = prefetch_tts
    # 整轮 TTS 合成 PCM 累积（分 chunk 合成，尾段按序拼接 = 完整口播），供实时对话音频回放
    audio_parts: list[bytes] = []
    audio_parts_sr: int | None = None

    try:
        for chunk_i, chunk_text in enumerate(text_chunks):
            if motion_only:
                segs_local = segs
                sr_pb = int(chat.tts_cfg.get("sample_rate") or 16000)
            else:
                if prefetch_tts_task is None:
                    prefetch_tts_task = asyncio.create_task(chat.tts_phoneme_segments(chunk_text, device_id=device_id))
                sr_pb, segs_local = await prefetch_tts_task
                prefetch_tts_task = None
                result.t_tts_synth_end = time.monotonic()
                pcm_ok = any(len(s.get("pcm") or b"") > 0 for s in segs_local)
                if not segs_local or not pcm_ok:
                    raise RuntimeError(f"phoneme TTS 无分片或无 PCM: {chunk_text!r}")
                if audio_parts_sr is None:
                    audio_parts_sr = int(sr_pb or 16000)
                audio_parts.append(b"".join((s.get("pcm") or b"") for s in segs_local))
                if chunk_i + 1 < len(text_chunks):
                    prefetch_tts_task = asyncio.create_task(
                        chat.tts_phoneme_segments(text_chunks[chunk_i + 1], device_id=device_id)
                    )

            chunk_is_first = chunk_i == 0
            chunk_is_last = chunk_i == len(text_chunks) - 1
            if chunk_is_first and decision_task is not None:
                # Optional post-reply gestures must not hold ready audio while
                # the decision provider is slow/unavailable.
                decision = await _await_decision(decision_task) if decision_task.done() else None
                if not decision_task.done():
                    decision_task.cancel()
                    await asyncio.gather(decision_task, return_exceptions=True)
                decision_task = None
                if decision is not None:
                    parsed_eff = dict(parsed_eff)
                    _apply_decision(parsed_eff, result, decision)
                    if auto_turn is not None:
                        # 自动转向是链首前导段：决策结果不能把它覆盖掉
                        parsed_eff["moves"] = [auto_turn] + list(parsed_eff.get("moves") or [])
            pairs, pb_req, n_pb, sr_pb = build_pb_wire_pairs(
                segs_local,
                chat.tts_cfg,
                servo_plan=list(parsed_eff.get("servo") or []) if chunk_is_first and not parsed_eff.get("moves") else None,
                moves=list(parsed_eff.get("moves") or []) if chunk_is_first else None,
                anims=list(parsed_eff.get("anims") or []) if chunk_is_first else None,
                sample_rate=sr_pb,
                request_id=(f"{request_id}_{chunk_i}" if request_id and len(text_chunks) > 1 else request_id),
                # 随机微动作对**每个**分句都传：它本来就是「说话时的细碎动作」，只在首句
                # 生效会让长回答的后半段变成静止画面（LLM 的 moves/anims 同样只在首句）。
                # 函数内部会跳过已带 servo 的分片，不会与 LLM 指定的动作叠加。
                random_servo_cfg=chat.settings.pb_random_servo_cfg(),
                volume=parsed_eff.get("volume") if chunk_is_first else None,
                device_id=device_id,
                # Application turns are serialized by DeviceInteractionArbiter.  APPEND
                # prevents ordinary same-priority speech from cutting a sentence; the
                # explicit user-barge-in path clears playback separately.
                action=PB_ACTION_APPEND,
                leading_move_steps=int(parsed_eff.get("leading_move_steps") or 0) if chunk_is_first else 0,
            )
            total_pb += n_pb
            frame_overview = [
                {
                    "i": i,
                    "type": m.get("type"),
                    "idx": m.get("idx"),
                    "chunk_ms": m.get("chunk_ms"),
                    "anim_n": len(m.get("anim") or []),
                    "phonemes": [
                        str(x.get("phoneme")) for x in (m.get("anim") or []) if isinstance(x, dict) and x.get("phoneme")
                    ],
                    "action": m.get("action"),
                    "bin_bytes": sum(len(b) for b in bins),
                }
                for i, (m, bins) in enumerate(pairs)
            ]
            logger.info(
                "[pb TX] 段 %d/%d TTS=%r pb_req=%s segments=%d sr=%s",
                chunk_i + 1,
                len(text_chunks),
                chunk_text,
                pb_req,
                n_pb,
                sr_pb,
            )
            logger.debug("[pb TX] 帧序一览 %s", json.dumps(frame_overview, ensure_ascii=False))

            interaction_arbiter.mark_current_speaking(
                device_id or "", request_id=interaction_request_id or request_id
            )
            if pb_stream is None and not motion_only and callable(getattr(device_ws, "open_speech_stream", None)):
                pb_stream = await device_ws.open_speech_stream(
                    device_id, pb_req, sr=sr_pb, fmt=str(chat.tts_cfg.get("output_codec") or "s16le"), level=task_level,
                )
            if not motion_only and callable(getattr(device_ws, "note_spoken_text", None)):
                device_ws.note_spoken_text(device_id, pb_stream.seq.req if pb_stream else pb_req, reply_text)
            if pb_stream is not None:
                await pb_stream.append(pairs)
                pb_aborted, playback_status, playback_reason = False, "streaming", None
            else:
                pb_aborted, playback_status, playback_reason = await _send_pb_pairs(
                    pairs=pairs, pb_req=pb_req, device_ws=device_ws, device_id=device_id, n_pb=n_pb, task_level=task_level,
                )
            result.playback_completed = None if pb_stream is not None else not pb_aborted
            result.playback_status = playback_status
            result.playback_reason = playback_reason
            if pb_aborted:
                if prefetch_tts_task is not None:
                    prefetch_tts_task.cancel()
                break

    except BaseException:
        if owns_stream and pb_stream is not None and pb_stream.seq.outcome is None:
            await device_ws.interrupt_current_playback(device_id, reason="playback_producer_failed", expected_req=pb_stream.seq.req)
        raise
    finally:
        if prefetch_tts_task is not None and not prefetch_tts_task.done():
            prefetch_tts_task.cancel()
            await asyncio.gather(prefetch_tts_task, return_exceptions=True)

    if pb_stream is not None and owns_stream:
        outcome = await pb_stream.finish()
        pb_aborted = not outcome.completed
        result.playback_completed = outcome.completed
        result.playback_status = outcome.status.value
        result.playback_reason = outcome.reason

    if not pb_aborted and chunk_is_last:
        for sc_name in llm_scenes:
            if not isinstance(sc_name, str):
                continue
            sc_key = sc_name.strip()
            if not sc_key or _pb_scene_entry_by_name({}, sc_key, device_id=device_id) is None:
                if sc_key:
                    logger.warning(
                        "[pb TX] LLM scenes 跳过未知场景 %r device_id=%s req=%s", sc_key, device_id, request_id
                    )
                continue
            sreq = uuid.uuid4().hex[:16]
            sframes = _prepare_pb_scene_chain_frames(sc_key, runtime_req=sreq, device_id=device_id)
            if not sframes:
                continue
            from deskbot_server.model.pb_seq import PbSeq
            scene_seq = PbSeq.from_wire_pairs([(frame, []) for frame in sframes], level=PB_LEVEL_DEBUG)
            await device_ws.send(device_id, scene_seq)
            n_scene_pb += scene_seq.block_count

    # 仅后台订阅期间留存本轮音频，供实时对话回放。
    if audio_parts and request_id and device_id and audio_parts_sr and _convo_watching_sync(device_id):
        ConvoAudioStore().put(device_id, request_id, "tts", b"".join(audio_parts), sample_rate=audio_parts_sr)

    logger.info(
        "[pb TX] 下发结束 device_id=%s request_id=%s 语音 JSON=%d%s%s",
        device_id,
        request_id,
        total_pb,
        "（已中止）" if pb_aborted else "",
        f"；LLM scenes 追加 {n_scene_pb} 条" if n_scene_pb else "",
    )
    result.t_tts_end = time.monotonic()


async def publish_chat_turn(
    events: PipelineEventsPort,
    device_id: str | None,
    *,
    source: str,
    asr_text: str | None,
    t_asr_start: float | None,
    t_asr_text: float | None,
    turn: ChatTurnResult,
    request_id: str | None = None,
) -> None:
    if not device_id:
        return
    flow = turn.as_dict()
    t_llm_end = flow.get("t_llm_end")
    t_tts_synth_end = flow.get("t_tts_synth_end")
    t_tts_end = flow.get("t_tts_end")
    end_t = t_tts_end or t_llm_end or t_asr_text
    llm_calls = list(flow.get("llm_calls") or [])
    tts_ms_val = flow.get("tts_synth_ms")
    if tts_ms_val is None:
        tts_ms_val = _ms_between(t_llm_end, t_tts_synth_end)
    tts_done = tts_ms_val is not None
    store = ConvoAudioStore()
    evt = {
        "device_id": device_id,
        "request_id": request_id,
        "asr_text": asr_text,
        "asr_ms": _ms_between(t_asr_start, t_asr_text) if source == "asr" else None,
        "asr_model": asr_model_label(device_id) if source == "asr" else None,
        "audio_asr": bool(request_id) and source == "asr" and store.has(device_id, request_id, "asr"),
        "face_img": bool(request_id) and source == "asr" and store.has(device_id, request_id, "face"),
        "face_sight": flow.get("face_sight"),
        "voice_sight": flow.get("voice_sight"),
        "face_ms": flow.get("face_ms"),
        "voice_ms": flow.get("voice_ms"),
        "llm_calls": llm_calls,
        "llm_model": (llm_calls[0].get("model") if llm_calls else None),
        "system_prompt": flow.get("system_prompt"),
        "llm_text": flow.get("llm_text"),
        "llm_raw": flow.get("llm_raw"),
        "moves": list(flow.get("moves") or []),
        "anims": list(flow.get("anims") or []),
        "tools": list(flow.get("tools") or []),
        "tool_results": list(flow.get("tool_results") or []),
        "scenes": list(flow.get("scenes") or []),
        "json_ok": bool(flow.get("json_ok")),
        "need_reply": bool(flow.get("need_reply", True)),
        "dialogue_act": str(flow.get("dialogue_act") or "answer"),
        "decision_valid": flow.get("decision_valid"),
        "decision_respond": flow.get("decision_respond"),
        "decision_model": flow.get("decision_model"),
        "decision_ms": flow.get("decision_ms"),
        "decision_error": flow.get("decision_error"),
        "voice_auto_reply_off": bool(flow.get("voice_auto_reply_off")),
        "llm_ms": _ms_between(t_asr_text, t_llm_end),
        "llm_first_token_ms": _ms_between(t_asr_text, flow.get("t_llm_first_token")),
        "first_audio_ready_ms": _ms_between(t_asr_text, flow.get("t_tts_first_audio")),
        "tts_text": flow.get("llm_text"),
        "tts_ms": tts_ms_val,
        "tts_model": tts_model_label(device_id) if tts_done else None,
        "audio_tts": bool(request_id) and tts_done and store.has(device_id, request_id, "tts"),
        "pb_ms": _ms_between(t_tts_synth_end, t_tts_end),
        "playback_completed": flow.get("playback_completed"),
        "playback_status": flow.get("playback_status"),
        "playback_reason": flow.get("playback_reason"),
        "e2e_ms": _ms_between(t_asr_start, end_t),
        "status": flow.get("status") or "ok",
        "error": flow.get("error"),
        "source": source,
    }
    await events.publish_turn(evt)
    await events.touch_device(device_id, evt["status"])
