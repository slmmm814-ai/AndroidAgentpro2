from __future__ import annotations

import unittest

from agentpro.agent_runner import RecordingBridgeClient
from agentpro.bridge_tools import build_default_registry
from agentpro.llm_planner import FakeLLMClient
from agentpro.memory import MemoryStore
from agentpro.model_manager import ModelManager
from agentpro.planner_v2 import SubgoalPlanner
from agentpro.models import AgentContext
from agentpro.screen import ScreenReader

_DECOMPOSE = (
    '{"subgoals": [{"text": "open the calculator", '
    '"verifiable": "calculator is visible"}, '
    '{"text": "compute 5+5", "verifiable": "result shows 10"}]}'
)


def _build(snapshot=None):
    registry = build_default_registry()
    memory = MemoryStore()
    return registry, memory


def _snapshot() -> object:
    tree = {
        "class": "android.widget.FrameLayout",
        "package": "com.example.calc",
        "children": [
            {"class": "android.widget.EditText", "text": "0", "bounds": "[100,200][900,300]",
             "editable": True, "focused": True},
            {"class": "android.widget.TextView", "text": "5", "bounds": "[100,400][300,500]",
             "clickable": True},
        ],
    }
    return ScreenReader(
        RecordingBridgeClient(ui_root=tree), include_screenshot=False
    ).observe()


class SubgoalPlannerTests(unittest.TestCase):
    def test_decompose(self) -> None:
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        planner.ensure_subgoals("build a calculator")
        self.assertEqual(len(memory.subgoal_list), 2)
        self.assertEqual(memory.subgoal_index, 0)
        self.assertEqual(memory.active_subgoal, "open the calculator")

    def test_decision(self) -> None:
        decision_json = (
            '{"tool": "tap", "args": {"x": 200, "y": 450}, '
            '"thought": "press five", "confidence": 0.8, '
            '"goal_done": false, "subgoal_done": false, "replan": false}'
        )
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE, decision_json]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        context = AgentContext(goal="build a calculator")
        decision = planner.decide(context, _snapshot())
        self.assertEqual(decision.tool, "tap")
        self.assertEqual(decision.args, {"x": 200, "y": 450})
        self.assertTrue(decision.is_action)
        self.assertFalse(decision.is_finish)

    def test_advance_subgoal(self) -> None:
        decision_json = (
            '{"tool": "tap", "args": {"x": 200, "y": 450}, '
            '"thought": "press five", "confidence": 0.8, '
            '"goal_done": false, "subgoal_done": true, "replan": false}'
        )
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE, decision_json]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        context = AgentContext(goal="build a calculator")
        decision = planner.decide(context, _snapshot())
        self.assertTrue(decision.subgoal_done)
        self.assertEqual(memory.active_subgoal, "open the calculator")
        planner.advance_subgoal()
        self.assertEqual(memory.subgoal_index, 1)
        self.assertEqual(memory.active_subgoal, "compute 5+5")

    def test_unknown_tool_falls_back_to_wait(self) -> None:
        bad = (
            '{"tool": "nope", "args": {}, "thought": "x", "confidence": 0.5, '
            '"goal_done": false, "subgoal_done": false, "replan": false}'
        )
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE, bad, bad]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        decision = planner.decide(AgentContext(goal="g"), _snapshot())
        self.assertEqual(decision.tool, "wait")
        self.assertLess(decision.confidence, 0.2)

    def test_invalid_args_falls_back_to_wait(self) -> None:
        bad = (
            '{"tool": "open_app", "args": {"package": 123}, "thought": "x", '
            '"confidence": 0.5, "goal_done": false, "subgoal_done": false, '
            '"replan": false}'
        )
        # first attempt rejects type; second is also bad -> ModelError -> wait
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE, bad, bad]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        decision = planner.decide(AgentContext(goal="g"), _snapshot())
        self.assertEqual(decision.tool, "wait")

    def test_goal_done_without_tool(self) -> None:
        done = (
            '{"tool": null, "args": {}, "thought": "verified", "confidence": 0.9, '
            '"goal_done": true, "subgoal_done": false, "replan": false}'
        )
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE, done]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        decision = planner.decide(AgentContext(goal="g"), _snapshot())
        self.assertTrue(decision.is_finish)
        self.assertIsNone(decision.tool)

    def test_no_tool_no_goal_done_falls_back(self) -> None:
        empty = '{"args": {}, "confidence": 0.5}'
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE, empty]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        decision = planner.decide(AgentContext(goal="g"), _snapshot())
        self.assertEqual(decision.tool, "wait")

    def test_replan(self) -> None:
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE, _DECOMPOSE]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        planner.ensure_subgoals("build a calculator")
        planner.replan("build a calculator")
        self.assertEqual(len(memory.subgoal_list), 2)

    def test_executor_compat_plan(self) -> None:
        decision_json = (
            '{"tool": "tap", "args": {"x": 200, "y": 450}, "thought": "x", '
            '"confidence": 0.5, "goal_done": false, "subgoal_done": false, '
            '"replan": false}'
        )
        mm = ModelManager(FakeLLMClient(script=[_DECOMPOSE, decision_json]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        actions = planner.plan(AgentContext(goal="g"))
        self.assertEqual(len(actions), 1)

    def test_decompose_failure_safe_fallback(self) -> None:
        bad = "not json at all"
        mm = ModelManager(FakeLLMClient(script=[bad, bad]), retries=0)
        registry, memory = _build()
        planner = SubgoalPlanner(mm, registry, memory)
        planner.ensure_subgoals("build a calculator")
        # single-subgoal fallback = whole goal
        self.assertEqual(memory.subgoal_count, 1)
        self.assertEqual(memory.active_subgoal, "build a calculator")


if __name__ == "__main__":
    unittest.main()