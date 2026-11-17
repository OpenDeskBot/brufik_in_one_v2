"""Device-scoped robot tool registration and preset compilation. No model calls."""

from __future__ import annotations

import json
from typing import Any

from deskbot_server.model.robot_performance import ROBOT_TOOL_NAME, RobotPerformance
from deskbot_server.pb.llm_plan import expand_llm_anims, expand_llm_moves
from deskbot_server.service.llm_context import list_llm_expression_names, list_llm_expression_titles, list_llm_move_ids

ROBOT_TOOL_INSTRUCTIONS = """
机器人身体表达：robot_perform 是本设备提供的动作与表情工具。
用户明确要求点头、转头或表情时调用；聊天时可适度使用，每轮最多调用一次。
只使用工具枚举中的名称，none 表示该项不执行；look_left/right 是机器人自身的左右。
调用工具的这一轮只输出工具调用，不同时输出口播；工具回传后再正常回答，避免重复播报。
一般使用 with_speech，让表现与随后的语音同时开始；before_speech/after_speech 用于明确的先后顺序。
用户要求只做动作、不说话时设置 speak=false，工具完成后保持安静。
scheduled 只表示已安排，不能声称动作已经完成。不要连续调用同一工具来查询执行状态。
""".strip()


class RobotPerformanceService:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.gestures, self.expressions = self._catalog()

    def _catalog(self) -> tuple[list[str], list[str]]:
        def names(values):
            return sorted({v for v in values if v and v not in {"none", "__custom__"}})[:128]
        return (names(list_llm_move_ids(device_id=self.device_id)),
                names(list_llm_expression_names(device_id=self.device_id)))

    def schema(self) -> dict[str, Any] | None:
        if not self.gestures and not self.expressions:
            return None
        titles = list_llm_expression_titles(device_id=self.device_id)
        return {
            "type": "function", "name": ROBOT_TOOL_NAME,
            "description": "执行当前机器人的一个动作和一个表情；每轮最多一次。none 表示不执行该项。"
                           "只做动作不说话时 speak=false。scheduled 仅表示已安排。",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "gesture": {"type": "string", "enum": ["none", *self.gestures],
                                "description": "舵机动作，左右按机器人自身视角理解"},
                    "expression": {"type": "string", "enum": ["none", *self.expressions],
                                   "description": "表情：" + "；".join(f"{n}={titles.get(n, n)}" for n in self.expressions)},
                    "duration_ms": {"type": "integer", "minimum": 200, "maximum": 3000,
                                    "description": "表现时长，通常 600～1000 毫秒"},
                    "timing": {"type": "string", "enum": ["before_speech", "with_speech", "after_speech"]},
                    "speak": {"type": "boolean", "default": True,
                              "description": "false=仅执行身体表现，完成后不再生成语音；省略为 true"},
                },
                "required": ["gesture", "expression", "duration_ms", "timing"],
            },
        }

    def compile(self, arguments: dict) -> RobotPerformance:
        required = {"gesture", "expression", "duration_ms", "timing"}
        if not isinstance(arguments, dict) or not required <= arguments.keys() or arguments.keys() - required - {"speak"}:
            raise ValueError("robot_perform 参数字段无效")
        gesture, expression = arguments["gesture"], arguments["expression"]
        current_gestures, current_expressions = self._catalog()
        if not isinstance(gesture, str) or gesture not in ["none", *set(self.gestures).intersection(current_gestures)]:
            raise ValueError("当前设备不支持该动作")
        if not isinstance(expression, str) or expression not in ["none", *set(self.expressions).intersection(current_expressions)]:
            raise ValueError("当前设备不支持该表情")
        if gesture == expression == "none":
            raise ValueError("至少指定一个动作或表情")
        duration = arguments["duration_ms"]
        if type(duration) is not int or not 200 <= duration <= 3000:
            raise ValueError("duration_ms 必须为 200～3000 的整数")
        timing, speak = arguments["timing"], arguments.get("speak", True)
        if timing not in ("before_speech", "with_speech", "after_speech") or not isinstance(speak, bool):
            raise ValueError("timing 或 speak 无效")
        servo = expand_llm_moves([{"move": gesture, "ms": duration}], device_id=self.device_id) if gesture != "none" else []
        anim = expand_llm_anims([{"anim": expression, "ms": duration}], device_id=self.device_id) if expression != "none" else []
        if (gesture != "none" and not servo) or (expression != "none" and not anim):
            raise ValueError("动作或表情资源不可用")
        duration = max(duration, sum(x["ms"] for x in servo), sum(x["ms"] for x in anim))
        if len(anim) > 64 or len(servo) > 64 or duration > 3000:
            raise ValueError("表现资源过长或帧数过多")
        if len(json.dumps({"anim": anim, "servo": servo}, ensure_ascii=False).encode()) > 48 * 1024:
            raise ValueError("表现资源超出单帧传输上限")
        return RobotPerformance(gesture, expression, duration, timing, speak, servo, anim)
