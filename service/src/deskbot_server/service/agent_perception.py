"""Agent access to fresh automatic observations and device-scoped enrollment."""

from __future__ import annotations

import asyncio
import time

from deskbot_server.service.application.face_registration import register_face_for_device
from deskbot_server.service.application.voice_registration import register_voice_for_device
from deskbot_server.service.system_event_service import PerceptionEvents


class AgentPerception:
    def __init__(self, device_id, bridge):
        self.device_id = device_id
        self.bridge = bridge
        self.events = PerceptionEvents(device_id)

    async def execute(self, name, arguments, *, generation):
        registering = name.startswith("register_")
        allowed = {"name", "face_id", "person_id"} if name == "register_face" else {"name", "person_id"} if registering else set()
        if arguments.keys() - allowed:
            raise ValueError("识别或注册参数无效")
        if registering:
            epoch = getattr(getattr(self.bridge, "memory", None), "epoch", None)
            person = arguments.get("name")
            if not isinstance(person, str) or not 1 <= len(person.strip()) <= 80:
                raise ValueError("姓名需要 1～80 字")
            person = person.strip()
        turn_id = getattr(getattr(self.bridge, "client", None), "_turn_id", None)
        if name in {"recognize_voiceprint", "register_voiceprint"}:
            if not turn_id:
                raise ValueError("没有本轮声音，请先对机器人说一句完整的话")
            event = await self.events.for_turn(turn_id, wait_seconds=1.2)
            observation = event.as_dict()["data"]["speaker"]
        else:
            deadline = time.monotonic() + 1.2
            while True:
                observation = self.events.snapshot(turn_id or "tool").as_dict()["data"]["camera"]
                if observation["state"] not in {"stale", "unavailable"} or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(.025)
        if not self.bridge._current(generation):
            return {"ok": False, "status": "cancelled", "error": "本轮已被打断或设备已重连"}
        if registering:
            if name == "register_face":
                if observation["state"] != "detected":
                    raise ValueError("当前没有新鲜可用的人脸，请正对镜头并确保人脸识别服务已开启")
                face_id = arguments.get("face_id")
                if face_id is not None and (type(face_id) is not int or face_id < 0):
                    raise ValueError("face_id 必须是当前画面的人脸整数编号")
                result = await asyncio.to_thread(register_face_for_device, self.device_id, person,
                                                 face_id=face_id, max_age_s=3, person_id=arguments.get("person_id"), memory_epoch=epoch)
            else:
                result = await asyncio.to_thread(register_voice_for_device, self.device_id, person, request_id=turn_id,
                                                 person_id=arguments.get("person_id"), memory_epoch=epoch)
                # The enrollment and memory link report independent outcomes.
            return {"ok": True, "status": "registered", "name": result["profile"].get("name"),
                    "profile_id": result["profile"].get("id"),
                    "person_id": result["profile"].get("memory_person_id"),
                    "memory_status": result["profile"].get("memory_status", "pending"),
                    **({"face_id": result["face_id"]} if name == "register_face" else {})}
        key = "camera" if name == "recognize_face" else "speaker"
        state = observation["state"]
        ok = state in {"detected", "empty", "found", "unknown"}
        result = {"ok": ok, key: observation}
        if not ok:
            result["error"] = ("暂无本轮可用识别结果，请确认感知服务已开启，并正对镜头或说一句完整的话后重试")
        return result
