from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentpro.agent_runner import FakeResponse, RecordingBridgeClient
from agentpro.agent_v2 import (
    AgentReport,
    AutonomousAgent,
    V2Limits,
    build_v2_runner,
)
from agentpro.budgets import KillSwitch
from agentpro.llm_planner import FakeLLMClient


class CalculatorPhone(RecordingBridgeClient):
    """A tiny deterministic phone that really computes 5+5."""

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
            l, t, r, b = self._LAUNCHER_RECT
            if l <= tx <= r and t <= ty <= b:
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


class StaticPhone(RecordingBridgeClient):
    """A phone that never changes, so the agent gets stuck."""

    def ui_dump(self) -> FakeResponse:
        self.calls.append(("ui_dump", {}))
        return FakeResponse(
            data={
                "root": {
                    "class": "android.widget.FrameLayout",
                    "package": "com.android.launcher3",
                    "children": [
                        {
                            "class": "android.widget.TextView",
                            "text": "Static",
                            "bounds": "[100,100][400,160]",
                            "clickable": True,
                        }
                    ],
                }
            }
        )

    def tap(self, x: float, y: float) -> FakeResponse:
        return self._record("tap", {"x": x, "y": y})


def _calculator_responder(phone: CalculatorPhone):
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
                    "tool": "tap",
                    "args": {"x": 540, "y": 1170},
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
        x, y = CalculatorPhone._BUTTONS[target]
        return json.dumps(
            {
                "tool": "tap",
                "args": {"x": x, "y": y},
                "thought": f"press {target}",
                "confidence": 0.9,
                "goal_done": False,
                "subgoal_done": target == "=",
                "replan": False,
            }
        )

    return responder


def _static_responder(_messages: list) -> str:
    return json.dumps(
        {
            "tool": "tap",
            "args": {"x": 100, "y": 100},
            "thought": "tap",
            "confidence": 0.5,
            "goal_done": False,
            "subgoal_done": False,
            "replan": False,
        }
    )


class _ReplanPhone(RecordingBridgeClient):
    def ui_dump(self) -> FakeResponse:
        self.calls.append(("ui_dump", {}))
        return FakeResponse(
            data={
                "root": {
                    "class": "android.widget.FrameLayout",
                    "package": "com.android.launcher3",
                    "children": [
                        {
                            "class": "android.widget.TextView",
                            "text": "Launch",
                            "bounds": "[100,100][400,160]",
                            "clickable": True,
                        }
                    ],
                }
            }
        )


def _replan_responder(phone: _ReplanPhone):
    state = {"replanned": False}

    def responder(messages: list) -> str:
        system = messages[0].get("content", "")
        if system.startswith("You are the planner of an Android automation agent"):
            return json.dumps(
                {
                    "subgoals": [
                        {
                            "text": "launch the app",
                            "verifiable": "app shows",
                        }
                    ]
                }
            )
        if system.startswith("You are the goal-verification module"):
            return json.dumps(
                {"done": True, "evidence": "launched", "confidence": 0.9}
            )
        if not state["replanned"]:
            state["replanned"] = True
            return json.dumps(
                {
                    "tool": "tap",
                    "args": {"x": 250, "y": 130},
                    "thought": "plan is wrong, rebuild",
                    "confidence": 0.5,
                    "goal_done": False,
                    "subgoal_done": False,
                    "replan": True,
                }
            )
        return json.dumps(
            {
                "tool": None,
                "args": {},
                "thought": "goal achieved",
                "confidence": 0.9,
                "goal_done": True,
                "subgoal_done": True,
                "replan": False,
            }
        )

    return responder


