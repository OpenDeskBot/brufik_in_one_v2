"""Durable reminders delivered by the connected external Agent, never local TTS."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from deskbot_server.dao import scheduled_task_mapper as mapper
from deskbot_server.dao.device_mapper import get_auto_reply
from deskbot_server.db.engine import default_db_path
from deskbot_server.schedule.lease import SchedulerLease
from deskbot_server.service.scheduled_task_service import (
    claim_due_tasks,
    expire_overdue_active_tasks,
    finish_scheduled_task,
    get_scheduled_task,
)

logger = logging.getLogger("deskbot-server")


class ExternalReminderScheduler:
    def __init__(self, hub, *, interval=5, lookback_minutes=5):
        self.hub = hub
        self.interval = max(1, min(float(interval), 60))
        self.lookback_minutes = lookback_minutes
        self._stop = asyncio.Event()
        self._task = None
        self._workers = set()
        self._devices = set()
        self._lease = None

    def start(self):
        if self._task and not self._task.done():
            return
        self._lease = SchedulerLease(default_db_path().with_name(".scheduler.lock"))
        self._lease.acquire()
        mapper.recover_running()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("[external-reminders] poll failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.interval)

    async def tick(self):
        await asyncio.to_thread(expire_overdue_active_tasks, lookback_minutes=self.lookback_minutes)
        tasks = await asyncio.to_thread(claim_due_tasks, limit=10, lookback_minutes=self.lookback_minutes)
        for item in tasks:
            if item["device_id"] in self._devices:
                await asyncio.to_thread(mapper.release_claim, item["id"], "等待前一条提醒播放结束")
                continue
            self._devices.add(item["device_id"])
            task = asyncio.create_task(self._deliver(item))
            self._workers.add(task)
            task.add_done_callback(self._workers.discard)

    async def _deliver(self, item):
        did, task_id = item["device_id"], item["id"]
        try:
            current = await asyncio.to_thread(get_scheduled_task, task_id, device_id=did)
            if current is None or not current.get("enabled") or current.get("status") != "running":
                return
            entry = self.hub._devices.get(did)
            session = entry.conversation_session if entry else None
            announce = getattr(session, "announce", None)
            if not callable(announce) or not await asyncio.to_thread(get_auto_reply, did):
                await asyncio.to_thread(mapper.release_claim, task_id, "设备离线、未启用自动回复或不支持外部提醒")
                return
            result = await announce(current["description"], task_id=task_id, scheduled_at=current["next_run_at"])
            if result.get("ok"):
                await asyncio.to_thread(finish_scheduled_task, task_id, ok=True, summary="设备已确认播完提醒")
            else:
                await asyncio.to_thread(mapper.release_claim, task_id, "提醒延后："+str(result.get("error") or result.get("status") or "未播完")[:300])
        except asyncio.CancelledError:
            await asyncio.to_thread(mapper.release_claim, task_id, "服务停止，等待重新播报")
            raise
        except Exception:
            logger.exception("[external-reminders] delivery failed task_id=%s", task_id)
            await asyncio.to_thread(mapper.release_claim, task_id, "外部会话暂不可用，等待重试")
        finally:
            self._devices.discard(did)

    async def aclose(self):
        self._stop.set()
        if self._task:
            await self._task
            self._task = None
        workers = list(self._workers)
        if workers:
            _, pending = await asyncio.wait(workers, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        if self._lease:
            self._lease.close()
            self._lease = None
