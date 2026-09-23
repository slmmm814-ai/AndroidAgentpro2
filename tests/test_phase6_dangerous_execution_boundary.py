from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentpro.authorization import (
    AuthorizationDecision,
    AuthorizationError,
    AuthorizationService,
    DangerousActionPolicy,
    OwnerConfirmation,
    action_fingerprint,
)
from agentpro.executor import AgentExecutor, ExecutionResult
from agentpro.fsm import AgentFSM
from agentpro.models import (
    ActionResult,
    ActionType,
    AgentAction,
    AgentContext,
    AgentState,
    GoalResult,
    Observation,
)
from agentpro.trace import TraceRecorder


class DangerousExecutionBoundaryTests(unittest.TestCase):
    def _run_executor(
        self,
        action: AgentAction,
        *,
        confirmation_handler=None,
        authorization_service=None,
        owner_confirmation_handler=None,
        execute_success: bool = True,
        verify_success: bool = True,
    ) -> tuple[
        ExecutionResult,
        AgentContext,
        list[ActionType],
        int,
        list[dict[str, object]],
]:
        with tempfile.TemporaryDirectory() as directory:
            context = AgentContext(goal="complete task")
            trace = TraceRecorder(
                Path(directory) / "trace.jsonl"
            )
            fsm = AgentFSM(context)

            executed: list[ActionType] = []
            verification_calls = 0

            def observe() -> Observation:
                return Observation(
                    success=True,
                    package_name="com.example",
                    activity_name="MainActivity",
                    fingerprint="stable",
                )

            def plan(
                current_context: AgentContext,
            ) -> list[AgentAction]:
                if current_context.action_count == 0:
                    return [action]
                return []

            def execute(
                executable_action: AgentAction,
            ) -> ActionResult:
                executed.append(executable_action.action_type)
                return ActionResult(
                    success=execute_success,
                    operation_id=1,
                    error_code=None
                    if execute_success
                    else "EXECUTION_FAILED",
                    error_message=None
                    if execute_success
                    else "execution failed",
                )

            def verify_goal(
                _context: AgentContext,
                _observation: Observation | None,
            ) -> GoalResult:
                nonlocal verification_calls
                verification_calls += 1
                return GoalResult(
                    success=verify_success,
                    reason=(
                        "verified"
                        if verify_success
                        else "verification failed"
                    ),
                )

            def recover(
                _context: AgentContext,
            ) -> bool:
                return False

            executor = AgentExecutor(
                context=context,
                fsm=fsm,
                trace=trace,
                observer=observe,
                planner=plan,
                action_executor=execute,
                goal_verifier=verify_goal,
                recovery_handler=recover,
                confirmation_handler=confirmation_handler,
                authorization_service=authorization_service,
                owner_confirmation_handler=owner_confirmation_handler,
            )

            result = executor.run()

            return (
        result,
        context,
        executed,
        verification_calls,
        trace.read_all(),
    )

    @staticmethod
    def _install_action(
        **arguments: object,
    ) -> AgentAction:
        return AgentAction(
            action_type=ActionType.INSTALL_APK,
            arguments=arguments
            or {"path": "/sdcard/Download/test.apk"},
        )

    def test_install_apk_is_classified_as_dangerous(self) -> None:
        action = self._install_action()

        self.assertTrue(
            DangerousActionPolicy.is_dangerous(action)
        )
        self.assertTrue(
            DangerousActionPolicy.requires_owner_confirmation(action)
        )

    def test_install_apk_owner_confirmation_allows_execute_and_verify(
        self,
    ) -> None:
        service = AuthorizationService()
        action = self._install_action(path="/tmp/approved.apk")

        def owner_confirm(received_action: AgentAction, request_id: str) -> OwnerConfirmation:
            request = service.get_confirmation_request(request_id)
            self.assertIsNotNone(request)
            return service.grant(request, received_action, granted=True)

        result, context, executed, verification_calls, trace = self._run_executor(
            action,
            authorization_service=service,
            owner_confirmation_handler=owner_confirm,
        )

        self.assertTrue(result.success)
        self.assertEqual(context.state, AgentState.SUCCESS)
        self.assertEqual(executed, [ActionType.INSTALL_APK])
        self.assertGreaterEqual(verification_calls, 1)
        self.assertTrue(any(event.get("event") == "dangerous_action_authorization_result" and event.get("payload", {}).get("decision") == AuthorizationDecision.GRANTED.value for event in trace))
        serialized_trace = str(trace)
        self.assertNotIn("action_fingerprint", serialized_trace)
        self.assertNotIn("request_id", serialized_trace)
        self.assertNotIn("token", serialized_trace)
        self.assertNotIn("credential", serialized_trace)

    def test_authorization_service_requires_confirmation_for_install_apk(
        self,
    ) -> None:
        service = AuthorizationService()
        action = self._install_action()

        result = service.authorize(action, None)

        self.assertEqual(
            result.decision,
            AuthorizationDecision.NEED_CONFIRMATION,
        )

    def test_owner_denial_is_denied_by_authorization_service(self) -> None:
        service = AuthorizationService()
        action = self._install_action()

        request = service.create_confirmation_request(action)
        confirmation = service.grant(
            request,
            action,
            granted=False,
        )

        result = service.authorize(action, confirmation)

        self.assertEqual(
            result.decision,
            AuthorizationDecision.DENIED,
        )
        self.assertEqual(
            result.reason,
            "owner denied the action",
        )

    def test_exact_owner_confirmation_is_granted_by_authorization_service(
        self,
    ) -> None:
        service = AuthorizationService()
        action = self._install_action()

        request = service.create_confirmation_request(action)
        confirmation = service.grant(
            request,
            action,
            granted=True,
        )

        result = service.authorize(action, confirmation)

        self.assertEqual(
            result.decision,
            AuthorizationDecision.GRANTED,
        )
        self.assertEqual(
            confirmation.action_fingerprint,
            action_fingerprint(action),
        )

    def test_expired_confirmation_is_rejected(self) -> None:
        current_time = 1000.0

        service = AuthorizationService(
            confirmation_ttl_seconds=10.0,
            clock=lambda: current_time,
        )
        action = self._install_action()

        request = service.create_confirmation_request(action)

        expired_service = AuthorizationService(
            confirmation_ttl_seconds=10.0,
            clock=lambda: request.expires_at + 1.0,
        )

        with self.assertRaises(AuthorizationError):
            expired_service.grant(
                request,
                action,
                granted=True,
            )

    def test_forged_request_id_is_rejected(self) -> None:
        service = AuthorizationService()
        action = self._install_action()

        request = service.create_confirmation_request(action)
        confirmation = service.grant(
            request,
            action,
            granted=True,
        )

        forged = OwnerConfirmation(
            request_id="forged-request-id",
            action_fingerprint=confirmation.action_fingerprint,
            granted=True,
            decided_at=confirmation.decided_at,
        )

        result = service.authorize(action, forged)

        self.assertEqual(
            result.decision,
            AuthorizationDecision.DENIED,
        )
        self.assertEqual(
            result.reason,
            "confirmation request is unknown",
        )

    def test_action_mismatch_is_rejected(self) -> None:
        service = AuthorizationService()

        approved_action = self._install_action(
            path="/sdcard/Download/approved.apk"
        )
        different_action = self._install_action(
            path="/sdcard/Download/different.apk"
        )

        request = service.create_confirmation_request(
            approved_action
        )
        confirmation = service.grant(
            request,
            approved_action,
            granted=True,
        )

        result = service.authorize(
            different_action,
            confirmation,
        )

        self.assertEqual(
            result.decision,
            AuthorizationDecision.DENIED,
        )

    def test_fingerprint_mismatch_is_rejected(self) -> None:
        service = AuthorizationService()
        action = self._install_action()

        request = service.create_confirmation_request(action)
        confirmation = service.grant(
            request,
            action,
            granted=True,
        )

        modified_action = self._install_action(
            path="/sdcard/Download/modified.apk"
        )

        self.assertNotEqual(
            confirmation.action_fingerprint,
            action_fingerprint(modified_action),
        )

        result = service.authorize(
            modified_action,
            confirmation,
        )

        self.assertEqual(
            result.decision,
            AuthorizationDecision.DENIED,
        )

    def test_confirmation_from_different_service_is_rejected(
        self,
    ) -> None:
        issuing_service = AuthorizationService()
        execution_service = AuthorizationService()
        action = self._install_action()

        request = issuing_service.create_confirmation_request(
            action
        )
        confirmation = issuing_service.grant(
            request,
            action,
            granted=True,
        )

        result = execution_service.authorize(
            action,
            confirmation,
        )

        self.assertEqual(
            result.decision,
            AuthorizationDecision.DENIED,
        )

    def test_install_apk_without_owner_confirmation_does_not_execute(
        self,
    ) -> None:
        action = self._install_action()

        result, _, executed, _, _ = self._run_executor(action)

        self.assertFalse(result.success)
        self.assertEqual(executed, [])
        self.assertNotEqual(result.reason, "goal completed")

    def test_install_apk_cannot_be_authorized_by_generic_confirmation_handler(
        self,
    ) -> None:
        action = self._install_action()

        result, _, executed, _, _ = self._run_executor(
            action,
            confirmation_handler=lambda _action: True,
        )

        self.assertFalse(result.success)
        self.assertEqual(executed, [])

    def test_install_apk_denial_from_generic_handler_does_not_define_owner_authorization(
        self,
    ) -> None:
        action = self._install_action()

        result, _, executed, _, _ = self._run_executor(
            action,
            confirmation_handler=lambda _action: False,
        )

        self.assertFalse(result.success)
        self.assertEqual(executed, [])

    def test_safe_action_remains_backward_compatible(self) -> None:
        action = AgentAction(
            action_type=ActionType.TAP,
            arguments={"x": 10, "y": 20},
        )

        result, _, executed, verification_calls, _ = (
            self._run_executor(action)
        )

        self.assertTrue(result.success)
        self.assertEqual(
            executed,
            [ActionType.TAP],
        )
        self.assertGreaterEqual(
            verification_calls,
            1,
        )

    def test_verification_remains_mandatory_after_execution(self) -> None:
        action = AgentAction(
            action_type=ActionType.TAP,
            arguments={"x": 10, "y": 20},
        )

        result, _, executed, verification_calls, _ = (
            self._run_executor(
                action,
                verify_success=False,
            )
        )

        self.assertEqual(
            executed,
            [ActionType.TAP],
        )
        self.assertGreaterEqual(
            verification_calls,
            1,
        )
        self.assertFalse(result.success)

    def test_failed_authorization_cannot_produce_false_success(self) -> None:
        action = self._install_action()

        result, context, executed, _, _ = self._run_executor(
            action,
        )

        self.assertIsInstance(result, ExecutionResult)
        self.assertFalse(result.success)
        self.assertEqual(executed, [])
        self.assertNotEqual(
            context.state,
            AgentState.SUCCESS,
        )

    def test_trace_does_not_expose_owner_authorization_material(
        self,
    ) -> None:
        action = self._install_action()

        result, _, _, _, trace = self._run_executor(
            action,
            confirmation_handler=lambda _action: False,
        )

        self.assertFalse(result.success)

        serialized_trace = str(trace)

        self.assertNotIn("action_fingerprint", serialized_trace)
        self.assertNotIn("request_id", serialized_trace)
        self.assertNotIn("granted", serialized_trace)
        self.assertNotIn("token", serialized_trace)
        self.assertNotIn("credential", serialized_trace)


    def test_confirmation_required_action_without_handler_fails_closed(self) -> None:
        action = AgentAction(
            action_type=ActionType.TAP,
            arguments={"x": 10, "y": 20},
            requires_confirmation=True,
        )

        result, _, executed, _, _ = self._run_executor(
            action,
            confirmation_handler=None,
        )

        self.assertFalse(result.success)
        self.assertEqual(executed, [])


if __name__ == "__main__":
    unittest.main()
