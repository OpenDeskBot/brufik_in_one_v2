"""任务声明、显式注册、重入跳过和执行记录。"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

logger = logging.getLogger("deskbot-server")


def scheduled(job_id: str, interval_key: str):
    def declare(fn):
        fn.__schedule__ = {"id": job_id, "interval_key": interval_key}
        return fn

    return declare


def register(entries, config):
    identities, functions, output = set(), set(), []
    for entry in entries:
        spec = getattr(entry, "__schedule__", None)
        if spec is None or not spec.get("id") or not spec.get("interval_key"):
            raise ValueError(f"missing schedule declaration: {entry}")
        if entry in functions or spec["id"] in identities:
            raise ValueError(f"duplicate schedule: {spec['id']}")
        value = config.get(spec["interval_key"])
        if isinstance(value, bool) or not isinstance(value, (float, int)) or value <= 0:
            raise ValueError(f"invalid schedule interval: {spec['id']}")
        identities.add(spec["id"])
        functions.add(entry)
        output.append((entry, float(value)))
    return output


class ScheduleExecutor:
    def __init__(self):
        self._running = set()

    async def run(self, job_id, callback, *, planned_at=None):
        trigger_id = uuid.uuid4().hex
        planned = planned_at or datetime.now(UTC).isoformat()
        started = datetime.now(UTC).isoformat()
        if job_id in self._running:
            logger.info(
                "schedule_run job_id=%s trigger_id=%s planned_at=%s started_at=null status=skipped duration_ms=null reason=overlap",
                job_id,
                trigger_id,
                planned,
            )
            return None
        self._running.add(job_id)
        begin, status, reason = time.perf_counter(), "success", ""
        try:
            result = await callback()
            if result is False:
                status, reason = "failure", "business_failure"
            return result
        except BaseException as exc:
            status, reason = "failure", type(exc).__name__
            raise
        finally:
            self._running.discard(job_id)
            logger.info(
                "schedule_run job_id=%s trigger_id=%s planned_at=%s started_at=%s status=%s duration_ms=%.3f reason=%s",
                job_id,
                trigger_id,
                planned,
                started,
                status,
                (time.perf_counter() - begin) * 1000,
                reason,
            )
