"""剧本主动推进：设备冷场 ≥1 分钟且用户在面前时，发起一轮主动对话推进剧情。

由 LiveService（on_face_tick 冷场判定）调度调用。Quest 当前 running 任务与
工具契约按 device_id 自动注入每轮对话的 system prompt（infrastructure/llm/utils.py
的 llm_quest_tasks / llm_quest_tools 附录），因此这里只需像定时任务
（ScheduledTaskScheduler）一样发起一轮 run_chat_turn —— LLM 会看到任务定义，
自主选择开口引导主人配合，或在成功/失败条件已满足时直接调 update_task_result
判定终态并沿连线传播分数。

user_text 以 ``_QUEST_PROACTIVE_PREFIX`` 开头：chat_flow 据此把它当作系统发起轮，
强制 need_reply 并把「已发送/已汇报」类 meta 文案兜底成面向主人的口播语。
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
                registry=self._device_ws,
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
    """构造剧情推进指令（以系统前缀开头，chat_flow 强制本轮开口）。"""
    parts = [
        f"{_QUEST_PROACTIVE_PREFIX} 主人约 1 分钟没有和本机器人对话，但人就在面前，现在需要主动推进剧情任务："
        f"[{task.get('task_id')}] {task.get('title') or 'notitle'}",
    ]
    for key in ("goal", "strategy", "success_condition", "failure_condition"):
        val = str(task.get(key) or "").strip()
        if val:
            parts.append(f"  {key}：{val}")
    parts.append(
        "要求：need_reply 必须为 true，tts 写直接说给主人听的引导语（提问 / 请求配合 / 简述进展），"
        "禁止写「已发送」「已汇报」等汇报语；若依据已掌握的信息能明确判断成功条件或失败条件满足，"
        "直接调用 update_task_result 判定终态并简短口播结论；若任务暂无法推进，"
        "就自然地把话题引向下一个进行中任务或闲聊，不要让对话冷场。"
    )
    return "\n".join(parts)