class AutonomyLoopTests(unittest.TestCase):
    def _run_agent(self, phone, responder, **kwargs) -> tuple[AgentReport, str]:
        limits = kwargs.pop("limits", V2Limits())
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "trace_v2.jsonl"
            kill_switch = KillSwitch(
                flag=False, kill_file=None, env_var=None
            )
            agent = AutonomousAgent(
                phone,
                FakeLLMClient(responder=responder),
                trace_path=trace_path,
                kill_switch=kill_switch,
                limits=limits,
                **kwargs,
            )
            report = agent.run("Open the calculator and compute five plus five")
            trace_text = (
                trace_path.read_text()
                if trace_path.exists()
                else ""
            )
            return report, trace_text

    def test_full_autonomy_success(self) -> None:
        phone = CalculatorPhone()
        report, trace = self._run_agent(phone, _calculator_responder(phone))
        self.assertTrue(report.success, f"reason: {report.reason}")
        self.assertGreaterEqual(report.actions, 5)
        self.assertIn("tap", phone.commands)
        self.assertIn("v2_success", trace)
        self.assertIsNotNone(report.trace_path)

    def test_terminates_on_static_screen(self) -> None:
        phone = StaticPhone()
        report, _ = self._run_agent(phone, _static_responder)
        self.assertFalse(report.success)
        self.assertGreaterEqual(report.actions, 4)
        self.assertIn("tap", phone.commands)

    def test_kill_switch_stops(self) -> None:
        phone = CalculatorPhone()
        responder = _calculator_responder(phone)
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(
                phone,
                FakeLLMClient(responder=responder),
                trace_path=Path(tmp) / "t.jsonl",
                kill_switch=KillSwitch(flag=True),
                limits=V2Limits(),
            )
            report = agent.run("Open the calculator and compute five plus five")
        self.assertFalse(report.success)
        self.assertIn("kill switch", report.reason.lower())

    def test_model_call_budget_enforced(self) -> None:
        phone = StaticPhone()
        report, _ = self._run_agent(
            phone,
            _static_responder,
            limits=V2Limits(max_model_calls=1),
        )
        self.assertFalse(report.success)
        self.assertIn("model-call budget", report.reason)

    def test_replan_path(self) -> None:
        phone = _ReplanPhone()
        report, trace = self._run_agent(phone, _replan_responder(phone))
        self.assertTrue(report.success, f"reason: {report.reason}")
        self.assertGreaterEqual(report.replans, 1)
        self.assertIn("v2_replan_subgoals", trace)


class AgentReportTests(unittest.TestCase):
    def test_to_dict(self) -> None:
        report = AgentReport(success=True, reason="done")
        data = report.to_dict()
        self.assertTrue(data["success"])
        for key in (
            "steps", "actions", "model_calls", "recoveries", "replans",
            "failures", "subgoals", "trace_path", "elapsed_s",
            "goal_evidence", "active_subgoal",
        ):
            self.assertIn(key, data)

    def test_builder(self) -> None:
        agent = build_v2_runner(
            RecordingBridgeClient(),
            FakeLLMClient(script=["{}"]),
        )
        self.assertIsInstance(agent, AutonomousAgent)


class SmartRecoveryTests(unittest.TestCase):
    def test_back_then_home_escalation(self) -> None:
        from agentpro.agent_v2 import SmartRecovery
        from agentpro.models import AgentContext

        client = RecordingBridgeClient()
        recovery = SmartRecovery(client)
        context = AgentContext(goal="g", recovery_count=3)
        from agentpro.memory import MemoryStore

        memory = MemoryStore()
        ok = recovery.recover(context, memory, None)
        self.assertTrue(ok)
        self.assertIn("key_event", client.commands)
        self.assertIn("back", client.commands)


class MeasuredGestureStallTests(unittest.TestCase):
    """The counter that ends a gesture proven to have done nothing."""

    def setUp(self) -> None:
        from agentpro.agent_v2 import MeasuredGestureStall

        self.stall = MeasuredGestureStall(limit=3)

    def test_stops_on_the_third_consecutive_dead_gesture(self) -> None:
        self.assertIsNone(self.stall.record("swipe(a)", measured_no_effect=True))
        self.assertIsNone(self.stall.record("swipe(a)", measured_no_effect=True))
        self.assertEqual(self.stall.record("swipe(a)", measured_no_effect=True), 3)

    def test_a_different_gesture_gets_a_fresh_budget(self) -> None:
        self.stall.record("swipe(a)", measured_no_effect=True)
        self.stall.record("swipe(a)", measured_no_effect=True)
        self.assertIsNone(self.stall.record("swipe(b)", measured_no_effect=True))
        self.assertEqual(self.stall.count, 1)

    def test_unmeasured_never_counts(self) -> None:
        for _ in range(10):
            self.assertIsNone(
                self.stall.record("swipe(a)", measured_no_effect=False)
            )
        self.assertEqual(self.stall.count, 0)

    def test_a_working_gesture_clears_the_streak(self) -> None:
        self.stall.record("swipe(a)", measured_no_effect=True)
        self.stall.record("swipe(a)", measured_no_effect=True)
        self.stall.record("swipe(a)", measured_no_effect=False)
        self.assertIsNone(self.stall.record("swipe(a)", measured_no_effect=True))
        self.assertEqual(self.stall.count, 1)

    def test_limit_must_be_positive(self) -> None:
        from agentpro.agent_v2 import MeasuredGestureStall

        with self.assertRaises(ValueError):
            MeasuredGestureStall(limit=0)


if __name__ == "__main__":
    unittest.main()