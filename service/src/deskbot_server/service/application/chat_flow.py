from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from deskbot_server.dao import device_mapper
from deskbot_server.dao.device_mapper import get_auto_reply
from deskbot_server.infrastructure.llm.runtime import native_tools_enabled
from deskbot_server.infrastructure.llm.utils import build_llm_user_message, parse_llm_reply, recognized_known_users
from deskbot_server.infrastructure.tts.text_split import split_tts_by_punctuation
from deskbot_server.model.chat import ChatTurnResult, LlmTurnResult
from deskbot_server.pb.scenes import _pb_scene_entry_by_name, _prepare_pb_scene_chain_frames
from deskbot_server.pb.shapes import PB_ACTION_APPEND, PB_ACTION_REPLACE, PB_LEVEL_TASK
from deskbot_server.pb.wire import build_pb_wire_pairs
from deskbot_server.ports.downlink import DownlinkPort, PipelineEventsPort
from deskbot_server.service.application.capability_labels import asr_model_label, llm_model_label, tts_model_label
from deskbot_server.service.application.convo_audio_store import ConvoAudioStore
from deskbot_server.service.application.llm_error_fallback import (
    build_llm_error_fallback_plan,
    start_llm_error_motion_feedback,
    stop_llm_error_motion_feedback,
)
from deskbot_server.service.application.llm_tool_runner import execute_llm_tools
from deskbot_server.service.application.tool_interim_tts import build_tool_interim_tts
from deskbot_server.utils.util import _ms_between

if TYPE_CHECKING:
    from deskbot_server.service.application.chat_service import ChatService
    from deskbot_server.service.device_ws_service import DeviceWsService

logger = logging.getLogger("deskbot-server")

_SCHEDULED_TASK_PREFIX = "[系统定时任务]"
_QUEST_PROACTIVE_PREFIX = "[系统剧情推进]"
# 社交主动问候轮：不进 _SYSTEM_INITIATED_PREFIXES —— 不强制开口，
# LLM 判定此刻无话可说时可 need_reply=false 静默退出（区别于剧情/定时轮必须口播）
_SOCIAL_PROACTIVE_PREFIX = "[系统主动问候]"
_SYSTEM_INITIATED_PREFIXES = (_SCHEDULED_TASK_PREFIX, _QUEST_PROACTIVE_PREFIX)
_ALL_SYSTEM_PREFIXES = _SYSTEM_INITIATED_PREFIXES + (_SOCIAL_PROACTIVE_PREFIX,)


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


def _extract_voice_sight_lines(user_message: str) -> str | None:
    """从装配好的 user 消息中抽取「声音识别」段（无该段 → None）。

    与 face_sight 同理：实验台气泡与 prompt 共用同一次装配。
    """
    lines = (user_message or "").splitlines()
    out: list[str] = []
    seen = False
    for line in lines:
        s = line.strip()
        if not seen:
            if s.startswith("声音识别:"):
                seen = True
                out.append(s)
            continue
        if s.startswith("name=") or s.startswith("(未识别出已知说话人)"):
            out.append(s)
            continue
        break  # 声音识别段结束（下一条是 ] 或空行）
    return "\n".join(out) if out else None


MAX_LLM_TOOL_ROUNDS = 8

# 会话上下文策略（run_chat_turn 组装 history_messages）：
# - 相邻轮次间隔超过该秒数 → 停止向上追溯（丢弃更旧段）
_HISTORY_MAX_GAP_SECONDS = 5 * 60
# - 历史消息累计 token 达到当前 LLM 上下文一半 → 停止追溯。
#   窗口取设备 llm_param.context_window；未配置时回退该默认
#   （本地引擎 llm-qwen/llm-minicpm 的 n_ctx 被 n_ctx_train 钉死在 8192 → 一半 4096）
_DEFAULT_LLM_CTX_TOKENS = 8192


