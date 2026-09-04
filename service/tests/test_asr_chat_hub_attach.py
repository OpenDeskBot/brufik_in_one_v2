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


# ---------------------------------------------------------------------------
# 接管防卡死回归：2026-09-04 同设备 3ms 双连触发接管竞态 → register() 永久卡死，
# 此后每条新连接都服务不了、设备 ~14.6s 重连风暴且不自愈。
# 修复原则：新连接服务绝不依赖旧连接清理完成——旧 ws.close 与旧 worker 退役全部有界。
# ---------------------------------------------------------------------------


def test_register_proceeds_when_old_ws_close_hangs():
    """旧连接 close 握手永不返回（僵尸 peer）时，新连接必须快速接管，不能陪葬。"""
    import deskbot_server.service.device_ws_service as dws

    async def _run() -> None:
        DeviceWsService.reset_instance()
        svc = DeviceWsService()

        async def _hang_close(*_a, **_k):
            # 模拟僵尸 peer：close 握手永不返回（websockets close_timeout 默认 None）
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

        old_ws = MagicMock()
        old_ws.close = AsyncMock(side_effect=_hang_close)
        new_ws = MagicMock()
        new_ws.close = AsyncMock()

        saved = dws._SUPERSEDE_CLOSE_TIMEOUT
        dws._SUPERSEDE_CLOSE_TIMEOUT = 0.05
        try:
            await svc.register("dev1", old_ws)
            await asyncio.wait_for(svc.register("dev1", new_ws), timeout=1.0)
        finally:
            dws._SUPERSEDE_CLOSE_TIMEOUT = saved

        entry = svc._devices["dev1"]
        assert entry.ws is new_ws
        assert old_ws.close.await_count == 1

    asyncio.run(_run())


def test_register_survives_stubborn_old_seq_task():
    """旧代 seq worker 无视取消、永久滞留（原卡死最坏形态）时，register 换代照常完成。"""
    import deskbot_server.service.device_ws_service as dws

    async def _run() -> None:
        DeviceWsService.reset_instance()
        svc = DeviceWsService()
        release = asyncio.Event()

        async def _stubborn():
            # 模拟旧 worker 最坏情形：吞掉第一次取消继续滞留，直到 release
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            await asyncio.sleep(0)

        old_ws = MagicMock()
        old_ws.close = AsyncMock()
        await svc.register("dev1", old_ws)  # 先正常注册出一个 worker
        entry = svc._devices["dev1"]
        old_task = entry.seq_task
        assert old_task is not None and not old_task.done()

        # 把当前 worker 换成拒不退出的顽固任务，模拟被顶替连接遗留的僵尸 worker
        entry.seq_task = asyncio.create_task(_stubborn())
        old_task.cancel()  # 清掉真实 worker，避免干扰
        try:
            # 新任务可能尚未启动就被取消：CancelledError 直接抛给 await 方，需捕获
            try:
                await old_task
            except asyncio.CancelledError:
                pass

            new_ws = MagicMock()
            new_ws.close = AsyncMock()
            # 旧实现会卡在 _stop_device 的 `await seq_task`（顽固任务不退）→ 测试挂死；
            # 新实现摘除即走、有界等待，register 必须在限时内返回
            await asyncio.wait_for(svc.register("dev1", new_ws), timeout=1.0)

            assert entry.ws is new_ws
            assert entry.generation >= 1
            new_task = entry.seq_task
            assert new_task is not None and not new_task.done()
            assert new_task is not old_task
        finally:
            release.set()  # 放行顽固任务自然退出
            if entry.seq_task is not None:
                entry.seq_task.cancel()

    asyncio.run(_run())


def test_superseded_worker_generation_gate_blocks_stale_send():
    """换代后，旧代 _do_send_to_device 必须拒绝发送，防止向新连接发旧队列内容。"""
    async def _run() -> None:
        DeviceWsService.reset_instance()
        svc = DeviceWsService()
        ws_a = MagicMock()
        ws_a.close = AsyncMock()
        ws_b = MagicMock()
        ws_b.close = AsyncMock()

        await svc.register("dev1", ws_a)
        gen_a = svc._devices["dev1"].generation
        await svc.register("dev1", ws_b)
        entry = svc._devices["dev1"]
        assert entry.generation == gen_a + 1

        # 旧代发送直接拒绝（False）
        from deskbot_server.model.pb_seq import PbBlock, PbType

        block = PbBlock(type=PbType.SINGLE, req="stale_req", idx=0)
        ok = await svc._do_send_to_device("dev1", block, generation=gen_a)
        assert ok is False

    asyncio.run(_run())
