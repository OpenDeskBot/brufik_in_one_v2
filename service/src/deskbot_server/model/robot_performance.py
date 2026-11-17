"""Validated robot performance, independent of model and transport protocols."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

ROBOT_TOOL_NAME = "robot_perform"


@dataclass(frozen=True)
class RobotPerformance:
    gesture: str
    expression: str
    duration_ms: int
    timing: str
    speak: bool
    servo: list[dict[str, Any]]
    anim: list[dict[str, Any]]

    def tracks(self) -> dict[str, Any]:
        return {key: copy.deepcopy(value) for key, value in
                (("servo", self.servo), ("anim", self.anim)) if value}

    def result(self, status: str) -> dict[str, Any]:
        return dict(ok=status in {"scheduled", "completed"}, status=status, gesture=self.gesture,
                    expression=self.expression, duration_ms=self.duration_ms, timing=self.timing, speak=self.speak)
