"""Bounded short-phrase TTS pipeline; each turn owns and cancels its workers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any


class StreamingSpeech:
    def __init__(
        self,
        synthesize: Callable[[str], Awaitable[Any]],
        play: Callable[[str, Any, int], Awaitable[None]],
        clean: Callable[[str], str],
        *,
        max_chars: int = 16,
        wait_seconds: float = 0.18,
    ) -> None:
        self._synthesize, self._play, self._clean = synthesize, play, clean
        self._max_chars, self._wait_seconds = max_chars, wait_seconds
        self._phrases: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._audio: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._buffer = ""
        self._raw = ""
        self._blocked = False
        self._lock = asyncio.Lock()
        self.used = False
        self.round_used = False
        self.last_round_text = ""
        self._round_spoken: list[str] = []
        self._tasks = [asyncio.create_task(self._synth_loop()), asyncio.create_task(self._play_loop())]
        self._timer: asyncio.Task | None = None

    async def _put(self, queue: asyncio.Queue, value: Any) -> None:
        # Propagate worker failure even if a bounded queue is full.
        for task in self._tasks:
            if task.done():
                task.result()
        put = asyncio.create_task(queue.put(value))
        try:
            done, _ = await asyncio.wait([put, *self._tasks], return_when=asyncio.FIRST_COMPLETED)
            for task in self._tasks:
                if task in done:
                    task.result()
            await put
        finally:
            if not put.done():
                put.cancel()
                await asyncio.gather(put, return_exceptions=True)

    async def _emit(self, phrase: str) -> None:
        spoken = self._clean(phrase)
        if spoken:
            await self._put(self._phrases, spoken)
            self._round_spoken.append(spoken)
            self.used = self.round_used = True

    async def feed(self, delta: str) -> None:
        async with self._lock:
            self._raw += delta
            # Legacy envelopes, fenced output and reasoning prefixes are held
            # for the existing whole-response parser, never read aloud as JSON.
            if self._raw.lstrip().startswith(("{", "[", "`", "<", "(", "（")):
                self._blocked = True
            if self._blocked:
                return
            self._buffer += delta
            if self._timer is None:
                self._timer = asyncio.create_task(self._flush_timer())
            while self._buffer:
                match = re.search(r"[。！？!?；;，,：:\n]", self._buffer)
                end = match.end() if match and match.end() >= 6 else 0
                if len(self._buffer) >= self._max_chars:
                    end = min(end or self._max_chars, self._max_chars)
                if not end:
                    break
                phrase, self._buffer = self._buffer[:end], self._buffer[end:]
                await self._emit(phrase)

    async def end_round(self) -> bool:
        async with self._lock:
            if not self._blocked and self._buffer:
                await self._emit(self._buffer)
            used = self.round_used
            if self._raw or self._round_spoken:
                self.last_round_text = "".join(self._round_spoken)
            self._round_spoken.clear()
            self._buffer = self._raw = ""
            self._blocked = self.round_used = False
            return used

    async def _flush_timer(self) -> None:
        while True:
            await asyncio.sleep(self._wait_seconds)
            async with self._lock:
                if not self._blocked and len(self._buffer) >= 8:
                    phrase, self._buffer = self._buffer, ""
                    await self._emit(phrase)

    async def _synth_loop(self) -> None:
        while True:
            phrase = await self._phrases.get()
            if phrase is None:
                await self._audio.put(None)
                return
            audio = await self._synthesize(phrase)
            await self._audio.put((phrase, audio))

    async def _play_loop(self) -> None:
        index = 0
        while True:
            item = await self._audio.get()
            if item is None:
                return
            phrase, audio = item
            await self._play(phrase, audio, index)
            index += 1

    async def finish(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
        await self.end_round()
        await self._put(self._phrases, None)
        await asyncio.gather(*self._tasks)

    async def cancel(self) -> None:
        tasks = [*self._tasks, *([self._timer] if self._timer is not None else [])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
