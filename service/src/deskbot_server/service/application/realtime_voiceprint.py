"""Bounded voice-only sampling, independent of realtime ASR/LLM/TTS."""

import asyncio
import logging
import time

import numpy as np

from deskbot_server.service.voiceprint_service import VoiceprintService

logger = logging.getLogger("deskbot-server")


class RealtimeVoiceprint:
    """Collect short voiced segments; never queue inference or save PCM to disk."""

    def __init__(self, device_id):
        self.device_id = device_id
        self.service = VoiceprintService()
        self.audio = bytearray()
        self.voiced_frames = 0
        self.quiet_frames = 0
        self.task = None
        self.closed = False
        self.mute_until = 0.0

    def reset(self):
        self.audio.clear()
        self.voiced_frames = self.quiet_frames = 0

    def feed(self, pcm, *, playback=False):
        if self.closed:
            return
        if playback:
            self.mute_until = time.monotonic()+0.5
        if not self.service.enabled() or playback or time.monotonic() < self.mute_until:
            self.reset()
            return
        if len(pcm) != 640:
            return
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        voiced = float(np.sqrt(np.mean(samples*samples))) >= 200
        if not voiced and not self.audio:
            return
        self.audio.extend(pcm)
        self.voiced_frames += int(voiced)
        self.quiet_frames = 0 if voiced else self.quiet_frames+1
        if self.quiet_frames < 30 and len(self.audio) < 8*32000:
            return
        if self.voiced_frames >= 30 and (self.task is None or self.task.done()):
            audio = bytes(self.audio)
            self.task = asyncio.create_task(self._identify(audio))
        self.reset()

    async def _identify(self, pcm):
        try:
            await self.service.identify(device_id=self.device_id, pcm_bytes=pcm, sample_rate=16000)
        except Exception:
            # Perception failures must never terminate a conversation session.
            logger.warning("[vpr] realtime sample failed device_id=%s", self.device_id)

    async def close(self):
        self.closed = True
        self.reset()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
