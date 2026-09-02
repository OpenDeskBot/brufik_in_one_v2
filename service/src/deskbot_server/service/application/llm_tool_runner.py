"""执行 LLM JSON 中的 ``tools`` 指令。"""

from __future__ import annotations

import logging
from typing import Any

from deskbot_server.dao.device_mapper import get_camera_servo_auto_mode, set_camera_servo_auto_mode
from deskbot_server.dao.device_memory_mapper import add_memory, delete_memory
from deskbot_server.dao.device_session_mapper import execute_session_tool
from deskbot_server.service.application.face_registration import register_face_for_device
from deskbot_server.service.application.voice_registration import register_voice_for_device
from deskbot_server.service.camera_face_service import capture_camera_for_device_async
from deskbot_server.service.miot_tools import execute_miot_tool
from deskbot_server.service.quest_service import QuestService
from deskbot_server.service.scheduled_task_service import execute_schedule_task_tool
from deskbot_server.service.web_tools import webfetch, websearch

logger = logging.getLogger("deskbot-server")

_FOLLOW_ALIASES = {
    "": "",
    "off": "",
    "none": "",
    "关闭": "",
    "关": "",
    "follow": "follow",
    "跟随": "follow",
    "跟随人脸": "follow",
    "follow_frontal": "follow_frontal",
    "正脸": "follow_frontal",
    "跟随正脸": "follow_frontal",
    "gaze": "gaze",
    "注视": "gaze",
    "注视感知": "gaze",
}


def _normalize_follow_mode(raw: object) -> str:
    key = str(raw or "").strip().lower()
    if key in _FOLLOW_ALIASES:
        return _FOLLOW_ALIASES[key]
    return str(raw or "").strip()


def _require_quest_playbook(device_id: str) -> str:
    """解析设备绑定剧本（playbook 参数从 devices.quest_id 来，LLM 可不传）。"""
    playbook = QuestService().get_bound_playbook(device_id)
    if not playbook:
        raise ValueError("设备未绑定剧情剧本，任务工具不可用")
    return playbook


async def execute_llm_tools(
    tools: list[dict[str, Any]],
    *,
    device_id: str | None = None,
    session_id: str | None = None,
    device_ws: Any = None,
    cam_fps: int | None = None,
) -> list[dict[str, Any]]:
    """逐条执行工具，返回结果摘要（供日志与 pipeline 事件）。"""
    results: list[dict[str, Any]] = []
    dev = str(device_id or "").strip()
    boost_fps = cam_fps or 5
    for raw in tools or []:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or raw.get("name") or "").strip()
        if not tool:
            continue
        try:
            if tool == "register_face":
                name = str(raw.get("name") or raw.get("person_name") or "").strip()
                fid_raw = raw.get("face_id")
                face_id = int(fid_raw) if fid_raw is not None else None
                out = register_face_for_device(dev, name, face_id=face_id)
                results.append(
                    {
                        "tool": tool,
                        "ok": True,
                        "profile_id": out["profile"].get("id"),
                        "name": out["profile"].get("name"),
                        "face_id": out.get("face_id"),
                    }
                )
            elif tool == "register_voiceprint":
                name = str(raw.get("name") or raw.get("person_name") or "").strip()
                out = register_voice_for_device(dev, name)
                results.append(
                    {
                        "tool": tool,
                        "ok": True,
                        "profile_id": out["profile"].get("id"),
                        "name": out["profile"].get("name"),
                    }
                )
                cap = await capture_camera_for_device_async(dev, hub=device_ws, cam_fps=boost_fps)
                if not cap.get("ok"):
                    results.append({"tool": tool, "ok": False, "error": cap.get("error")})
                else:
                    results.append({"tool": tool, **cap})
            elif tool in ("set_camera_follow", "set_camera_follow_mode", "camera_follow"):
                mode = _normalize_follow_mode(raw.get("mode") or raw.get("value"))
                if mode not in ("", "follow", "follow_frontal", "gaze"):
                    raise ValueError(f"未知跟随模式: {mode!r}")
                before = get_camera_servo_auto_mode(device_id)
                norm = set_camera_servo_auto_mode(device_id, mode)
                already = before == norm
                row: dict[str, Any] = {"tool": tool, "ok": True, "mode": norm}
                if already:
                    row["already_active"] = True
                    row["hint"] = (
                        f"摄像头跟随已是 {norm or '关闭'}，无需再次调用。请返回完整 JSON（tools:[] + tts 回答用户）。"
                    )
                results.append(row)
            elif tool == "memory_add":
                text = str(raw.get("text") or raw.get("value") or "").strip()
                if not text:
                    raise ValueError("memory_add 需要 text")
                entry = add_memory(text, device_id=dev or None)
                results.append({"tool": tool, "ok": True, "id": entry["id"], "text": entry["text"]})
            elif tool == "memory_delete":
                eid = str(raw.get("id") or "").strip()
                if not eid:
                    raise ValueError("memory_delete 需要 id")
                ok = delete_memory(eid, device_id=dev or None)
                if not ok:
                    raise ValueError(f"未找到记忆 id={eid}")
                results.append({"tool": tool, "ok": True, "id": eid})
            elif tool in ("schedule_task", "scheduled_task"):
                out = execute_schedule_task_tool(raw, device_id=dev, default_session_id=session_id)
                results.append(out)
            elif tool == "session":
                out = execute_session_tool(raw, device_id=dev)
                results.append(out)
            elif tool == "update_task_result":
                playbook = _require_quest_playbook(dev)
                task_id = str(raw.get("task_id") or "").strip()
                if not task_id:
                    raise ValueError("update_task_result 需要 task_id")
                out = QuestService().update_task_result(
                    dev, playbook, task_id,
                    str(raw.get("status") or "").strip(),
                    str(raw.get("result") or "").strip(),
                )
                results.append({"tool": tool, "ok": True, **out})
            elif tool == "update_task_strategy":
                playbook = _require_quest_playbook(dev)
                task_id = str(raw.get("task_id") or "").strip()
                if not task_id:
                    raise ValueError("update_task_strategy 需要 task_id")
                out = QuestService().update_task_strategy(
                    dev, playbook, task_id, str(raw.get("strategy") or "").strip()
                )
                results.append({"tool": tool, "ok": True, **out})
            elif tool == "contribute_score":
                playbook = _require_quest_playbook(dev)
                task_id = str(raw.get("task_id") or "").strip()
                if not task_id:
                    raise ValueError("contribute_score 需要 task_id")
                out = QuestService().contribute_score(dev, playbook, task_id, int(raw.get("points") or 0))
                results.append({"tool": tool, "ok": True, **out})
            elif tool in ("miot", "mihome", "mijia"):
                if not dev:
                    raise ValueError("miot 需要 device_id")
                out = execute_miot_tool(raw, device_id=dev)
                results.append(out)
            elif tool == "webfetch":
                url = str(raw.get("url") or "").strip()
                out = webfetch(url)
                results.append({"tool": tool, **out})
            elif tool == "websearch":
                query = str(raw.get("query") or raw.get("q") or "").strip()
                max_results = raw.get("max_results") or raw.get("limit") or 5
                out = websearch(query, max_results=int(max_results))
                results.append({"tool": tool, **out})
            else:
                results.append({"tool": tool, "ok": False, "error": f"未知工具: {tool}"})
        except Exception as exc:
            logger.warning("[LLM tools] %s 失败: %s", tool, exc)
            results.append({"tool": tool, "ok": False, "error": str(exc)})
    return results
