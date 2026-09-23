"""Breaker tests: the executor must not die with an FSM "Invalid transition" error.

Suspected from READING executor.py + fsm.py. The reviewer did NOT run these against
the real code (only pasted excerpts were available), so a failure here is evidence,
and a pass means the suspicion was wrong.

Put in tests/breaker/ and run from the project root:
    python -m unittest tests.breaker.test_executor_paths -v
Only APIs already used by tests/test_phase5_hierarchical.py are used here.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentpro import (
    ActionResult,
    ActionType,
    AgentContext,
    AgentExecutor,
    AgentFSM,
    GoalResult,
    Observation,
    TraceRecorder,
)
from agentpro.hierarchical_planner import (
    HierarchicalPlanner,
    StaticHierarchicalTaskProvider,
    make_step,
    make_subtask,
    make_task,
)


def _observer() -> Observation:
    return Observation(
        success=True,
        package_name="com.example.app",
        activity_name="MainActivity",
        fingerprint="stable",
    )


def _execute(_action) -> ActionResult:
    return ActionResult(success=True, operation_id=1)


class TestExecutorFlowDoesNotCrash(unittest.TestCase):
    def _run(self, planner, verify):
        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(goal="flow test")
            executor = AgentExecutor(
                context=context,
                fsm=AgentFSM(context),
                trace=TraceRecorder(Path(directory) / "trace.jsonl"),
                observer=_observer,
                planner=planner,
                action_executor=_execute,
                goal_verifier=verify,
                recovery_handler=lambda _context: False,
            )
            return executor.run(), context

    def test_empty_plan_when_goal_already_met_is_success(self) -> None:
        # Suspected: PLAN -> VERIFY_GOAL is not in the FSM table.
        result, _ = self._run(
            planner=lambda _context: [],
            verify=lambda _context, _obs: GoalResult(True, "already done"),
        )
        self.assertTrue(result.success, result.reason)

    def test_empty_plan_when_goal_not_met_fails_cleanly(self) -> None:
        # Suspected: after REPLAN -> PLAN the loop restarts while still in PLAN,
        # then PLAN -> PLAN is not in the FSM table.
        result, _ = self._run(
            planner=lambda _context: [],
            verify=lambda _context, _obs: GoalResult(False, "not done"),
        )
        self.assertFalse(result.success)
        self.assertNotIn("Invalid transition", result.reason)

    def test_plan_exhausted_without_goal_fails_cleanly(self) -> None:
        # Suspected: the last action's verification fails -> state PLAN -> the for
        # loop ends -> next loop iteration transitions PLAN -> PLAN.
        task = make_task(
            "no finish step",
            [make_subtask("only", [make_step("dump", ActionType.UI_DUMP)])],
        )
        hierarchical = HierarchicalPlanner(StaticHierarchicalTaskProvider(task))
        result, _ = self._run(
            planner=hierarchical.plan,
            verify=lambda _context, _obs: GoalResult(False, "not done"),
        )
        self.assertFalse(result.success)
        self.assertNotIn("Invalid transition", result.reason)


if __name__ == "__main__":
    unittest.main()
