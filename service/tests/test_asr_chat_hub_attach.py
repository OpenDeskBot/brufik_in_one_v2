"""DeviceWsService.register：同 device 仅保留最新连接。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from deskbot_server.service.device_ws_service import DeviceWsService


def test_register_closes_previous_connection_for_same_device():
    async def _run() -> None:
        DeviceWsService.reset_instance()
        svc = DeviceWsService()
        old_ws = MagicMock()
        old_ws.close = AsyncMock()
        new_ws = MagicMock()
        new_ws.close = AsyncMock()

        await svc.register("dev1", old_ws)
        await svc.register("dev1", new_ws)

        entry = svc._devices["dev1"]
        assert entry.ws is new_ws
        old_ws.close.assert_awaited_once()
        assert old_ws.close.await_args.kwargs.get("code") == 1000

    asyncio.run(_run())


def test_register_claim_slot_false_does_not_evict_asr_chat():
    """device_pipeline 生产者（claim_slot=False）不得抢占 asr_chat 的下行槽位，
    否则两连接互相踢形成反复重连。"""
    async def _run() -> None:
        DeviceWsService.reset_instance()
        svc = DeviceWsService()
        asr_ws = MagicMock()
        asr_ws.close = AsyncMock()
        producer_ws = MagicMock()
        producer_ws.close = AsyncMock()

        await svc.register("dev1", asr_ws)
        # 生产者接入：不覆盖 entry.ws，也不关闭 asr_chat 连接
        await svc.register("dev1", producer_ws, claim_slot=False)

        entry = svc._devices["dev1"]
        assert entry.ws is asr_ws
        asr_ws.close.assert_not_awaited()
        # 反向：asr_chat 重连仍可正常抢占
        asr_ws2 = MagicMock()
        asr_ws2.close = AsyncMock()
        await svc.register("dev1", asr_ws2)
        assert entry.ws is asr_ws2

    asyncio.run(_run())


def test_register_keeps_only_one_ws():
    async def _run() -> None:
        DeviceWsService.reset_instance()
        svc = DeviceWsService()
        ws_a = MagicMock()
        ws_a.close = AsyncMock()
        ws_b = MagicMock()
        ws_b.close = AsyncMock()
        ws_c = MagicMock()
        ws_c.close = AsyncMock()

        await svc.register("dev1", ws_a)
        await svc.register("dev1", ws_b)
        await svc.register("dev1", ws_c)

        entry = svc._devices["dev1"]
        assert entry.ws is ws_c
        assert ws_a.close.await_count == 1
        assert ws_b.close.await_count == 1
        ws_c.close.assert_not_awaited()

    asyncio.run(_run())
