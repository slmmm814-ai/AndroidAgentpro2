"""Offline self-test for the v2 autonomy loop.

Runs the real :class:`agentpro.agent_v2.AutonomousAgent` against a simulated
device and a scripted LLM responder. No network, no device, no API key — the
CLI uses this to prove the loop mechanics (observe -> plan -> act -> verify ->
goal-verify) before a live run.
"""

from __future__ import annotations

import json
from pathlib import Path

from .agent_runner import FakeResponse, RecordingBridgeClient
from .agent_v2 import AgentReport, AutonomousAgent, V2Limits
from .budgets import KillSwitch
from .llm_planner import FakeLLMClient

GOAL = "Open the calculator and compute five plus five"


class CalculatorPhone(RecordingBridgeClient):
    """A tiny deterministic phone that really computes 5 + 5."""

    _LAUNCHER_RECT = (440, 1100, 640, 1240)
    _BUTTONS = {
        "5": (220, 575),
        "+": (490, 575),
        "=": (760, 575),
    }

    def __init__(self) -> None:
        super().__init__()
        self._state = "launcher"
        self.expr = ""
        self.display = "0"

    def _tree(self) -> dict:
        if self._state == "launcher":
            return {
                "class": "android.widget.FrameLayout",
                "package": "com.android.launcher3",
                "children": [
                    {
                        "class": "android.widget.TextView",
                        "text": "Calculator",
                        "bounds": "[440,1100][640,1240]",
                        "clickable": True,
                    }
                ],
            }
        return {
            "class": "android.widget.FrameLayout",
            "package": "com.example.calc",
            "children": [
                {
                    "class": "android.widget.EditText",
                    "text": self.display,
                    "bounds": "[100,300][980,400]",
                    "editable": True,
                    "focused": True,
                },
                {
                    "class": "android.widget.TextView",
                    "text": "5",
                    "bounds": "[100,500][340,650]",
                    "clickable": True,
                },
                {
                    "class": "android.widget.TextView",
                    "text": "+",
                    "bounds": "[370,500][610,650]",
                    "clickable": True,
                },
                {
                    "class": "android.widget.TextView",
                    "text": "=",
                    "bounds": "[640,500][880,650]",
                    "clickable": True,
                },
            ],
        }

    def ui_dump(self) -> FakeResponse:
        self.calls.append(("ui_dump", {}))
        return FakeResponse(data={"root": self._tree()})

    def get_window(self) -> FakeResponse:
        self.calls.append(("get_window", {}))
        if self._state == "launcher":
            return FakeResponse(
                data={
                    "package_name": "com.android.launcher3",
                    "activity_name": "launcher",
                    "window_title": "Launcher",
                }
            )
        return FakeResponse(
            data={
                "package_name": "com.example.calc",
                "activity_name": "CalcActivity",
                "window_title": "Calculator",
            }
        )

    def tap(self, x: float, y: float) -> FakeResponse:
        self.calls.append(("tap", {"x": x, "y": y}))
        tx, ty = int(x), int(y)
        if self._state == "launcher":
            left, top, right, bottom = self._LAUNCHER_RECT
            if left <= tx <= right and top <= ty <= bottom:
                self._state = "calc"
            return self._record("tap_effect", {"target": "launcher"})

        pressed: str | None = None
        for label, (bx, by) in self._BUTTONS.items():
            if abs(bx - tx) <= 120 and abs(by - ty) <= 75:
                pressed = label
                break
        if pressed is None:
            return FakeResponse(data={"operation_id": 0})

        if pressed == "=":
            try:
                left, right = self.expr.split("+", 1)
                self.display = str(int(left) + int(right))
            except Exception:
                self.display = self.expr or "0"
            self.expr = ""
        else:
            self.expr += pressed
            self.display = self.expr
        return self._record("tap_effect", {"target": pressed})


def _scripted_responder(phone: CalculatorPhone):
    def responder(messages: list) -> str:
        system = messages[0].get("content", "")
        if system.startswith("You are the planner of an Android automation agent"):
            return json.dumps(
                {
                    "subgoals": [
                        {
                            "text": "open the calculator",
                            "verifiable": "calculator is visible",
                        },
                        {
                            "text": "compute five plus five",
                            "verifiable": "result shows 10",
                        },
                    ]
                }
            )
        if system.startswith("You are the goal-verification module"):
            return json.dumps(
                {
                    "done": True,
                    "evidence": "result shows 10",
                    "confidence": 0.95,
                }
            )
        if phone._state == "launcher":
            return json.dumps(
                {
                    "tool": "tap_element",
                    "args": {"text": "Calculator"},
                    "thought": "open calculator",
                    "confidence": 0.9,
                    "goal_done": False,
                    "subgoal_done": False,
                    "replan": False,
                }
            )
        if phone.display == "10" and phone.expr == "":
            return json.dumps(
                {
                    "tool": None,
                    "args": {},
                    "thought": "result is on screen",
                    "confidence": 0.95,
                    "goal_done": True,
                    "subgoal_done": True,
                    "replan": False,
                }
            )
        if phone.expr == "":
            target = "5"
        elif phone.expr == "5":
            target = "+"
        elif phone.expr == "5+":
            target = "5"
        else:
            target = "="
        return json.dumps(
            {
                "tool": "tap_element",
                "args": {"text": target},
                "thought": f"press {target}",
                "confidence": 0.9,
                "goal_done": False,
                "subgoal_done": target == "=",
                "replan": False,
            }
        )

    return responder


def run_self_test(
    *,
    trace_path: str | Path,
    limits: V2Limits | None = None,
) -> AgentReport:
    """Run the v2 loop offline against the simulated calculator phone."""
    phone = CalculatorPhone()
    agent = AutonomousAgent(
        phone,
        FakeLLMClient(responder=_scripted_responder(phone)),
        trace_path=trace_path,
        kill_switch=KillSwitch(flag=False, kill_file=None, env_var=None),
        limits=limits or V2Limits(),
    )
    return agent.run(GOAL)