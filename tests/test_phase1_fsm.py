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
    FSMLimits,
    GoalResult,
    InvalidTransitionError,
    Observation,
    StateLimitExceededError,
    TraceRecorder,
)


class TestAgentFSM(unittest.TestCase):
    def test_initial_state(self) -> None:
        context = AgentContext(goal="test goal")
        fsm = AgentFSM(context)

        self.assertEqual(fsm.state, AgentState.IDLE)
        self.assertEqual(context.step, 0)

    def test_normal_transition_sequence(self) -> None:
        context = AgentContext(goal="test goal")
        fsm = AgentFSM(context)

        sequence = [
            AgentState.DECOMPOSE,
            AgentState.OBSERVE,
            AgentState.PLAN,
            AgentState.EXECUTE,
            AgentState.VERIFY_ACTION,
            AgentState.VERIFY_GOAL,
            AgentState.SUCCESS,
        ]

        for state in sequence:
            fsm.transition(state)

        self.assertEqual(fsm.state, AgentState.SUCCESS)
        self.assertEqual(context.step, len(sequence))

    def test_invalid_transition_is_rejected(self) -> None:
        context = AgentContext(goal="test goal")
        fsm = AgentFSM(context)

        with self.assertRaises(InvalidTransitionError):
            fsm.transition(AgentState.EXECUTE)

        self.assertEqual(fsm.state, AgentState.IDLE)

    def test_terminal_state_cannot_transition(self) -> None:
        context = AgentContext(goal="test goal")
        fsm = AgentFSM(context)

        fsm.transition(AgentState.DECOMPOSE)
        fsm.transition(AgentState.OBSERVE)
        fsm.transition(AgentState.PLAN)
        fsm.transition(AgentState.EXECUTE)
        fsm.transition(AgentState.VERIFY_ACTION)
        fsm.transition(AgentState.VERIFY_GOAL)
        fsm.transition(AgentState.SUCCESS)

        with self.assertRaises(InvalidTransitionError):
            fsm.transition(AgentState.OBSERVE)

    def test_action_limit(self) -> None:
        context = AgentContext(goal="test goal")
        fsm = AgentFSM(
            context,
            FSMLimits(max_actions=1),
        )

        fsm.register_action()

        with self.assertRaises(StateLimitExceededError):
            fsm.register_action()

        self.assertEqual(fsm.state, AgentState.STUCK)

    def test_recovery_limit(self) -> None:
        context = AgentContext(goal="test goal")
        fsm = AgentFSM(
            context,
            FSMLimits(max_recoveries=1),
        )

        fsm.transition(AgentState.DECOMPOSE)
        fsm.transition(AgentState.OBSERVE)
        fsm.transition(AgentState.RECOVER)

        with self.assertRaises(StateLimitExceededError):
            fsm.transition(AgentState.OBSERVE)

        self.assertEqual(fsm.state, AgentState.STUCK)

    def test_empty_goal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AgentContext(goal="   ")


class TestTraceRecorder(unittest.TestCase):
    def test_trace_write_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            recorder = TraceRecorder(path)

            recorder.record(
                event="test",
                state="IDLE",
                step=0,
                payload={"value": 1},
            )

            events = recorder.read_all()

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "test")
            self.assertEqual(events[0]["payload"]["value"], 1)


class TestAgentExecutor(unittest.TestCase):
    def test_successful_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(goal="complete task")
            trace = TraceRecorder(
                Path(directory) / "trace.jsonl"
            )
            fsm = AgentFSM(context)

            observations = [
                Observation(
                    success=True,
                    package_name="com.example",
                    fingerprint="initial",
                )
            ]

            executed: list[ActionType] = []

            def observe() -> Observation:
                return observations[0]

            def plan(
                _context: AgentContext,
            ) -> list[AgentAction]:
                if _context.action_count == 0:
                    return [
                        AgentAction(
                            ActionType.TAP,
                            {"x": 10, "y": 20},
                        )
                    ]
                return []

            def execute(
                action: AgentAction,
            ) -> ActionResult:
                executed.append(action.action_type)
                return ActionResult(
                    success=True,
                    operation_id=1,
                )

            def verify_goal(
                _context: AgentContext,
                _observation: Observation | None,
            ) -> GoalResult:
                return GoalResult(
                    success=True,
                    reason="test goal reached",
                )

            def recover(
                _context: AgentContext,
            ) -> bool:
                return True

            executor = AgentExecutor(
                context=context,
                fsm=fsm,
                trace=trace,
                observer=observe,
                planner=plan,
                action_executor=execute,
                goal_verifier=verify_goal,
                recovery_handler=recover,
            )

            result = executor.run()

            self.assertTrue(result.success)
            self.assertEqual(fsm.state, AgentState.SUCCESS)
            self.assertEqual(
                executed,
                [ActionType.TAP],
            )

            events = trace.read_all()
            self.assertGreater(len(events), 0)

    def test_confirmation_denial_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(goal="confirmed task")
            trace = TraceRecorder(
                Path(directory) / "trace.jsonl"
            )
            fsm = AgentFSM(context)

            def observe() -> Observation:
                return Observation(
                    success=True,
                    fingerprint="stable",
                )

            def plan(
                _context: AgentContext,
            ) -> list[AgentAction]:
                return [
                    AgentAction(
                        ActionType.TAP,
                        {"x": 1, "y": 1},
                        requires_confirmation=True,
                    )
                ]

            def execute(
                _action: AgentAction,
            ) -> ActionResult:
                return ActionResult(success=True)

            def verify_goal(
                _context: AgentContext,
                _observation: Observation | None,
            ) -> GoalResult:
                return GoalResult(
                    success=True,
                    reason="should not be reached",
                )

            def recover(
                _context: AgentContext,
            ) -> bool:
                return True

            executor = AgentExecutor(
                context=context,
                fsm=fsm,
                trace=trace,
                observer=observe,
                planner=plan,
                action_executor=execute,
                goal_verifier=verify_goal,
                recovery_handler=recover,
                confirmation_handler=lambda _action: False,
            )

            result = executor.run()

            self.assertFalse(result.success)
            self.assertEqual(fsm.state, AgentState.FAILED)
            self.assertEqual(
                result.reason,
                "Action confirmation denied",
            )


if __name__ == "__main__":
    unittest.main()