def _history_token_budget(device_id: str | None) -> int:
    """当前 LLM 上下文窗口一半作为历史 token 预算（保近弃远）。"""
    cw: int | None = None
    did = str(device_id or "").strip()
    if did:
        try:
            from deskbot_server.infrastructure.llm.runtime import resolve_llm_config

            cw = resolve_llm_config(did).context_window
        except Exception:
            cw = None
    if not cw:
        cw = _DEFAULT_LLM_CTX_TOKENS
    return max(1024, int(cw) // 2)

_CAPTURE_TOOLS = frozenset({"capture_camera", "get_camera_frame", "camera_capture"})
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


def _tools_need_camera(tools: list[dict[str, Any]]) -> bool:
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or raw.get("name") or "").strip()
        if tool in _CAPTURE_TOOLS:
            return True
    return False


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
    from deskbot_server.infrastructure.llm.utils import estimate_text_tokens

    keep: list[dict[str, Any]] = []
    total = 0
    for row in reversed(rows):
        cost = estimate_text_tokens(str(row.get("content") or "")) + 4
        if keep and total + cost > token_budget:
            break
        keep.append(row)
        total += cost
    keep.reverse()
    return [{"role": str(r["role"]), "content": str(r["content"])} for r in keep]


def build_llm_tool_followup_message(tool_results: list[dict[str, Any]]) -> str:
    """工具执行后反馈给 LLM 的 user 消息。"""
    slim = [_tool_result_for_llm(r) for r in tool_results]
    payload = json.dumps(slim, ensure_ascii=False)
    return (
        "[工具执行结果]\n"
        f"{payload}\n\n"
        "请根据结果继续。若还需调用工具，请输出 JSON 且 ``tools`` 非空，"
        "并在 ``tts`` 写一句口语化过渡语（如「稍等，我帮你查一下」）以便立刻播报；"
        "若已完成，请输出最终 JSON，``tools`` 写 [] 并填写 ``tts`` 等字段。"
    )


async def _execute_tools_round(
    tools: list[dict[str, Any]],
    *,
    device_id: str,
    session_id: str | None,
    device_ws: DeviceWsService | None,
) -> list[dict[str, Any]]:
    """执行一轮工具（相机帧率由固件默认策略决定，无 cam_fps 下发/提升）。"""
    return await execute_llm_tools(tools, device_id=device_id, session_id=session_id, device_ws=device_ws)


