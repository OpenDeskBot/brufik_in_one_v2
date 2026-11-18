"""Build bounded per-device events without exposing biometric vectors or PCM."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from deskbot_server.model.system_event import SystemEvent, timestamp
from deskbot_server.service.application.face_snapshot_cache import get_perception_snapshot
from deskbot_server.service.application.voice_snapshot_cache import get_voice_snapshot
from deskbot_server.service.camera_face_service import CameraFaceService
from deskbot_server.service.voiceprint_service import VoiceprintService


def confidence(value):
    try:
        value = float(value)
        return round(max(0, min(1, value)), 3) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def text(value, limit=80):
    return str(value or "").strip()[:limit]


def age_ms(observed_at, now):
    if type(observed_at) not in (int, float) or not math.isfinite(observed_at):
        return None
    age = now-observed_at
    return int(age*1000) if 0 <= age <= 86400 else None


class PerceptionEvents:
    def __init__(self, device_id):
        self.device_id = device_id

    async def for_turn(self, turn_id, *, wait_seconds=.8):
        voice = VoiceprintService()
        deadline = time.monotonic()+wait_seconds
        while voice.enabled() and time.monotonic() < deadline:
            result = get_voice_snapshot(self.device_id)
            if result and result.get("request_id") == turn_id and result.get("state") != "identifying":
                break
            await asyncio.sleep(.025)
        return self.snapshot(turn_id)

    def snapshot(self, turn_id, *, now=None):
        now = time.time() if now is None else now
        face = CameraFaceService()
        camera = {"state": "unavailable", "people": []}
        if face.is_configured() and face.runtime.mode == "none":
            camera["state"] = "disabled"
        elif face.is_configured():
            snapshot = get_perception_snapshot(self.device_id)
            age = age_ms(snapshot.get("observed_at"), now)
            camera["state"] = "stale" if age is not None and age > 3000 else "unavailable"
            if age is not None and age <= 3000:
                people = []
                for person in snapshot["people"][:5]:
                    item = {"track_id": person["track_id"], "recognized": bool(person.get("name"))}
                    score = confidence(person.get("confidence"))
                    if item["recognized"] and score is not None:
                        item.update(name=text(person["name"]), profile_id=text(person.get("profile_id")), confidence=score)
                    else:
                        item["recognized"] = False
                    people.append(item)
                camera = {"state": "detected" if snapshot["count"] else "empty", "age_ms": age,
                          "observed_at": timestamp(datetime.fromtimestamp(snapshot["observed_at"], UTC)),
                          "expires_at": timestamp(datetime.fromtimestamp(snapshot["observed_at"], UTC)+timedelta(seconds=3)),
                          "count": snapshot["count"], "people": people}
        speaker = {"state": "disabled" if not VoiceprintService().enabled() else "pending"}
        result = get_voice_snapshot(self.device_id)
        if speaker["state"] != "disabled" and result and result.get("request_id") == turn_id:
            age = age_ms(result.get("ts"), now)
            if age is None or age > 5000:
                speaker = {"state": "stale"}
            else:
                state = result.get("state")
                observed = datetime.fromtimestamp(result["ts"], UTC)
                speaker = {"state": state if state in {"found", "unknown", "degraded"} else "pending", "age_ms": age,
                           "observed_at": timestamp(observed), "expires_at": timestamp(observed+timedelta(seconds=5))}
                score = confidence(result.get("score"))
                if speaker["state"] == "found" and result.get("name") and score is not None:
                    speaker.update(name=text(result["name"]), confidence=score)
                    if result.get("profile_id"):
                        speaker["profile_id"] = text(result["profile_id"])
                elif speaker["state"] == "found":
                    speaker = {"state": "unknown"}
        return SystemEvent.create("perception_snapshot", "perception-"+turn_id, "perception",
                                  {"turn_id": turn_id, "camera": camera, "speaker": speaker},
                                  now=datetime.fromtimestamp(now, UTC))


def reminder_event(device_id, content, *, task_id=None, scheduled_at=None, now=None):
    now = now or datetime.now(UTC)
    scheduled = datetime.fromisoformat(scheduled_at) if isinstance(scheduled_at, str) else scheduled_at or now
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    scheduled = scheduled.astimezone(UTC)
    identity = f"{device_id}|{task_id}|{scheduled.isoformat()}"
    event_id = "reminder-"+hashlib.sha256(identity.encode()).hexdigest()[:32]
    return SystemEvent.create("reminder_due", event_id, "scheduler",
                              {"task_id": text(task_id, 128), "scheduled_at": scheduled.isoformat(),
                               "fired_at": now.isoformat(), "content": text(content, 1000)}, now=now, ttl_seconds=45)
