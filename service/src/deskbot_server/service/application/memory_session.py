"""Persistent evidence consumer, independent of the lossy admin-view queue."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import OrderedDict

from deskbot_server.model.memory import MemoryEpochChanged, now_iso
from deskbot_server.service import memory_service as memory
from deskbot_server.service.memory_consolidation import consolidate_event, resume_consolidation

logger = logging.getLogger("deskbot-server")


class MemorySession:
    def __init__(self, device_id):
        self.device_id = device_id
        self.session_id = uuid.uuid4().hex
        self.epoch = None
        self.queue = asyncio.Queue(maxsize=512)
        self.observations = OrderedDict()
        self.input_events = OrderedDict()
        self.used_sources = set()
        self.pending_inputs = {}
        self.changed = asyncio.Event()
        self.worker = None
        self.closing = False
        self.invalidated = False

    async def start(self):
        self.epoch = await asyncio.to_thread(memory.memory_epoch, self.device_id)
        self.worker = asyncio.create_task(self._run())

    def offer(self, action, data):
        if self.invalidated:
            return
        if action not in {"input.transcript.done", "output.transcript.done", "memory.action", "memory.playback"}:
            return
        value = dict(data)
        value.setdefault("occurred_at", now_iso())
        value.setdefault("external_id", self.session_id + ":" + str(value.get("item_id") or value.get("turn_id") or uuid.uuid4().hex) + ":" + action)
        # Never evict evidence. Explicit failure closes the upstream rather than
        # silently dropping a conversation when disk throughput is insufficient.
        self.queue.put_nowait((action, value))

    async def prepare(self, turn_id, observation, *, budget=4000):
        self.observations[turn_id] = observation
        while len(self.observations) > 128:
            self.observations.popitem(last=False)
        self.changed.set()
        # ASR may arrive after speech_stopped. Wait briefly for its durable
        # source ID; a timeout leaves the conversation usable, with no invented source.
        deadline = asyncio.get_running_loop().time() + .8
        while turn_id not in self.input_events and asyncio.get_running_loop().time() < deadline:
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), max(.001, deadline - asyncio.get_running_loop().time()))
            except TimeoutError:
                break
        return await self.context(turn_id, observation, budget=budget)

    async def context(self, turn_id, observation, *, budget=4000):
        if budget < 200 or self.invalidated:
            return ""
        event_id = self.input_events.get(turn_id)
        if event_id and observation.get("speaker", {}).get("state") != "found":
            source = await asyncio.to_thread(memory.read_chat, self.device_id, event_id, context=0)
            if source["event"].get("person_id") and source["event"]["identity_basis"] == "self_declared":
                observation = {**observation, "speaker": {"state": "found", "name": source["event"]["actor"]}}
        context = await asyncio.to_thread(memory.person_context, self.device_id, observation, budget=max(0, budget-180), expected_epoch=self.epoch)
        profiles = json.loads(context.split("\n", 1)[1])
        await self.link_sources(turn_id, [sid for profile in profiles for entry in profile["entries"] for sid in entry["source_ids"]])
        if event_id:
            context += "\n本轮用户发言来源事件编号：" + event_id
        return context

    async def link_sources(self, turn_id, source_ids):
        # Earlier tool results remain in this upstream conversation even when a
        # later turn has no recognized speaker. Track that exposure as well.
        self.used_sources.update(source_ids)
        await asyncio.to_thread(memory.link_turn_sources, self.device_id, turn_id, list(self.used_sources), expected_epoch=self.epoch)

    async def update_sources(self, person_id, source_ids):
        """Include unfinished ASR fragments from this same person's exchange."""
        if not all(isinstance(sid, str) for sid in source_ids):
            return []
        pending = list(self.pending_inputs.values())
        if not set(source_ids).intersection(pending):
            return source_ids
        sources = list(source_ids)
        for event_id in pending:
            event = (await asyncio.to_thread(memory.read_chat, self.device_id, event_id, context=0))["event"]
            if event.get("person_id") == person_id and event_id not in sources:
                sources.append(event_id)
        if len(sources) > 20:
            raise ValueError("本次发言片段超过来源上限，请分项整理并明确选择来源")
        return sources

    async def _run(self):
        try:
            await asyncio.to_thread(resume_consolidation, self.device_id, expected_epoch=self.epoch)
        except MemoryEpochChanged:
            self.invalidated = True
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                action, data = item
                if not self.invalidated:
                    try:
                        await self._persist(action, data)
                    except MemoryEpochChanged:
                        self.invalidated = True
            finally:
                self.queue.task_done()

    async def _persist(self, action, data):
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        turn_id = data.get("source_turn_id") or data.get("turn_id")
        person = None
        basis = "unknown"
        if action == "input.transcript.done" and turn_id:
            # Wait for this exact turn's observation, never use another turn's identity.
            deadline = asyncio.get_running_loop().time() + 1.2
            while not self.closing and turn_id not in self.observations and asyncio.get_running_loop().time() < deadline:
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), max(.001, deadline - asyncio.get_running_loop().time()))
                except TimeoutError:
                    break
            person = await asyncio.to_thread(memory.resolve_speaker, self.device_id, self.observations.get(turn_id, {}), expected_epoch=self.epoch)
            if person:
                basis = "voice"
        if action == "input.transcript.done" and person is None:
            # An explicit self-introduction is a different attribution basis
            # from face/voice inference. Do not infer names from other clauses.
            declaration = re.search(r"(?:^|[，,。\s])我(?:叫|的名字是)([^，,。！!？?；;\n]{1,12})(?:[，,。！!；;]|$)", text.strip())
            if (declaration and not any(w in text for w in ("如果", "假如", "他说", "她说", "开玩笑", "“", '"'))
                    and not any(w in declaration[1] for w in ("喜欢", "住在", "正在", "准备", "想要"))):
                person = await asyncio.to_thread(memory.ensure_person, self.device_id, declaration[1].strip(), expected_epoch=self.epoch)
                basis = "self_declared"
        if action != "input.transcript.done" and turn_id in self.input_events:
            source = await asyncio.to_thread(memory.read_chat, self.device_id, self.input_events[turn_id], context=0)
            if pid := source["event"].get("person_id"):
                person = {"id": pid}
        kind = {"input.transcript.done": "user", "output.transcript.done": "assistant",
                "memory.action": "action", "memory.playback": "playback"}[action]
        status = data.get("status") or ("generated_playback_unconfirmed" if kind == "assistant" else "observed")
        event = await asyncio.to_thread(memory.record_chat_event, self.device_id, kind=kind, text=text,
                                        external_id=data["external_id"], person_id=person["id"] if person else None,
                                        identity_basis=basis, turn_id=turn_id,
                                        occurred_at=data["occurred_at"], status=status, expected_epoch=self.epoch)
        if action == "input.transcript.done":
            self.input_events[turn_id] = event["id"]
            self.pending_inputs[turn_id] = event["id"]
            while len(self.input_events) > 128:
                self.input_events.popitem(last=False)
            self.changed.set()
            await asyncio.to_thread(consolidate_event, self.device_id, event["id"], expected_epoch=self.epoch)
        elif action == "output.transcript.done" and turn_id in self.pending_inputs:
            # A later user's transcript can arrive while an older answer is
            # finishing. Retire only fragments through that answer's own turn.
            for pending_turn in list(self.pending_inputs):
                self.pending_inputs.pop(pending_turn)
                if pending_turn == turn_id:
                    break

    async def attribute_enrollment(self, person_id, turn_id):
        await self.flush()
        event_id = self.input_events.get(turn_id)
        if event_id and await asyncio.to_thread(memory.attribute_self_introduction, self.device_id, event_id, person_id, expected_epoch=self.epoch):
            await asyncio.to_thread(consolidate_event, self.device_id, event_id, expected_epoch=self.epoch)

    async def flush(self):
        if self.worker and self.worker.done():
            self.worker.result()
        joined = asyncio.create_task(self.queue.join())
        try:
            if self.worker:
                await asyncio.wait([joined, self.worker], return_when=asyncio.FIRST_COMPLETED)
                if self.worker.done():
                    self.worker.result()
            await joined
        finally:
            if not joined.done():
                joined.cancel()
                await asyncio.gather(joined, return_exceptions=True)

    async def close(self):
        self.closing = True
        self.changed.set()
        if self.worker:
            if not self.worker.done():
                stopped = asyncio.create_task(self.queue.put(None))
                try:
                    await asyncio.wait([stopped, self.worker], return_when=asyncio.FIRST_COMPLETED)
                    if self.worker.done():
                        self.worker.result()
                    await stopped
                finally:
                    if not stopped.done():
                        stopped.cancel()
                        await asyncio.gather(stopped, return_exceptions=True)
            await self.worker
        self.observations.clear()
        self.input_events.clear()
        self.used_sources.clear()
        self.pending_inputs.clear()
