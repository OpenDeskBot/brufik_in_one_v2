"""One bounded response stream shared by all synthesized phrases."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from deskbot_server.model.pb_seq import PbBlock, PbSeq, PbType, PlaybackOutcome


class PbSpeechStream:
    def __init__(self, seq: PbSeq):
        self.seq = seq
        self.index = 0
        self.pts_ms = 0
        self.closed = False

    async def _put(self, block: PbBlock) -> None:
        if self.seq.outcome is not None:
            raise RuntimeError(f"speech stream ended: {self.seq.outcome}")
        put = asyncio.create_task(self.seq.source.put(block))
        ended = asyncio.create_task(self.seq._done.wait())
        try:
            await asyncio.wait([put, ended], return_when=asyncio.FIRST_COMPLETED)
            if self.seq.outcome is not None:
                raise RuntimeError(f"speech stream ended: {self.seq.outcome}")
            await put
        finally:
            for task in (put, ended):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put, ended, return_exceptions=True)

    async def append(self, pairs: list[tuple[dict, list[bytes]]]) -> None:
        if self.closed:
            raise RuntimeError("speech stream is closed")
        for wire, binaries in pairs:
            block = PbBlock.from_wire(wire, tuple(binaries))
            # Phrase END/SINGLE is payload, not a response-wide playback barrier.
            block = replace(block, req=self.seq.req, idx=self.index,
                            type=PbType.START if self.index == 0 else PbType.CHUNK,
                            epoch=self.seq.epoch, stream=True, pts_ms=self.pts_ms)
            await self._put(block)
            self.index += 1
            self.pts_ms += max(0, block.chunk_ms)

    async def finish(self) -> PlaybackOutcome:
        if not self.closed:
            self.closed = True
            await self._put(PbBlock(type=PbType.END, req=self.seq.req, idx=self.index,
                                    epoch=self.seq.epoch, stream=True, pts_ms=self.pts_ms))
        await self.seq._done.wait()
        return self.seq.outcome
