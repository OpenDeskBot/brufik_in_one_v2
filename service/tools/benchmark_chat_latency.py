"""Compare whole-response vs streamed speech using existing local model/TTS.

No model processes are launched. Uses a temporary DB and an acknowledging device
sink: the endpoint is first PB submission, not physical speaker playback. Both
modes use the updated motion/queue policy; this isolates the streaming benefit.
Run from service/: .venv/bin/python tools/benchmark_chat_latency.py --output ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from deskbot_server.service.llm_service import OpenAiLlmAdapter
from deskbot_server.client.llm.runtime import ResolvedLlmConfig
from deskbot_server.client.tts.moss_adapter import _TTS_CACHE, MossTtsAdapter
from deskbot_server.model.pb_seq import PlaybackOutcome, PlaybackStatus
from deskbot_server.model.settings import AppSettings
from deskbot_server.service.application.chat_flow import run_chat_turn
from deskbot_server.service.application.chat_service import ChatService


class DeviceSink:
    def __init__(self):
        self.first_audio = None
        self.blocks = 0

    async def pb_ack_llm_context(self, _device_id):
        return None

    def _get_ws(self, _device_id):
        return None

    async def interrupt_current_playback(self, *_args, **_kwargs):
        return 0

    async def send_and_wait(self, _device_id, seq):
        self.blocks += seq.block_count
        if self.first_audio is None and any(block.binaries for block in seq.entries):
            self.first_audio = time.monotonic()
        return PlaybackOutcome(PlaybackStatus.COMPLETED, req=seq.req)


class Stages:
    async def emit_stage(self, *_args, **_kwargs):
        pass


async def benchmark(args):
    cfg = ResolvedLlmConfig(
        args.model,
        "",
        args.model_url,
        "openai",
        "test",
        args.model,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0, "seed": 42},
    )
    settings = AppSettings.from_config(
        {
            "llm": {"system_prompt": ("你是中文语音助手。只回答简短自然的口语纯文本，不输出 JSON、思考或括号说明。")},
            "tts": {"sample_rate": 16000},
        }
    )
    results = []
    reference_text = "傍晚的公园很安静，夕阳把树叶染成金黄色。微风轻轻吹过湖面，远处传来清脆的鸟鸣。"
    with tempfile.TemporaryDirectory(prefix="deskbot-latency-") as directory:
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        os.environ["DESKBOT_DB_PATH"] = str(Path(directory) / "test.db")
        reset_engine()
        init_engine(Path(directory) / "test.db")
        init_database()
        with (
            patch("deskbot_server.service.llm_service.resolve_llm_config", return_value=cfg),
            patch("deskbot_server.client.llm.runtime.resolve_llm_config", return_value=cfg),
        ):
            for i in range(args.rounds):
                # Alternate order to reduce warm/cache order bias. TTS client
                # cache is cleared before each sample; the model stays loaded.
                modes = [False, True] if i % 2 == 0 else [True, False]
                for streamed in modes:
                    _TTS_CACHE.clear()
                    adapter = OpenAiLlmAdapter(settings)

                    class MeasuredChat(ChatService):
                        async def tts_phoneme_segments(self, text, **kwargs):
                            if not hasattr(self, "first_synth_start"):
                                self.first_synth_start = time.monotonic()
                                self.first_phrase = text
                            audio = await super().tts_phoneme_segments(text, **kwargs)
                            if not hasattr(self, "first_synth_end"):
                                self.first_synth_end = time.monotonic()
                            return audio

                    chat = MeasuredChat(settings, asr=None, llm=adapter, tts=MossTtsAdapter(settings))
                    chat.supports_streaming = streamed
                    sink = DeviceSink()
                    start = time.monotonic()
                    try:
                        turn = await run_chat_turn(
                            Stages(),
                            chat,
                            f"小歪，请原样复述这段话，不要增加或删减文字：{reference_text}",
                            device_id=f"latency_{i}_{int(streamed)}",
                            request_id=f"bench_{i}_{int(streamed)}",
                            device_ws=sink,
                        )
                        if turn.status == "error" or sink.first_audio is None:
                            raise RuntimeError(turn.error or "no audio produced")
                        row = {
                            "round": i + 1,
                            "mode": "streamed" if streamed else "whole_response",
                            "first_pb_ms": round((sink.first_audio - start) * 1000, 1),
                            "llm_round_return_ms": round((turn.t_llm_end - start) * 1000, 1),
                            "first_pb_before_llm_round_return": sink.first_audio < turn.t_llm_end,
                            "first_model_delta_ms": round((turn.t_llm_first_token - start) * 1000, 1)
                            if turn.t_llm_first_token
                            else None,
                            "first_synth_start_ms": round((chat.first_synth_start - start) * 1000, 1),
                            "first_synth_ms": round((chat.first_synth_end - chat.first_synth_start) * 1000, 1),
                            "first_phrase": chat.first_phrase,
                            "matches_reference_text": turn.llm_text == reference_text,
                            "generated_chars": len(turn.llm_text or ""),
                            "pb_blocks": sink.blocks,
                            "output_text": turn.llm_text,
                        }
                        results.append(row)
                        print(json.dumps(row, ensure_ascii=False), flush=True)
                    finally:
                        await chat.aclose()
        reset_engine()
    summary = {
        mode: statistics.median(r["first_pb_ms"] for r in results if r["mode"] == mode)
        for mode in ["whole_response", "streamed"]
    }
    report = {
        "scope": "real local LLM + MOSS TTS + application flow + PB assembly; simulated immediate playback ACK",
        "excludes": ["microphone/VAD/ASR", "voice decision gate (text input)", "physical device transport/playback"],
        "comparison": "same updated motion/queue behavior; only whole-response vs streaming differs",
        "reference_text": reference_text,
        "sampling": {"temperature": 0, "seed": 42},
        "model": args.model,
        "model_url": args.model_url,
        "tts": "existing moss-tts-nano at 127.0.0.1:9101; client cache cleared per sample",
        "samples": results,
        "median_first_pb_ms": summary,
        "median_reduction_percent": round((1 - summary["streamed"] / summary["whole_response"]) * 100, 1),
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": summary, "output": args.output}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-url", default="http://127.0.0.1:9106/v1")
    parser.add_argument("--model", default="qwen3.8-2b")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(benchmark(args))
