"""Run the normal backend plus one real-device voice-output latency check.

No test endpoint is added. Inputs are text fixtures, so microphone/ASR latency
is excluded. Playback acknowledgments come from the physical robot.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from deskbot_server.service.external.manager import ServiceState


async def verify(runtime, device_id, output):
    from deskbot_server.service.application.ws_chat_turn import run_ws_chat_turn
    hub = runtime.device_ws
    original = hub.record_pb_ack
    events = []
    report = {"device_id": device_id, "scope": "text input to real device I2S ACK; ASR excluded", "trials": []}
    feedback = []

    class Capture(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if ("[ASR echo]" in message or "[barge-in]" in message or
                    "[ASR] 识别成功" in message) and device_id in message:
                feedback.append(message)

    capture = Capture()
    logger = logging.getLogger("deskbot-server")
    logger.addHandler(capture)

    async def record(dev, ack):
        if dev == device_id:
            events.append({**ack, "host_mono": time.monotonic()})
        await original(dev, ack)

    live = hub._live_service
    hub._live_service = None
    hub.record_pb_ack = record
    try:
        async with asyncio.timeout(120):
            while True:
                entry = hub._devices.get(device_id)
                status = runtime.external_manager.snapshot("llm-realtime")
                if (entry and {"pb_stream_v1", "raw_preview_pcm_v1"} <= entry.capabilities and entry.audio_ready_mono
                        and status and status.state == ServiceState.RUNNING and status.healthy):
                    break
                await asyncio.sleep(.1)
        await runtime.chat.warmup_realtime(device_id)
        # Preserve the old conversation; isolate fixtures from earlier feedback
        # loops which otherwise teach the model to repeat those fragments.
        from deskbot_server.service.session_service import create_session
        await asyncio.to_thread(create_session, device_id, title="回声修复验证")
        await hub.interrupt_current_playback(device_id, reason="voice_latency_fixture")
        async with asyncio.timeout(5):
            while entry.sending_seq is not None:
                await asyncio.sleep(.01)
        inputs = ["在吗？", "现在几点了？", "今天有点累，你陪我聊两句。", "你能给我讲个很短的小笑话吗？"]
        for index, text in enumerate(inputs):
            req = f"realtime_verify_{index}"
            started = time.monotonic()
            result = await run_ws_chat_turn(entry.ws, runtime.chat, text, request_id=req,
                                            t_asr_start=started, t_asr_text=started,
                                            device_ws=hub, device_id=device_id, bus_service=runtime.bus_service)
            first = next((e for e in events if e.get("req") == req and e.get("ack_type") == "pb_started"), None)
            report["trials"].append({"input": text, "req": req, "reply": result.get("llm_text"),
                                     "first_output_ack_ms": round((first["host_mono"]-started)*1000, 1) if first else None,
                                     "device_queue_wait_ms": first.get("queue_wait_ms") if first else None,
                                     "status": result.get("playback_status"), "error": result.get("error")})
        report["completed"] = all(t["status"] == "completed" for t in report["trials"])
        await asyncio.sleep(3)
    except asyncio.CancelledError:
        report["completed"] = False
        report["interrupted_by_live_input"] = True
    except Exception as exc:
        report["completed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        hub.record_pb_ack = original
        hub._live_service = live
        logger.removeHandler(capture)
        report["microphone_events"] = feedback
        report["raw_preview_negotiated"] = bool(hub._devices.get(device_id) and hub._devices[device_id].raw_preview_seen)
        report["events"] = events
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k != "events"}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    import deskbot_server.main as server_main
    from deskbot_server.__main__ import cli
    factory = server_main.create_fastapi_app

    def create(runtime):
        app = factory(runtime)
        asyncio.create_task(verify(runtime, args.device_id, args.output), name="real-device-voice-verification")
        return app
    server_main.create_fastapi_app = create
    cli()


if __name__ == "__main__":
    main()
