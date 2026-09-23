from __future__ import annotations

import unittest

from agentpro.models import ActionResult, ActionType, AgentAction, GoalResult
from agentpro.verifier import (
    VerificationDecision,
    VerificationLayer,
    VerificationRequest,
    SixLayerVerifier,
)


class TestPhase3FalseSuccessCases(unittest.TestCase):
    """
    Breaker cases are intentionally adversarial.

    These cases must never be accepted as successful verification:
    1. A failed action cannot become success because another field says success.
    2. Missing execution evidence cannot become success.
    3. A failed goal result cannot become success.
    4. Sensitive actions cannot be accepted without confirmation.
    5. Malformed verification input cannot become success.
    6. Evidence belonging to another operation cannot prove the current action.
    """

    def setUp(self) -> None:
        self.verifier = SixLayerVerifier()

    def test_failed_action_cannot_be_false_success(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=False,
                error_code="ACTION_FAILED",
                error_message="tap was rejected",
            ),
            evidence=None,
            goal_result=None,
        )

        report = self.verifier.verify(request)

        self.assertNotEqual(report.decision, VerificationDecision.ACCEPT)
        self.assertFalse(report.accepted)

    def test_missing_evidence_cannot_be_false_success(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=1,
            ),
            evidence=None,
            goal_result=GoalResult(
                success=True,
                reason="claimed complete",
            ),
        )

        report = self.verifier.verify(request)

        self.assertNotEqual(report.decision, VerificationDecision.ACCEPT)
        self.assertFalse(report.accepted)

    def test_failed_goal_cannot_be_false_success(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=1,
            ),
            evidence={
                "operation_id": 1,
                "verified": True,
            },
            goal_result=GoalResult(
                success=False,
                reason="goal not reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertNotEqual(report.decision, VerificationDecision.ACCEPT)
        self.assertFalse(report.accepted)

    def test_sensitive_action_requires_confirmation(self) -> None:
        request = VerificationRequest(
            action=AgentAction(
                ActionType.TAP,
                {"x": 10, "y": 10},
                requires_confirmation=True,
            ),
            action_result=ActionResult(
                success=True,
                operation_id=1,
            ),
            evidence={
                "operation_id": 1,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
            confirmation_granted=False,
        )

        report = self.verifier.verify(request)

        self.assertEqual(
            report.decision,
            VerificationDecision.NEED_CONFIRMATION,
        )
        self.assertFalse(report.accepted)

    def test_malformed_request_cannot_be_false_success(self) -> None:
        report = self.verifier.verify(None)  # type: ignore[arg-type]

        self.assertNotEqual(report.decision, VerificationDecision.ACCEPT)
        self.assertFalse(report.accepted)

    def test_wrong_operation_evidence_cannot_prove_action(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=7,
            ),
            evidence={
                "operation_id": 8,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertNotEqual(report.decision, VerificationDecision.ACCEPT)
        self.assertFalse(report.accepted)


if __name__ == "__main__":
    unittest.main()
