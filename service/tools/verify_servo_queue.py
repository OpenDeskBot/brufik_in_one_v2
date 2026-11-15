"""Verify real append ordering and a downloaded optional scene over the HTTP API.

Moves only the explicitly selected robot two degrees out and back. Uses the
existing owner's debug credential locally; never writes settings or saves it.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx


def verify(args):
    from deskbot_server.auth.debug_ws_token import issue_debug_ws_token
    from deskbot_server.dao.device_mapper import get_by_device_id

    device = get_by_device_id(args.device_id)
    if device is None:
        raise ValueError("unknown device")
    headers = {"X-Deskbot-Debug-Token": issue_debug_ws_token(device.owner_user_id).token}
    report = {"device_id": args.device_id, "completed": False, "requests": [], "acks": []}
    try:
        with httpx.Client(base_url=args.base_url, headers=headers, timeout=5) as client:
            start = time.monotonic()
            for delta, scene in ((2, "curious"), (-2, "")):
                response = client.get(
                    "/api/device_servo",
                    params={
                        "device_id": args.device_id,
                        "dyaw": delta,
                        "dpitch": 0,
                        "xm": 1,
                        "ym": 1,
                        "ms": 300,
                        "level": 3,
                        "action": "append",
                        "with_scene": scene,
                    },
                )
                response.raise_for_status()
                body = response.json()
                assert body["delivered"] == 1 and body["action"] == "append", body
                report["requests"].append(
                    {
                        "req": body["req"],
                        "type": body["type"],
                        "scene_req": body["scene_req"],
                        "scene_frames": body["scene_frames"],
                        "elapsed_ms": (time.monotonic() - start) * 1000,
                    }
                )
            first, second = report["requests"]
            assert first["scene_frames"] > 0 and first["scene_req"] == first["req"], first
            wanted = {first["req"], second["req"]}
            while time.monotonic() - start < 15:
                response = client.get("/api/devices")
                response.raise_for_status()
                row = next(d for d in response.json()["devices"] if d["device_id"] == args.device_id)
                ack = row.get("last_pb_ack") or {}
                if ack.get("req") in wanted and ack.get("ack_type") == "pb_end":
                    report["acks"].append({**ack, "elapsed_ms": (time.monotonic() - start) * 1000})
                    wanted.remove(ack["req"])
                if not wanted:
                    break
                time.sleep(0.025)
            assert [a["req"] for a in report["acks"]] == [first["req"], second["req"]], report["acks"]
            assert report["acks"][0]["idx"] == first["scene_frames"]
            assert report["acks"][1]["idx"] == 0
            report["completed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    from deskbot_server.utils.env import load_dotenv

    load_dotenv()
    raise SystemExit(verify(args))
