"""实时对话「按需采集」门控：仅当后台页面订阅者在场才采集/推送，无人查看不落数据。

- ``bus.pub`` 仅在目标设备存在 WS 订阅者时写入滚动窗口（seq 照常分配供上行 ack）；
- 最后一位订阅者离开时清空该设备的滚动窗口与音频/图像媒体留存；
- ``has_subscribers_sync`` 供媒体留存侧同步判活（device_ws / chat_flow）。
"""

from __future__ import annotations

import asyncio

import pytest

from deskbot_server.service.application.convo_audio_store import ConvoAudioStore
from deskbot_server.service.bus_service import BusService

SILENCE_16K = b"\x00\x00" * 1600  # 0.1s @16k mono


class _FakeWs:
    pass


class _NoopFanout:
    """替换 _PerWsFireAndForget：单测不跑真实 ws 发送。"""

    def __init__(self) -> None:
        self.sent: list = []

    def submit(self, ws, msg) -> None:
        self.sent.append((ws, msg))

    def discard(self, ws) -> None:
        pass


@pytest.fixture(autouse=True)
def _fresh_singletons():
    BusService.reset_instance()
    ConvoAudioStore.reset_instance()
    yield
    BusService.reset_instance()
    ConvoAudioStore.reset_instance()


def _bus() -> BusService:
    bus = BusService()
    bus._fanout = _NoopFanout()  # noqa: SLF001 单测直改内部
    return bus


def _stage_event(request_id: str, **extra) -> dict:
    return {"request_id": request_id, "stage": "asr_done", "asr_text": "你好", **extra}


class TestPubCollectionGate:
    def test_no_viewer_pub_skips_window_but_keeps_seq(self):
        bus = _bus()

        async def run():
            evt = await bus.pub("dev_a", _stage_event("r1"))
            assert evt["seq"] >= 1  # 上行生产者仍拿到 ack
            assert evt["device_id"] == "dev_a"
            assert bus.snapshot("dev_a") == []
            assert bus.snapshot() == []

        asyncio.run(run())

    def test_pub_collected_only_for_watched_device(self):
        bus = _bus()
        ws = _FakeWs()

        async def run():
            await bus.subscribe_ws(ws, "dev_a")
            await bus.pub("dev_a", _stage_event("r1"))
            await bus.pub("dev_b", _stage_event("r2"))
            assert len(bus.snapshot("dev_a")) == 1
            assert bus.snapshot("dev_a")[0]["request_id"] == "r1"
            assert bus.snapshot("dev_b") == []
            await bus.unsubscribe_ws(ws)
            assert bus.snapshot("dev_a") == []  # 最后一位离开即清空

        asyncio.run(run())

    def test_watch_all_subscriber_covers_every_device(self):
        bus = _bus()
        ws = _FakeWs()

        async def run():
            await bus.subscribe_ws(ws, None)  # 不设过滤：全部设备
            await bus.pub("dev_c", _stage_event("r3"))
            assert len(bus.snapshot("dev_c")) == 1
            assert await bus.has_subscribers("dev_c") is True

        asyncio.run(run())

    def test_second_viewer_keeps_data_until_last_leaves(self):
        bus = _bus()
        ws1 = _FakeWs()
        ws2 = _FakeWs()

        async def run():
            await bus.subscribe_ws(ws1, "dev_a")
            await bus.subscribe_ws(ws2, "dev_a")
            await bus.pub("dev_a", _stage_event("r1"))
            await bus.unsubscribe_ws(ws1)  # 仍有 ws2 在看
            assert len(bus.snapshot("dev_a")) == 1
            await bus.unsubscribe_ws(ws2)
            assert bus.snapshot("dev_a") == []

        asyncio.run(run())

    def test_last_viewer_leave_purges_media_too(self):
        bus = _bus()
        store = ConvoAudioStore()
        ws = _FakeWs()

        async def run():
            # 先有订阅者：媒体与事件正常留存
            await bus.subscribe_ws(ws, "dev_a")
            store.put("dev_a", "r1", "asr", SILENCE_16K, 16000)
            store.put("dev_b", "r1", "asr", SILENCE_16K, 16000)
            await bus.pub("dev_a", _stage_event("r1"))
            assert store.has("dev_a", "r1", "asr") is True
            assert len(bus.snapshot("dev_a")) == 1
            # 最后一位离开：该设备事件与媒体一并清空，其它设备不受影响
            await bus.unsubscribe_ws(ws)
            assert bus.snapshot("dev_a") == []
            assert store.has("dev_a", "r1", "asr") is False
            assert store.has("dev_b", "r1", "asr") is True

        asyncio.run(run())


class TestSyncViewerCheck:
    def test_sync_check_matches_async(self):
        bus = _bus()
        ws = _FakeWs()

        async def run():
            assert bus.has_subscribers_sync("dev_a") is False
            assert await bus.has_subscribers("dev_a") is False
            await bus.subscribe_ws(ws, "dev_a")
            assert bus.has_subscribers_sync("dev_a") is True
            assert bus.has_subscribers_sync("dev_b") is False
            assert await bus.has_subscribers("dev_a") is True
            assert await bus.has_subscribers("dev_b") is False
            await bus.unsubscribe_ws(ws)
            assert bus.has_subscribers_sync("dev_a") is False

        asyncio.run(run())

    def test_watch_all_sync_sees_all_devices(self):
        bus = _bus()
        ws = _FakeWs()

        async def run():
            await bus.subscribe_ws(ws, None)
            assert bus.has_subscribers_sync("dev_x") is True
            assert bus.has_subscribers_sync() is True

        asyncio.run(run())
