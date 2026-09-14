"""Per-device arbitration for speech-producing interaction turns.

The arbiter deliberately knows nothing about dialogue semantics.  It only applies
deterministic source rules:

* user turns are highest priority and may cancel a system turn;
* scheduled reminders wait in a per-device queue;
* proactive social/quest turns are best-effort and are dropped while busy;
* ordinary turns never run concurrently for the same device.

Playback is still owned by ``DeviceWsService``.  A lease covers the complete
LLM -> TTS -> PB lifecycle so "busy" starts before the LLM call, not after the
audio has already finished.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import count

logger = logging.getLogger("deskbot-server")


class InteractionKind(StrEnum):
    USER = "user"
    SCHEDULED = "scheduled"
    QUEST = "quest_proactive"
    SOCIAL = "social_proactive"


_PRIORITY = {
    InteractionKind.USER: 100,
    InteractionKind.SCHEDULED: 60,
    InteractionKind.QUEST: 20,
    InteractionKind.SOCIAL: 10,
}
_PROACTIVE = frozenset({InteractionKind.QUEST, InteractionKind.SOCIAL})


@dataclass
class _Waiter:
    kind: InteractionKind
    request_id: str
    task: asyncio.Task
    future: asyncio.Future
    order: int


@dataclass
class _DeviceState:
    active: InteractionLease | None = None
    waiters: list[_Waiter] = field(default_factory=list)


class InteractionLease:
    """Exclusive ownership of one device's speech pipeline."""

    def __init__(
        self,
        arbiter: DeviceInteractionArbiter,
        state_key: tuple[asyncio.AbstractEventLoop, str],
        *,
        kind: InteractionKind,
        request_id: str,
        task: asyncio.Task,
    ) -> None:
        self._arbiter = arbiter
        self._state_key = state_key
        self.kind = kind
        self.request_id = request_id
        self.task = task
        self.phase = "generating"
        self._released = False

    @property
    def device_id(self) -> str:
        return self._state_key[1]

    def mark_speaking(self) -> None:
        """Mark the point after which ordinary system events must not cut audio."""
        self.phase = "speaking"

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._arbiter._release(self)  # noqa: SLF001 - lease/arbiter are one abstraction

    async def __aenter__(self) -> InteractionLease:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class DeviceInteractionArbiter:
    """Small in-process priority queue, isolated per event loop and device."""

    def __init__(self) -> None:
        self._states: dict[tuple[asyncio.AbstractEventLoop, str], _DeviceState] = {}
        self._order = count()

    async def acquire(
        self,
        device_id: str,
        *,
        kind: InteractionKind,
        request_id: str | None = None,
    ) -> InteractionLease | None:
        dev = str(device_id or "").strip()
        if not dev:
            return None
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always provides one here
            raise RuntimeError("interaction arbitration requires an asyncio task")
        key = (loop, dev)
        state = self._states.setdefault(key, _DeviceState())
        rid = str(request_id or "").strip()

        # Proactive turns have no queueing value: if anything owns or is waiting
        # for the device, the natural moment has already passed.
        if kind in _PROACTIVE and (state.active is not None or state.waiters):
            logger.info(
                "[interaction] 主动事件因设备忙跳过 device_id=%s req=%s kind=%s active=%s",
                dev,
                rid,
                kind.value,
                state.active.kind.value if state.active is not None else "queued",
            )
            return None

        future: asyncio.Future[InteractionLease] = loop.create_future()
        waiter = _Waiter(kind=kind, request_id=rid, task=task, future=future, order=next(self._order))
        state.waiters.append(waiter)

        active = state.active
        should_preempt = False
        if active is not None and not active.task.done():
            if kind == InteractionKind.USER and active.kind != InteractionKind.USER:
                should_preempt = True
            elif kind == InteractionKind.SCHEDULED and active.kind in _PROACTIVE and active.phase == "generating":
                # A reminder may prevent a not-yet-spoken proactive turn, but it
                # must never cut a proactive sentence already being played.
                should_preempt = True
        if should_preempt:
            logger.info(
                "[interaction] 高优先级事件取消未完成事件 device_id=%s new_req=%s new_kind=%s "
                "old_req=%s old_kind=%s phase=%s",
                dev,
                rid,
                kind.value,
                active.request_id,
                active.kind.value,
                active.phase,
            )
            active.task.cancel()

        self._grant_next(key, state)
        try:
            return await future
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            if waiter in state.waiters:
                state.waiters.remove(waiter)
            self._grant_next(key, state)
            self._cleanup_state(key, state)
            raise

    def mark_current_speaking(self, device_id: str, *, request_id: str | None = None) -> None:
        """Advance the owning lease to its non-preemptible speech phase.

        Interim TTS runs in a child task, so ownership may be identified by the
        parent request id as well as by the current asyncio task.
        """
        dev = str(device_id or "").strip()
        if not dev:
            return
        loop = asyncio.get_running_loop()
        state = self._states.get((loop, dev))
        task = asyncio.current_task()
        active = state.active if state is not None else None
        rid = str(request_id or "").strip()
        if active is not None and (active.task is task or (rid and active.request_id == rid)):
            active.mark_speaking()

    def is_busy(self, device_id: str) -> bool:
        dev = str(device_id or "").strip()
        if not dev:
            return False
        state = self._states.get((asyncio.get_running_loop(), dev))
        return bool(state and (state.active is not None or state.waiters))

    def _grant_next(self, key: tuple[asyncio.AbstractEventLoop, str], state: _DeviceState) -> None:
        if state.active is not None:
            return
        state.waiters = [waiter for waiter in state.waiters if not waiter.future.cancelled()]
        if not state.waiters:
            return
        waiter = max(state.waiters, key=lambda item: (_PRIORITY[item.kind], -item.order))
        state.waiters.remove(waiter)
        lease = InteractionLease(
            self,
            key,
            kind=waiter.kind,
            request_id=waiter.request_id,
            task=waiter.task,
        )
        state.active = lease
        if not waiter.future.done():
            waiter.future.set_result(lease)
        logger.info(
            "[interaction] 获得设备发言权 device_id=%s req=%s kind=%s queued=%d",
            key[1],
            waiter.request_id,
            waiter.kind.value,
            len(state.waiters),
        )

    def _release(self, lease: InteractionLease) -> None:
        state = self._states.get(lease._state_key)  # noqa: SLF001
        if state is None or state.active is not lease:
            return
        state.active = None
        logger.info(
            "[interaction] 释放设备发言权 device_id=%s req=%s kind=%s",
            lease.device_id,
            lease.request_id,
            lease.kind.value,
        )
        self._grant_next(lease._state_key, state)  # noqa: SLF001
        self._cleanup_state(lease._state_key, state)  # noqa: SLF001

    def _cleanup_state(
        self, key: tuple[asyncio.AbstractEventLoop, str], state: _DeviceState
    ) -> None:
        if state.active is None and not state.waiters:
            self._states.pop(key, None)


interaction_arbiter = DeviceInteractionArbiter()
