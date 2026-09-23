from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentpro.agent_v2 import V2Limits
from agentpro.self_test import GOAL, CalculatorPhone, run_self_test


class SelfTestTests(unittest.TestCase):
    def test_self_test_reaches_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_self_test(
                trace_path=Path(tmp) / "trace.jsonl",
                limits=V2Limits(max_actions=60),
            )
        self.assertTrue(report.success, f"reason: {report.reason}")
        self.assertGreaterEqual(report.actions, 5)
        self.assertTrue(report.goal_evidence)

    def test_self_test_uses_element_taps(self) -> None:
        phone = CalculatorPhone()
        from agentpro.budgets import KillSwitch
        from agentpro.llm_planner import FakeLLMClient

        from agentpro.agent_v2 import AutonomousAgent
        from agentpro.self_test import _scripted_responder

        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "t.jsonl"
            agent = AutonomousAgent(
                phone,
                FakeLLMClient(responder=_scripted_responder(phone)),
                trace_path=trace_path,
                kill_switch=KillSwitch(flag=False, kill_file=None, env_var=None),
                limits=V2Limits(max_actions=60),
            )
            report = agent.run(GOAL)
            trace_text = (
                trace_path.read_text()
                if trace_path.exists()
                else ""
            )
        self.assertTrue(report.success)
        self.assertIn("tap_element", trace_text)

    def test_calculator_phone_computes(self) -> None:
        phone = CalculatorPhone()
        phone._state = "calc"
        from agentpro.self_test import CalculatorPhone as _CP

        x5, y5 = _CP._BUTTONS["5"]
        xp, yp = _CP._BUTTONS["+"]
        xe, ye = _CP._BUTTONS["="]
        phone.tap(x5, y5)
        phone.tap(xp, yp)
        phone.tap(x5, y5)
        phone.tap(xe, ye)
        self.assertEqual(phone.display, "10")


if __name__ == "__main__":
    unittest.main()