"""pb 窗口 ACK 流控：末窗口必须等到 pb_end 才结束 seq / 下发下一 seq。

修复对象：device_ws_service._device_loop / _wait_ack。
设备 ack 的 ``type`` 恒为 "pb_ack"，真正的子类型在 ``ack_type``
（pb_chunk=每分发 10 帧，pb_end=执行器全部执行完）。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from deskbot_server.model.pb_seq import PbAction, PbBlock, PbSeq, PbType
from deskbot_server.service.device_ws_service import DeviceWsService, _DeviceEntry


def _ack(req: str, ack_type: str, idx: int = 0) -> dict:
    """模拟 _normalize_incoming_pb_ack 归一化后的设备 ack。"""
    return {"type": "pb_ack", "ack_type": ack_type, "req": req, "idx": idx, "space": 40}


def _blocks(req: str, n: int) -> tuple[PbBlock, ...]:
    """n 片链：pb_start(idx=0) → pb_chunk(1..n-2) → pb_end(idx=n-1)。"""
    return tuple(
        PbBlock(
            type=PbType.START if i == 0 else (PbType.END if i == n - 1 else PbType.CHUNK),
            req=req,
            idx=i,
            chunk_ms=100,
        )
        for i in range(n)
    )


def _make_svc(monkeypatch):
    DeviceWsService.reset_instance()
    svc = DeviceWsService()
    entry = _DeviceEntry(device_id="d1")
    svc._devices["d1"] = entry
    sent: list[tuple[str, PbType, str, int]] = []

    async def fake_send(device_id: str, block: PbBlock, **_kw) -> bool:
        sent.append((device_id, block.type, block.req, block.idx))
        return True

    monkeypatch.setattr(svc, "_do_send_to_device", fake_send)
    return svc, entry, sent


async def _wait_until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() >= deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.01)


async def _run_device_loop(svc):
    """启动 _device_loop 任务，测试结束统一取消。"""
    task = asyncio.create_task(svc._device_loop("d1"))
    try:
        yield task
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_final_window_waits_for_pb_end(monkeypatch):
    """单窗口链（n=3）：pb_chunk 不得结束末窗口，pb_end 才结束。"""

    async def _run() -> None:
        svc, entry, sent = _make_svc(monkeypatch)
        seq = PbSeq(req="r1", entries=_blocks("r1", 3), level=1, action=PbAction.REPLACE)
        assert svc._enqueue(entry, seq) == 1

        async for _task in _run_device_loop(svc):
            await _wait_until(lambda: len(sent) == 3)
            assert [b[1] for b in sent] == [PbType.START, PbType.CHUNK, PbType.END]

            # pb_chunk：只能说明分发过 10 帧/空间够，不能代表播放完成
            entry.ack_queue.put_nowait(_ack("r1", "pb_chunk", idx=2))
            await asyncio.sleep(0.1)
            assert not seq._done.is_set()
            assert not any(t == PbType.CANCEL for _d, t, _r, _i in sent)

            entry.ack_queue.put_nowait(_ack("r1", "pb_end", idx=2))
            await _wait_until(seq._done.is_set)
            assert entry.sending_seq is None

    asyncio.run(_run())


def test_multi_window_chunk_advances_but_final_waits_end(monkeypatch):
    """多窗口链（n=15）：pb_chunk 推进窗口，但末窗口必须等 pb_end。"""

    async def _run() -> None:
        svc, entry, sent = _make_svc(monkeypatch)
        seq = PbSeq(req="r2", entries=_blocks("r2", 15), level=1, action=PbAction.REPLACE)
        assert svc._enqueue(entry, seq) == 1

        async for _task in _run_device_loop(svc):
            await _wait_until(lambda: len(sent) == 10)
            entry.ack_queue.put_nowait(_ack("r2", "pb_chunk", idx=9))
            await _wait_until(lambda: len(sent) == 15)

            # 末窗口：chunk 不得放行
            entry.ack_queue.put_nowait(_ack("r2", "pb_chunk", idx=14))
            await asyncio.sleep(0.1)
            assert not seq._done.is_set()

            entry.ack_queue.put_nowait(_ack("r2", "pb_end", idx=14))
            await _wait_until(seq._done.is_set)

            assert len(sent) == 15
            assert [b[1] for b in sent] == (
                [PbType.START] + [PbType.CHUNK] * 13 + [PbType.END]
            )

    asyncio.run(_run())


def test_preempt_during_end_wait_sends_cancel_and_plays_next(monkeypatch):
    """end-wait 期间高优先级 seq 入队：立即 pb_cancel 抢占，然后播下一 seq。"""

    async def _run() -> None:
        svc, entry, sent = _make_svc(monkeypatch)
        seq1 = PbSeq(req="r3", entries=_blocks("r3", 3), level=1, action=PbAction.REPLACE)
        assert svc._enqueue(entry, seq1) == 1

        async for _task in _run_device_loop(svc):
            await _wait_until(lambda: len(sent) == 3)
            entry.ack_queue.put_nowait(_ack("r3", "pb_chunk", idx=2))
            await asyncio.sleep(0.1)
            assert not seq1._done.is_set()  # 正在 end-wait

            seq2 = PbSeq(req="r4", entries=_blocks("r4", 2), level=2, action=PbAction.REPLACE)
            assert svc._enqueue(entry, seq2) == 1  # level 2 抢占

            await _wait_until(seq1._done.is_set)
            assert sent[3] == ("d1", PbType.CANCEL, "r3", 0)  # cancel 带旧 req（第 4 个发送）

            await _wait_until(lambda: len(sent) == 3 + 1 + 2)
            assert [b[2] for b in sent[4:]] == ["r4", "r4"]

            entry.ack_queue.put_nowait(_ack("r4", "pb_end", idx=1))
            await _wait_until(seq2._done.is_set)

    asyncio.run(_run())


def test_wrong_req_acks_are_skipped(monkeypatch):
    """异 req / 空 ack_type 的 ack 被跳过或静默消费，仍等待 pb_end。"""

    async def _run() -> None:
        svc, entry, sent = _make_svc(monkeypatch)
        seq = PbSeq(req="r5", entries=_blocks("r5", 3), level=1, action=PbAction.REPLACE)
        assert svc._enqueue(entry, seq) == 1

        async for _task in _run_device_loop(svc):
            await _wait_until(lambda: len(sent) == 3)
            entry.ack_queue.put_nowait(_ack("r-other", "pb_end", idx=2))  # 异 req：跳过
            entry.ack_queue.put_nowait(  # 同 req 但 ack_type 为空：末窗口静默消费
                {"type": "pb_ack", "ack_type": "", "req": "r5", "idx": 2, "space": 40}
            )
            await asyncio.sleep(0.1)
            assert not seq._done.is_set()

            entry.ack_queue.put_nowait(_ack("r5", "pb_end", idx=2))
            await _wait_until(seq._done.is_set)

    asyncio.run(_run())


def test_next_seq_starts_only_after_pb_end(monkeypatch):
    """队列中的下一 seq（APPEND 并存）必须等当前 seq 的 pb_end 后才开始。"""

    async def _run() -> None:
        svc, entry, sent = _make_svc(monkeypatch)
        seq1 = PbSeq(req="r6", entries=_blocks("r6", 3), level=1, action=PbAction.REPLACE)
        assert svc._enqueue(entry, seq1) == 1

        async for _task in _run_device_loop(svc):
            await _wait_until(lambda: len(sent) == 3)

            # 同级 APPEND：并存排队（REPLACE 会触发抢占，语义不同）
            seq2 = PbSeq(req="r7", entries=_blocks("r7", 3), level=1, action=PbAction.APPEND)
            assert svc._enqueue(entry, seq2) == 1

            await asyncio.sleep(0.1)
            assert len(sent) == 3  # seq1 未收到 pb_end，seq2 不得开始
            assert not seq2._done.is_set()

            entry.ack_queue.put_nowait(_ack("r6", "pb_end", idx=2))
            await _wait_until(lambda: len(sent) == 6)
            assert [b[2] for b in sent[3:]] == ["r7", "r7", "r7"]

            entry.ack_queue.put_nowait(_ack("r7", "pb_end", idx=2))
            await _wait_until(seq2._done.is_set)

    asyncio.run(_run())


def test_empty_seq_done_without_ack(monkeypatch):
    """空链（n=0）：不等待任何 ack，直接 done。"""

    async def _run() -> None:
        svc, entry, sent = _make_svc(monkeypatch)
        seq = PbSeq(req="r0", entries=(), level=1, action=PbAction.REPLACE)
        assert svc._enqueue(entry, seq) == 1

        async for _task in _run_device_loop(svc):
            await _wait_until(seq._done.is_set)
            assert sent == []

    asyncio.run(_run())
