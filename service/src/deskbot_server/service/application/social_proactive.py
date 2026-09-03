"""主动社交问候推进：冷场且识别到认识的人在面前时，发起一轮「主动问候」轮。

由 LiveService（on_face_tick 冷场判定，与 quest 共用节流通道）调度调用。识别到
已知用户时 system prompt 会自动注入按人档案/今日记录与「你的当前任务」规则
（infrastructure/llm/utils.py 的 llm_user_social_context / llm_social_active_tasks
附录），因此这里只需像 QuestProactiveRunner 一样发起一轮 run_chat_turn：LLM
依据规则自行判断此刻是否该主动问候/关心/表达思念——该开口就先调 update_daily_task
记账再口播；无话可说时输出 need_reply=false 静默退出。

user_text 以 ``_SOCIAL_PROACTIVE_PREFIX``（[系统主动问候]）开头：chat_flow 对社交
前缀轮**不**强制开口（区别于定时/剧情轮），并把 meta 汇报类文案兜底为静默，
避免「已问候」式文案被照字朗读。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter, WsPipelineEventsAdapter
from deskbot_server.service.application.chat_flow import (
    _SOCIAL_PROACTIVE_PREFIX,
    _voice_was_played,
    publish_chat_turn,
    run_chat_turn,
)

if TYPE_CHECKING:
    from deskbot_server.service.application.chat_service import ChatService
    from deskbot_server.service.bus_service import BusService
    from deskbot_server.service.device_ws_service import DeviceWsService

logger = logging.getLogger("deskbot-server")


class SocialProactiveRunner:
    """社交主动推进器：对设备发起一轮「主动问候」对话。

    ``attempt(device_id) -> bool``：False = 设备离线 / 面前无已识别已知用户（由
    调用方做空转冷却）；True = 已发起一轮（本轮开口或静默退出由 LLM 依据规则决定）。
    内部异常一律兜底并记日志，不向调度层上抛。
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
            ws = self._device_ws._get_ws(dev)  # noqa: SLF001 - 同层服务内部访问
            if ws is None:
                logger.info("[social_proactive] 设备离线，跳过 device_id=%s", dev)
                return False
            names = self._known_face_names(dev)
            if not names:
                logger.debug("[social_proactive] 面前无已识别已知用户，跳过 device_id=%s", dev)
                return False

            user_text = _build_user_text(names)
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
                source="social_proactive",
                asr_text=user_text,
                t_asr_start=t0,
                t_asr_text=t0,
                turn=turn,
                request_id=req_id,
            )
            voice_ok = _voice_was_played(turn)
            logger.info(
                "[social_proactive] device_id=%s req=%s voice_ok=%s users=%s summary=%r",
                dev,
                req_id,
                voice_ok,
                names,
                (turn.llm_text or turn.error or "(静默退出)")[:120],
            )
            return True
        except Exception:
            logger.exception("[social_proactive] 主动轮异常 device_id=%s", device_id)
            return True  # 异常视为已尝试，避免调度层按空转立即重试

    @staticmethod
    def _known_face_names(device_id: str) -> list[str]:
        """当前人脸快照中已匹配到档案的名字（升序去重，最多 4 个）。"""
        try:
            from deskbot_server.service.application.face_snapshot_cache import list_recognized_faces

            rows = list_recognized_faces(device_id, limit=4) or []
        except Exception:
            return []
        names: list[str] = []
        for row in rows:
            name = str(row.get("person_name") or "").strip()
            if name and name not in names:
                names.append(name)
        return names


def _build_user_text(names: list[str]) -> str:
    """构造社交问候指令（以系统前缀开头；chat_flow 允许静默退出，不强制开口）。"""
    name_text = "、".join(names)
    return (
        f"{_SOCIAL_PROACTIVE_PREFIX} 检测到认识的人（{name_text}）在面前，且有一段时间"
        "没有对话，现在轮到你主动开口。请依据 system 提示中的「你的当前任务」规则判断：\n"
        "- 属于该主动表达的情形（该时段首次见面问候 / 饭点关心吃饭 / 距上次对话较久表达思念）："
        "先调用 update_daily_task 记账，再正常开口（need_reply=true，tts 写直接说给"
        "对方听的口语，称呼其名），不要复述规则本身。\n"
        "- 不属于任何需要主动表达的情形（如今天该时段已经问候过、刚聊过不久且不在饭点）："
        "本轮不开口 —— need_reply=false、tts 留空，只输出 JSON。\n"
        "禁止输出「已问候/已汇报」类的汇报语。"
    )
