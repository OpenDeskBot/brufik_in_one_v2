"""剧本主动推进：设备冷场 ≥1 分钟且用户在面前时，发起一轮主动对话推进剧情。

由 LiveService（on_face_tick 冷场判定）调度调用。Quest 当前 running 任务按
device_id 自动注入每轮对话的 system prompt（infrastructure/llm/utils.py 的
llm_quest_tasks 附录 + 该用户记录），因此这里只需像定时任务
（ScheduledTaskScheduler）一样发起一轮 run_chat_turn —— LLM 会看到任务定义与
用户今日记录，自主选择开口引导主人配合并调 complete_task 收尾记账，或在该推进的
事已做完/无新进展时 need_reply=false 静默收尾。

user_text 以 ``_QUEST_PROACTIVE_PREFIX`` 开头：chat_flow 据此把它当作系统发起轮
（不强制开口，允许静默；need_reply=true 且 tts 为「已发送/已汇报」类 meta 文案时
兜底成面向主人的口播语）。LiveService 在每次 attempt 后落冷却，防高频骚扰。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter, WsPipelineEventsAdapter
from deskbot_server.service.application.chat_flow import (
    _QUEST_PROACTIVE_PREFIX,
    _voice_was_played,
    publish_chat_turn,
    run_chat_turn,
)
from deskbot_server.service.quest_service import QuestService

if TYPE_CHECKING:
    from deskbot_server.service.application.chat_service import ChatService
    from deskbot_server.service.bus_service import BusService
    from deskbot_server.service.device_ws_service import DeviceWsService

logger = logging.getLogger("deskbot-server")



class QuestProactiveRunner:
    """剧本主动推进器：对设备发起一轮「剧情推进」对话。

    ``attempt(device_id) -> bool`` 返回是否已发起尝试（False = 无可推进任务 /
    设备离线，由调用方做空转冷却）；内部异常一律兜底并记日志，不向调度层上抛。
    """

    def __init__(
        self,
        *,
        chat: ChatService,
        device_ws: DeviceWsService,
        bus_service: BusService | None = None,
    ) -> None:
        self._chat = chat
        self._device_ws = device_ws
        self._bus_service = bus_service

    async def attempt(self, device_id: str) -> bool:
        dev = str(device_id or "").strip()
        if not dev:
            return False
        try:
            tasks = QuestService().get_current_tasks(dev)
            if not tasks:
                return False
            ws = self._device_ws._get_ws(dev)  # noqa: SLF001 - 同层服务内部访问
            if ws is None:
                logger.info("[quest_proactive] 设备离线，跳过 device_id=%s", dev)
                return False

            task = tasks[0]  # get_current_tasks 已按达成率降序
            user_text = _build_user_text(task)
            req_id = uuid.uuid4().hex[:16]
            downlink = WsDownlinkAdapter(
                ws, settings=self._chat.settings, device_id=dev, bus_service=self._bus_service
            )
            events = WsPipelineEventsAdapter(self._bus_service, self._device_ws)
            t0 = time.monotonic()
            turn = await run_chat_turn(
                downlink,
                self._chat,
                user_text,
                request_id=req_id,
                device_id=dev,
                device_ws=self._device_ws,
                t_asr_text=t0,
                force_voice=True,
                bus_service=self._bus_service,
            )
            await publish_chat_turn(
                events,
                dev,
                source="quest_proactive",
                asr_text=user_text,
                t_asr_start=t0,
                t_asr_text=t0,
                turn=turn,
                request_id=req_id,
            )
            voice_ok = _voice_was_played(turn)
            if not voice_ok:
                logger.warning(
                    "[quest_proactive] 主动轮未开口 task_id=%s device_id=%s status=%s error=%r llm_text=%r",
                    task.get("task_id"),
                    dev,
                    turn.status,
                    turn.error,
                    (turn.llm_text or "")[:120],
                )
            logger.info(
                "[quest_proactive] task_id=%s device_id=%s req=%s voice_ok=%s summary=%r",
                task.get("task_id"),
                dev,
                req_id,
                voice_ok,
                (turn.llm_text or turn.error or "")[:120],
            )
            return True
        except Exception:
            logger.exception("[quest_proactive] 主动轮异常 device_id=%s", device_id)
            return True  # 异常视为已尝试，避免调度层按空转立即重试


def _build_user_text(task: dict[str, Any]) -> str:
    """构造剧情推进指令（以系统前缀开头；chat_flow 允许本轮 need_reply=false 静默）。"""
    from deskbot_server.service.quest_service import TYPE_LABELS

    ttype = str(task.get("type") or "once")
    ttype_label = TYPE_LABELS.get(ttype, ttype)
    prompt = str(task.get("prompt") or "").strip()
    parts = [
        f"{_QUEST_PROACTIVE_PREFIX} 主人约 1 分钟没有和本机器人对话，但人就在面前，现在需要主动推进剧情任务："
        f"[{task.get('task_id')}]（{ttype_label}）：{prompt}",
    ]
    if ttype == "once":
        parts.append("  - 一次性任务：本轮引导主人配合达成目标；若依据对话已能确定目标达成，"
                     "直接调 complete_task(task_id, reason, user=当前说话人) 收尾，服务端会自动接续其后继任务。")
    elif ttype == "daily":
        parts.append("  - 日常任务：若该用户今日记录（见 system 下方记录）里还没有此任务的完成行，"
                     "可引导其完成一次并调 complete_task 记账（user 填当前说话人）；已有今日记录就不要重复做。")
    else:  # long_term
        parts.append("  - 长期任务：可自然聊聊相关话题；只在此次对话取得实质新进展时调 "
                     "complete_task(task_id, user=当前说话人, reason=新进展) 追加记录。")
    parts.append(
        "要求：有可开口推进/问候/记录的事项就开口（need_reply=true，tts 写直接说给主人听的引导语，"
        "禁止「已发送/已汇报」式汇报腔）；若该任务今日已对当前用户记录过、或此刻没有值得推进的新事项，"
        "就自然闲聊或保持安静（need_reply=false、tts 留空），不要硬推任务、不要让对话冷场。"
    )
    return "\n".join(parts)
