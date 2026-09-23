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
    AgentFSM,
    AgentState,
    GoalResult,
    Observation,
    TraceRecorder,
)
from agentpro.hierarchical_planner import (
    HierarchicalPlanner,
    HierarchicalPlanningError,
    HierarchyValidationError,
    StaticHierarchicalTaskProvider,
    make_step,
    make_subtask,
    make_task,
)


class TestHierarchicalPlanner(unittest.TestCase):
    def make_three_level_task(self):
        return make_task(
            "open and verify application",
            [
                make_subtask(
                    "open",
                    [
                        make_step(
                            "open application",
                            ActionType.OPEN_APP,
                            {"package": "com.example.app"},
                        ),
                    ],
                ),
                make_subtask(
                    "inspect",
                    [
                        make_step("capture screen", ActionType.SCREENSHOT),
                        make_step("dump ui", ActionType.UI_DUMP),
                    ],
                ),
                make_subtask(
                    "finish",
                    [
                        make_step("finish task", ActionType.FINISH),
                    ],
                ),
            ],
        )

    def test_three_level_task_has_expected_structure(self) -> None:
        task = self.make_three_level_task()

        self.assertEqual(task.depth, 3)
        self.assertEqual(len(task.subtasks), 3)
        self.assertEqual(task.action_count, 4)
        self.assertEqual(
            [action.action_type for action in task.flatten()],
            [
                ActionType.OPEN_APP,
                ActionType.SCREENSHOT,
                ActionType.UI_DUMP,
                ActionType.FINISH,
            ],
        )

    def test_planner_flattens_three_level_task_deterministically(self) -> None:
        context = AgentContext(goal="open and verify application")
        planner = HierarchicalPlanner(
            StaticHierarchicalTaskProvider(self.make_three_level_task())
        )

        actions = planner.plan(context)

        self.assertEqual(len(actions), 4)
        self.assertEqual(actions[0].action_type, ActionType.OPEN_APP)
        self.assertEqual(actions[-1].action_type, ActionType.FINISH)
        self.assertEqual(planner.describe()["depth"], 3)

    def test_action_budget_is_enforced(self) -> None:
        context = AgentContext(goal="limited")
        planner = HierarchicalPlanner(
            StaticHierarchicalTaskProvider(self.make_three_level_task()),
            max_actions=3,
        )

        with self.assertRaises(HierarchicalPlanningError):
            planner.plan(context)

    def test_empty_subtask_is_rejected(self) -> None:
        with self.assertRaises(HierarchyValidationError):
            make_subtask("invalid", [])

    def test_empty_task_is_rejected(self) -> None:
        with self.assertRaises(HierarchyValidationError):
            make_task("invalid", [])

    def test_invalid_provider_result_is_rejected(self) -> None:
        class InvalidProvider:
            def build(self, context: AgentContext):
                context.validate()
                return "invalid"

        planner = HierarchicalPlanner(InvalidProvider())

        with self.assertRaises(HierarchicalPlanningError):
            planner.plan(AgentContext(goal="test"))

    def test_end_to_end_three_level_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = TraceRecorder(Path(directory) / "trace.jsonl")
            context = AgentContext(goal="execute three level task")
            fsm = AgentFSM(context)

            planner = HierarchicalPlanner(
                StaticHierarchicalTaskProvider(
                    make_task(
                        "three level execution",
                        [
                            make_subtask(
                                "first",
                                [make_step("open", ActionType.OPEN_APP)],
                            ),
                            make_subtask(
                                "second",
                                [make_step("observe", ActionType.UI_DUMP)],
                            ),
                            make_subtask(
                                "third",
                                [make_step("finish", ActionType.FINISH)],
                            ),
                        ],
                    )
                )
            )

            executed: list[AgentAction] = []

            def observer() -> Observation:
                return Observation(
                    success=True,
                    package_name="com.example.app",
                    activity_name="MainActivity",
                    fingerprint="stable",
                )

            def execute(action: AgentAction) -> ActionResult:
                executed.append(action)
                return ActionResult(
                    success=True,
                    operation_id=len(executed),
                    data={"action": action.action_type.value},
                )

            def verify(
                current: AgentContext,
                observation: Observation | None,
            ) -> GoalResult:
                if current.last_action is not None and (
                    current.last_action.action_type == ActionType.FINISH
                ):
                    return GoalResult(True, "three level task completed")
                return GoalResult(False, "task not complete")

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
            self.assertEqual(context.state, AgentState.SUCCESS)
            self.assertEqual(len(executed), 3)
            self.assertEqual(
                [action.action_type for action in executed],
                [
                    ActionType.OPEN_APP,
                    ActionType.UI_DUMP,
                    ActionType.FINISH,
                ],
            )

            events = trace.read_all()
            event_names = [event["event"] for event in events]
            self.assertIn("plan_created", event_names)
            self.assertIn("action_executed", event_names)
            self.assertIn("goal_verified", event_names)


    def test_failed_action_triggers_replan_and_uses_new_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = TraceRecorder(Path(directory) / "trace.jsonl")
            context = AgentContext(goal="recover by replanning")
            fsm = AgentFSM(context)

            first_plan = [
                AgentAction(
                    action_type=ActionType.OPEN_APP,
                    arguments={"package": "com.example.bad"},
                ),
            ]
            second_plan = [
                AgentAction(
                    action_type=ActionType.OPEN_APP,
                    arguments={"package": "com.example.good"},
                ),
                AgentAction(action_type=ActionType.FINISH),
            ]

            planner_calls = 0
            executed: list[AgentAction] = []

            def planner(_context: AgentContext):
                nonlocal planner_calls
                planner_calls += 1
                return first_plan if planner_calls == 1 else second_plan

            def observer() -> Observation:
                return Observation(
                    success=True,
                    package_name="com.example.app",
                    activity_name="MainActivity",
                    fingerprint="stable",
                )

            def execute(action: AgentAction) -> ActionResult:
                executed.append(action)
                if action.arguments.get("package") == "com.example.bad":
                    return ActionResult(
                        success=False,
                        operation_id=len(executed),
                        error_code="ACTION_FAILED",
                        error_message="target application failed to open",
                    )
                return ActionResult(
                    success=True,
                    operation_id=len(executed),
                    data={"action": action.action_type.value},
                )

            def verify(
                current: AgentContext,
                _observation: Observation | None,
            ) -> GoalResult:
                if (
                    current.last_action is not None
                    and current.last_action.action_type == ActionType.FINISH
                ):
                    return GoalResult(True, "completed")
                return GoalResult(False, "continue")

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
            self.assertEqual(context.state, AgentState.SUCCESS)
            self.assertEqual(planner_calls, 2)
            self.assertEqual(
                [action.arguments.get("package") for action in executed],
                ["com.example.bad", "com.example.good", None],
            )
            self.assertEqual(
                executed[-1].action_type,
                ActionType.FINISH,
            )
            self.assertEqual(context.replan_count, 1)

            events = trace.read_all()
            event_names = [event["event"] for event in events]
            self.assertIn("plan_created", event_names)

            replan_events = [
                event for event in events if event["state"] == AgentState.REPLAN.value
            ]
            self.assertGreaterEqual(len(replan_events), 1)

if __name__ == "__main__":
    unittest.main()
