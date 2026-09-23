from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentpro import (
    ActionResult,
    ActionType,
    AgentAction,
    AgentContext,
    AgentExecutor,
    AgentState,
    AgentFSM,
    FSMLimits,
    GoalResult,
    Observation,
    TraceRecorder,
)
from agentpro.hierarchical_planner import (
    HierarchicalPlanner,
    HierarchicalPlanningError,
    StaticHierarchicalTaskProvider,
    make_step,
    make_subtask,
    make_task,
)


class TestPhase5HierarchicalBreaker(unittest.TestCase):
    def _make_task(self):
        return make_task(
            "three-step task",
            [
                make_subtask(
                    "first",
                    [make_step("open", ActionType.OPEN_APP, {"package": "com.example.app"})],
                ),
                make_subtask(
                    "second",
                    [make_step("inspect", ActionType.UI_DUMP)],
                ),
                make_subtask(
                    "third",
                    [make_step("finish", ActionType.FINISH)],
                ),
            ],
        )

    def test_plan_continuation_does_not_repeat_previous_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(goal="execute without repetition")
            fsm = AgentFSM(context)
            trace = TraceRecorder(Path(directory) / "trace.jsonl")
            task = self._make_task()

            planner_calls = 0
            executed: list[ActionType] = []

            def planner(_context: AgentContext):
                nonlocal planner_calls
                planner_calls += 1
                return HierarchicalPlanner(
                    StaticHierarchicalTaskProvider(task)
                ).plan(_context)

            def observer() -> Observation:
                return Observation(
                    success=True,
                    package_name="com.example.app",
                    activity_name="MainActivity",
                    fingerprint="stable",
                )

            def execute(action):
                executed.append(action.action_type)
                return ActionResult(
                    success=True,
                    operation_id=len(executed),
                    data={"action": action.action_type.value},
                )

            def verify(current: AgentContext, _observation):
                if current.last_action is not None and current.last_action.action_type == ActionType.FINISH:
                    return GoalResult(True, "completed")
                return GoalResult(False, "not complete")

            executor = AgentExecutor(
                context=context,
                fsm=fsm,
                trace=trace,
                observer=observer,
                planner=planner,
                action_executor=execute,
                goal_verifier=verify,
                recovery_handler=lambda _context: False,
            )

            result = executor.run()

            self.assertTrue(result.success)
            self.assertEqual(
                executed,
                [ActionType.OPEN_APP, ActionType.UI_DUMP, ActionType.FINISH],
            )
            self.assertEqual(planner_calls, 1)

    def test_failed_intermediate_goal_check_cannot_replan_without_explicit_replan_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(goal="continue existing plan")
            fsm = AgentFSM(context)
            trace = TraceRecorder(Path(directory) / "trace.jsonl")
            task = self._make_task()

            planner_calls = 0
            executed: list[ActionType] = []

            hierarchical_planner = HierarchicalPlanner(
                StaticHierarchicalTaskProvider(task)
            )

            def planner(current: AgentContext):
                nonlocal planner_calls
                planner_calls += 1
                return hierarchical_planner.plan(current)

            def observer() -> Observation:
                return Observation(
                    success=True,
                    package_name="com.example.app",
                    activity_name="MainActivity",
                    fingerprint="stable",
                )

            def execute(action):
                executed.append(action.action_type)
                return ActionResult(
                    success=True,
                    operation_id=len(executed),
                )

            def verify(current: AgentContext, _observation):
                return GoalResult(
                    current.last_action is not None
                    and current.last_action.action_type == ActionType.FINISH,
                    "completed" if current.last_action is not None
                    and current.last_action.action_type == ActionType.FINISH
                    else "continue",
                )

            executor = AgentExecutor(
                context=context,
                fsm=fsm,
                trace=trace,
                observer=observer,
                planner=planner,
                action_executor=execute,
                goal_verifier=verify,
                recovery_handler=lambda _context: False,
            )

            result = executor.run()

            self.assertTrue(result.success)
            self.assertEqual(planner_calls, 1)
            self.assertEqual(len(executed), 3)
            self.assertEqual(executed.count(ActionType.OPEN_APP), 1)
            self.assertEqual(executed.count(ActionType.UI_DUMP), 1)
            self.assertEqual(executed.count(ActionType.FINISH), 1)

            events = trace.read_all()
            continuations = [
                event
                for event in events
                if event["event"] == "plan_continuation"
            ]
            self.assertEqual(len(continuations), 2)

    def test_hierarchical_action_budget_cannot_be_bypassed(self) -> None:
        context = AgentContext(goal="budget test")
        planner = HierarchicalPlanner(
            StaticHierarchicalTaskProvider(self._make_task()),
            max_actions=2,
        )

        with self.assertRaises(HierarchicalPlanningError):
            planner.plan(context)

    def test_replan_limit_cannot_be_bypassed_after_repeated_action_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = TraceRecorder(Path(directory) / "trace.jsonl")
            context = AgentContext(goal="bounded replanning")
            fsm = AgentFSM(context, FSMLimits(max_steps=100, max_actions=50, max_recoveries=0, max_replans=1))

            planner_calls = 0
            executed: list[AgentAction] = []

            def observer() -> Observation:
                return Observation(
                    success=True,
                    package_name="com.example.app",
                    activity_name="MainActivity",
                    fingerprint="stable",
                )

            def planner(_context: AgentContext):
                nonlocal planner_calls
                planner_calls += 1
                return [
                    AgentAction(
                        action_type=ActionType.OPEN_APP,
                        arguments={"package": f"com.example.failed{planner_calls}"},
                    )
                ]

            def execute(action: AgentAction) -> ActionResult:
                executed.append(action)
                return ActionResult(
                    success=False,
                    operation_id=len(executed),
                    error_code="ACTION_FAILED",
                    error_message="action failed",
                )

            def verify(
                _context: AgentContext,
                _observation: Observation | None,
            ) -> GoalResult:
                return GoalResult(False, "not complete")

            executor = AgentExecutor(
                context=context,
                fsm=fsm,
                trace=trace,
                observer=observer,
                planner=planner,
                action_executor=execute,
                goal_verifier=verify,
                recovery_handler=lambda _context: False,
            )

            result = executor.run()

            self.assertFalse(result.success)
            self.assertEqual(context.state, AgentState.STUCK)
            self.assertEqual(context.replan_count, 1)
            self.assertEqual(planner_calls, 2)
            self.assertEqual(len(executed), 2)

            events = trace.read_all()
            replan_events = [
                event
                for event in events
                if event["state"] == AgentState.REPLAN.value
            ]
            self.assertEqual(len(replan_events), 2)
            self.assertTrue(
                any(event["event"] == "action_failed_replan_requested" for event in events)
            )
            self.assertTrue(
                any(event["event"] == "replan_requested" for event in events)
            )

if __name__ == "__main__":
    unittest.main()