async def _complete_llm_native_rounds(
    chat: ChatService,
    user_text: str,
    *,
    device_id: str | None,
    session_id: str | None,
    device_context: str | None,
    history_messages: list[dict[str, str]] | None,
    request_id: str | None,
    pipeline_source: str | None,
    device_ws: DeviceWsService | None,
    tts_prefetch: _TtsPrefetch | None,
    on_interim_tts_play: Callable[[str, int], Awaitable[None]] | None,
    bus_service: Any | None,
    user_message_override: str | None,
) -> LlmTurnResult:
    """原生 function calling 多轮：每轮 tools=原生 schema，执行结果以 role=tool 回灌。

    收尾语义：模型某轮无 tool_calls → content 即最终 JSON envelope，直接结束；
    content 非 JSON/为空 → 走 legacy 文本路径收口一次（自带 JSON 重试/纯文本包装兜底）。
    """
    from deskbot_server.infrastructure.llm.runtime import native_tools_enabled
    from deskbot_server.infrastructure.llm.tool_schema import build_native_tool_schemas

    extra_messages: list[dict[str, Any]] = []
    all_tools: list[dict[str, Any]] = []
    all_tool_results: list[dict[str, Any]] = []
    llm_calls: list[dict[str, Any]] = []
    answer = ""
    parsed: dict[str, Any] = parse_llm_reply("")
    system_prompt: str | None = None
    captured_system_prompt = False
    llm_model = llm_model_label(device_id)
    native_schemas = build_native_tool_schemas(device_id=device_id)

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
            result = await chat.llm_tool_round(
                user_text,
                tools=native_schemas,
                tool_choice="auto",
                **_base_kwargs(round_idx),
            )
        except Exception as llm_exc:
            llm_calls.append({
                "n": round_idx + 1, "model": llm_model,
                "ms": int((time.monotonic() - round_t0) * 1000),
                "text": f"[调用失败] {llm_exc}", "truncated": False,
            })
            raise
        content = str(result.content or "")
        calls = list(result.tool_calls or [])
        summary = content or (
            f"[tool_calls: {', '.join(str(c.get('name') or '') for c in calls)}]" if calls else ""
        )
        llm_calls.append({
            "n": round_idx + 1, "model": llm_model,
            "ms": int((time.monotonic() - round_t0) * 1000),
            "text": summary[:3000], "truncated": len(summary) > 3000,
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
            interim_text = build_tool_interim_tts(native_tools)
            if interim_text and on_interim_tts_play is not None:
                await on_interim_tts_play(interim_text, round_idx + 1)
            tool_results = await _execute_tools_round(
                native_tools, device_id=str(device_id), session_id=session_id, device_ws=device_ws
            )
            all_tool_results.extend(tool_results)
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

        # 模型收尾：无工具调用
        if content:
            answer = content
            parsed = parse_llm_reply(content)
            if parsed.get("json_ok"):
                break
            logger.warning(
                "[LLM] native 收尾轮非 JSON，走文本收口 device_id=%s req=%s preview=%r",
                device_id, request_id, content[:120],
            )
        else:
            logger.warning("[LLM] native 轮空内容无调用，走文本收口 device_id=%s req=%s", device_id, request_id)
        answer = await _legacy_final_round()
        parsed = parse_llm_reply(answer)
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
    )


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
) -> LlmTurnResult:
    """多轮 LLM：有 tools 则执行并继续，无 tools 则返回最终 parsed。

    ``history_messages``：会话上下文（调用方已按「5 分钟间隔 + 半量 token
    预算」裁剪），只注入首轮；工具追加轮沿用 extra_messages。
    ``user_message_override``：语音轮由应用层在 asr 完成时一次性装配的
    user 消息全文（含该时刻的人脸识别），整轮各次 LLM 调用锁定使用，
    保证 prompt 与实验台气泡展示的识别内容同源。

    返回 ``LlmTurnResult(parsed, tools, tool_results, answer, system_prompt)``；
    ``system_prompt`` 为每轮 LLM 调用构建的 system prompt（取首轮即主轮）。

    设备开启原生 function calling（``devices.llm_param.native_tools`` / config
    ``llm.native_tools``）时，委托 ``_complete_llm_native_rounds`` 走原生 tools 通道。
    """
    if device_id and native_tools_enabled(device_id):
        return await _complete_llm_native_rounds(
            chat,
            user_text,
            device_id=device_id,
            session_id=session_id,
            device_context=device_context,
            history_messages=history_messages,
            request_id=request_id,
            pipeline_source=pipeline_source,
            device_ws=device_ws,
            tts_prefetch=tts_prefetch,
            on_interim_tts_play=on_interim_tts_play,
            bus_service=bus_service,
            user_message_override=user_message_override,
        )
    extra_messages: list[dict[str, str]] = []
    all_tools: list[dict[str, Any]] = []
    all_tool_results: list[dict[str, Any]] = []
    llm_calls: list[dict[str, Any]] = []
    answer = ""
    parsed: dict[str, Any] = parse_llm_reply("")
    system_prompt: str | None = None
    captured_system_prompt = False
    llm_model = llm_model_label(device_id)

    def _on_system_prompt(content: str) -> None:
        nonlocal system_prompt, captured_system_prompt
        if not captured_system_prompt:
            system_prompt = content
            captured_system_prompt = True

    for round_idx in range(MAX_LLM_TOOL_ROUNDS):
        round_t0 = time.monotonic()
        try:
            llm_kwargs = {
                "device_context": device_context if round_idx == 0 else None,
                "device_id": device_id,
                "history_messages": history_messages if round_idx == 0 else None,
                "extra_messages": extra_messages or None,
                "on_tts_ready": tts_prefetch.on_ready if tts_prefetch is not None else None,
                "on_system_prompt": _on_system_prompt,
            }
            if user_message_override:
                # 整轮锁定同一份装配好的 user 消息（仅语音轮提供），避免各轮现读快照漂移
                llm_kwargs["user_message_override"] = user_message_override
            answer = await chat.llm(user_text, **llm_kwargs)
        except Exception as llm_exc:
            llm_calls.append({
                "n": round_idx + 1,
                "model": llm_model,
                "ms": int((time.monotonic() - round_t0) * 1000),
                "text": f"[调用失败] {llm_exc}",
                "truncated": False,
            })
            raise
        answer = str(answer or "")
        llm_calls.append({
            "n": round_idx + 1,
            "model": llm_model,
            "ms": int((time.monotonic() - round_t0) * 1000),
            "text": answer[:3000],
            "truncated": len(answer) > 3000,
        })
        parsed = parse_llm_reply(answer)
        tools = list(parsed.get("tools") or [])

        if not tools:
            break

        if not device_id:
            logger.warning(
                "[LLM] tools 无 device_id，无法执行 device_id=%s req=%s tools=%s", device_id, request_id, tools
            )
            break

        interim_text = (parsed.get("reply") or "").strip()
        if interim_text:
            # LLM 已给出完整回复，reply 即最终结果，不再继续调用 LLM
            all_tools.extend(tools)
            tool_results = await _execute_tools_round(
                tools, device_id=str(device_id), session_id=session_id, device_ws=device_ws
            )
            all_tool_results.extend(tool_results)
            break
        interim_text = build_tool_interim_tts(tools)
        if interim_text:
            logger.info(
                "[LLM] tool 轮兜底过渡 TTS device_id=%s req=%s text=%r", device_id, request_id, interim_text[:80]
            )

        # 拍照须先拿到帧再播过渡 TTS（播报期间固件暂停 camera 上行）
        if _tools_need_camera(tools):
            if interim_text and tts_prefetch is not None:
                tts_prefetch.cancel()
            tool_results = await _execute_tools_round(
                tools, device_id=str(device_id), session_id=session_id, device_ws=device_ws
            )
            if interim_text and on_interim_tts_play is not None:
                await on_interim_tts_play(interim_text, round_idx + 1)
        else:
            play_coro = None
            if interim_text and on_interim_tts_play is not None:
                play_coro = on_interim_tts_play(interim_text, round_idx + 1)
            elif interim_text and tts_prefetch is not None:
                tts_prefetch.cancel()

            tool_coro = _execute_tools_round(
                tools, device_id=str(device_id), session_id=session_id, device_ws=device_ws
            )
            if play_coro is not None:
                tool_results, _ = await asyncio.gather(tool_coro, play_coro)
            else:
                tool_results = await tool_coro

        all_tools.extend(tools)
        all_tool_results.extend(tool_results)
        logger.info(
            "[LLM] tool round=%d device_id=%s req=%s tools=%s results=%s",
            round_idx + 1,
            device_id,
            request_id,
            tools,
            [_tool_result_for_llm(r) for r in tool_results],
        )
        if bus_service is not None and device_id and request_id:
            tool_names = [str(t.get("tool") or "").strip() for t in tools if str(t.get("tool") or "").strip()]
            await bus_service.pub(device_id, {
                "request_id": request_id,
                "source": pipeline_source or "asr",
                "asr_text": user_text,
                "stage": f"llm_tool_{round_idx + 1}",
                "status": "running",
                "llm_text": (f"执行工具: {', '.join(tool_names)}" if tool_names else "执行工具"),
            })
        extra_messages.append({"role": "assistant", "content": answer})
        extra_messages.append({"role": "user", "content": build_llm_tool_followup_message(tool_results)})
    else:
        logger.warning("[LLM] tool 循环达到上限 %d device_id=%s req=%s", MAX_LLM_TOOL_ROUNDS, device_id, request_id)

    return LlmTurnResult(
        parsed=parsed,
        tools=all_tools,
        tool_results=all_tool_results,
        answer=answer,
        system_prompt=system_prompt,
        llm_calls=llm_calls,
    )


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
    """在已有用户侧文本后执行 LLM + TTS/pb 管道（应用层，不依赖 WebSocket 类型）。"""
    result = ChatTurnResult()
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
                user_text, device_id=device_id, device_context=ack_ctx
            )
            result.face_sight = _extract_face_sight_lines(voice_user_message)
            result.voice_sight = _extract_voice_sight_lines(voice_user_message)
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
            from deskbot_server.dao.device_session_mapper import ensure_active_session, session_context_window
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

        tts_prefetch = _TtsPrefetch(chat, device_id=device_id)

        async def _on_interim_tts_play(text: str, round_idx: int) -> None:
            await _play_interim_tts(
                downlink, chat, text, tts_prefetch, request_id=request_id, device_id=device_id, round_idx=round_idx,
                device_ws=device_ws,
            )

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
        )
        parsed = llm_turn.parsed

        reply_text = parsed["reply"]
        llm_scenes = list(parsed.get("scenes") or [])
        llm_moves = list(parsed.get("moves") or [])
        llm_anims = list(parsed.get("anims") or [])
        need_reply = bool(parsed.get("need_reply", True))
        if is_scheduled or is_quest_proactive:
            need_reply = True  # 系统发起轮必须开口，禁止静默
        # 社交主动问候轮允许静默退出：meta 汇报语/空文案一律按不开口处理
        # （防「已问候」类汇报语被照字朗读；有动作则走下方静默分支只下发动作）
        if is_social_proactive and _social_tts_looks_like_meta_report(reply_text):
            need_reply = False

        if parsed.get("volume") is not None and device_id:
            from deskbot_server.pb.servo_pcm import parse_pb_volume

            vol = parse_pb_volume(parsed["volume"])
            if vol is not None:
                device_mapper.update_volume(device_id, vol)

        result.llm_text = reply_text
        result.llm_raw = llm_turn.answer or parsed.get("raw") or ""
        result.scenes = llm_scenes
        result.moves = llm_moves
        result.anims = llm_anims
        result.tools = llm_turn.tools
        result.tool_results = llm_turn.tool_results
        result.servo = list(parsed.get("servo") or [])
        result.need_reply = need_reply
        result.json_ok = parsed["json_ok"]
        result.llm_calls = list(llm_turn.llm_calls or [])
        result.system_prompt = llm_turn.system_prompt
        result.t_llm_end = time.monotonic()

        if device_id and session_id:
            from deskbot_server.dao.device_session_mapper import append_turn
            from deskbot_server.utils.async_helpers import run_blocking

            # 会话归档用整轮最终输出（含 legacy 原始文本）；llm_turn.answer 兜底
            # 空 tts 轮（静默/纯动作），避免引用作用域外的局部变量
            assistant_text = (reply_text or "").strip() or (llm_turn.answer or "").strip()
            try:
                await run_blocking(append_turn, device_id, session_id, user_text, assistant_text)
            except Exception:
                logger.exception(
                    "[session] 保存对话失败 device_id=%s session_id=%s req=%s", device_id, session_id, request_id
                )

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
                "source": "asr" if t_asr_start is not None else "text",
            },
        )

        if not parsed["json_ok"]:
            logger.warning("[LLM] 输出未通过 JSON 解析，按整段文本走 TTS。device_id=%s req=%s", device_id, request_id)

        if not need_reply and not (is_scheduled or is_quest_proactive):
            has_motion = bool(llm_moves or llm_anims)
            if has_motion:
                logger.info(
                    "[LLM] need_reply=false 但有 moves/anims/屏幕内容，下发动作 pb device_id=%s req=%s",
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
            if llm_moves or llm_anims:
                playback_text = "。"
                logger.info("[LLM] tts 为空但有 moves/anims，使用占位 TTS device_id=%s req=%s", device_id, request_id)
            else:
                logger.info("[LLM] tts 为空且无 moves/anims，跳过 TTS/pb device_id=%s req=%s", device_id, request_id)
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
                llm_scenes=llm_scenes if not llm_anims else [],
                request_id=request_id,
                device_id=device_id,
                result=result,
                t_asr_start=t_asr_start,
                auto_face_turn=True,
                prefetch_tts=tts_prefetch.task,
                device_ws=device_ws,
            )
        except Exception as tts_exc:
            tts_prefetch.cancel()
            logger.exception("TTS 流程失败")
            result.status = "error"
            result.error = f"tts: {tts_exc}"
    except Exception as llm_exc:
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
) -> bool:
    """下发一组 pb wire 帧。经 DeviceWsService 消息队列统一调度，返回是否因失败而中止。"""
    from deskbot_server.model.pb_seq import PbSeq

    pb_seq = PbSeq.from_wire_pairs(pairs, level=task_level)
    logger.info(
        "[pb TX] enqueue device_id=%s req=%s level=%d blocks=%d",
        device_id, pb_seq.req, pb_seq.level, pb_seq.block_count,
    )
    success = await device_ws.send(device_id, pb_seq, wait=True)
    if not success:
        logger.error("[pb TX] enqueue 失败 device_id=%s req=%s", device_id, pb_req)
    return not success


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
) -> None:
    """下发 pb 音频/动作帧。

    ``auto_face_turn``：说话链开启"说话前自动转向"（画面有人且无朝向表达时，
    把转向 __custom__ 步前插为链首前导舵机段，转向完成后才开始口播）。
    """
    parsed_eff = parsed
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
                parsed_eff = dict(parsed)
                parsed_eff["moves"] = [turn] + list(parsed.get("moves") or [])
                parsed_eff["leading_move_steps"] = max(1, int(parsed.get("leading_move_steps") or 0))
                logger.info(
                    "[LLM] 说话前自动转向 device_id=%s req=%s step=%s",
                    device_id,
                    request_id,
                    {k: turn.get(k) for k in ("x", "y", "ms")},
                )

    n_scene_pb = 0
    pb_aborted = False
    total_pb = 0
    chunk_is_last = True
    prefetch_tts_task: asyncio.Task | None = prefetch_tts
    # 整轮 TTS 合成 PCM 累积（分 chunk 合成，尾段按序拼接 = 完整口播），供实时对话音频回放
    audio_parts: list[bytes] = []
    audio_parts_sr: int | None = None

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
        pairs, pb_req, n_pb, sr_pb = build_pb_wire_pairs(
            segs_local,
            chat.tts_cfg,
            servo_plan=list(parsed_eff.get("servo") or []) if chunk_is_first and not parsed_eff.get("moves") else None,
            moves=list(parsed_eff.get("moves") or []) if chunk_is_first else None,
            anims=list(parsed_eff.get("anims") or []) if chunk_is_first else None,
            sample_rate=sr_pb,
            request_id=(f"{request_id}_{chunk_i}" if request_id and len(text_chunks) > 1 else request_id),
            random_servo_cfg=chat.settings.pb_random_servo_cfg() if chunk_is_first else None,
            volume=parsed_eff.get("volume") if chunk_is_first else None,
            device_id=device_id,
            action=PB_ACTION_REPLACE if chunk_is_first else PB_ACTION_APPEND,
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

        pb_aborted = await _send_pb_pairs(
            pairs=pairs, pb_req=pb_req, device_ws=device_ws, device_id=device_id, n_pb=n_pb, task_level=task_level,
        )
        if pb_aborted:
            if prefetch_tts_task is not None:
                prefetch_tts_task.cancel()
            break

    if prefetch_tts_task is not None:
        prefetch_tts_task.cancel()

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
            from deskbot_server.model.pb_seq import PbBlock, PbSeq
            scene_blocks = [PbBlock.from_wire(f) for f in sframes]
            scene_seq = PbSeq(req=sreq, entries=tuple(scene_blocks), level=PB_LEVEL_DEBUG)
            await device_ws.send(device_id, scene_seq)
            n_scene_pb += len(scene_blocks)

    if audio_parts and request_id and device_id and audio_parts_sr:
        # 存入内存音频仓库，供实验台实时对话按 request_id 回放（失败不影响主流程）；
        # 采集门控：仅当后台有订阅者查看该设备实时对话时才留存（无人查看不采集）
        if _convo_watching_sync(device_id):
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
        "voice_auto_reply_off": bool(flow.get("voice_auto_reply_off")),
        "llm_ms": _ms_between(t_asr_text, t_llm_end),
        "tts_text": flow.get("llm_text"),
        "tts_ms": tts_ms_val,
        "tts_model": tts_model_label(device_id) if tts_done else None,
        "audio_tts": bool(request_id) and tts_done and store.has(device_id, request_id, "tts"),
        "pb_ms": _ms_between(t_tts_synth_end, t_tts_end),
        "e2e_ms": _ms_between(t_asr_start, end_t),
        "status": flow.get("status") or "ok",
        "error": flow.get("error"),
        "source": source,
    }
    await events.publish_turn(evt)
    await events.touch_device(device_id, evt["status"])
