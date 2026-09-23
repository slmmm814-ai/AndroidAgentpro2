from __future__ import annotations

import unittest

from agentpro.models import ActionResult, ActionType, AgentAction, GoalResult
from agentpro.verifier import (
    SixLayerVerifier,
    VerificationDecision,
    VerificationLayer,
    VerificationRequest,
)


class TestPhase3Verifier(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = SixLayerVerifier()

    def _valid_request(self) -> VerificationRequest:
        return VerificationRequest(
            action=AgentAction(
                ActionType.TAP,
                {"x": 10, "y": 10},
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
        )

    def test_all_six_layers_pass_for_valid_request(self) -> None:
        report = self.verifier.verify(self._valid_request())

        self.assertEqual(report.decision, VerificationDecision.ACCEPT)
        self.assertTrue(report.accepted)
        self.assertEqual(len(report.layers), 6)
        self.assertEqual(
            tuple(layer.layer for layer in report.layers),
            (
                VerificationLayer.SCHEMA,
                VerificationLayer.ACTION_RESULT,
                VerificationLayer.SAFETY,
                VerificationLayer.EVIDENCE,
                VerificationLayer.CORRELATION,
                VerificationLayer.GOAL,
            ),
        )
        self.assertTrue(all(layer.passed for layer in report.layers))

    def test_schema_layer_rejects_invalid_operation_id(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=0,
            ),
            evidence={
                "operation_id": 0,
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
        self.assertEqual(report.error_code, "INVALID_OPERATION_ID")
        self.assertEqual(report.layers[0].layer, VerificationLayer.SCHEMA)
        self.assertFalse(report.layers[0].passed)

    def test_action_result_layer_rejects_failed_action(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=False,
                error_code="ACTION_FAILED",
                error_message="tap failed",
            ),
            evidence=None,
            goal_result=None,
        )

        report = self.verifier.verify(request)

        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "ACTION_FAILED")
        self.assertEqual(
            report.layers[1].layer,
            VerificationLayer.ACTION_RESULT,
        )
        self.assertFalse(report.layers[1].passed)

    def test_safety_layer_requires_confirmation(self) -> None:
        valid = self._valid_request()

        request = VerificationRequest(
            action=AgentAction(
                ActionType.TAP,
                {"x": 10, "y": 10},
                requires_confirmation=True,
            ),
            action_result=valid.action_result,
            evidence=valid.evidence,
            goal_result=valid.goal_result,
            confirmation_granted=False,
        )

        report = self.verifier.verify(request)

        self.assertEqual(
            report.decision,
            VerificationDecision.NEED_CONFIRMATION,
        )
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "CONFIRMATION_REQUIRED")
        self.assertEqual(report.layers[2].layer, VerificationLayer.SAFETY)
        self.assertFalse(report.layers[2].passed)

    def test_safety_layer_passes_after_confirmation(self) -> None:
        valid = self._valid_request()

        request = VerificationRequest(
            action=AgentAction(
                ActionType.TAP,
                {"x": 10, "y": 10},
                requires_confirmation=True,
            ),
            action_result=valid.action_result,
            evidence=valid.evidence,
            goal_result=valid.goal_result,
            confirmation_granted=True,
        )

        report = self.verifier.verify(request)

        self.assertEqual(report.decision, VerificationDecision.ACCEPT)
        self.assertTrue(report.accepted)
        self.assertTrue(report.layers[2].passed)

    def test_evidence_layer_rejects_missing_evidence(self) -> None:
        valid = self._valid_request()

        request = VerificationRequest(
            action=valid.action,
            action_result=valid.action_result,
            evidence=None,
            goal_result=valid.goal_result,
        )

        report = self.verifier.verify(request)

        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "MISSING_EVIDENCE")
        self.assertEqual(report.layers[3].layer, VerificationLayer.EVIDENCE)
        self.assertFalse(report.layers[3].passed)

    def test_evidence_layer_rejects_unverified_evidence(self) -> None:
        valid = self._valid_request()

        request = VerificationRequest(
            action=valid.action,
            action_result=valid.action_result,
            evidence={
                "operation_id": 1,
                "verified": False,
            },
            goal_result=valid.goal_result,
        )

        report = self.verifier.verify(request)

        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "EVIDENCE_NOT_VERIFIED")
        self.assertFalse(report.layers[3].passed)

    def test_correlation_layer_rejects_wrong_operation(self) -> None:
        valid = self._valid_request()

        request = VerificationRequest(
            action=valid.action,
            action_result=valid.action_result,
            evidence={
                "operation_id": 999,
                "verified": True,
            },
            goal_result=valid.goal_result,
        )

        report = self.verifier.verify(request)

        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "OPERATION_MISMATCH")
        self.assertEqual(
            report.layers[4].layer,
            VerificationLayer.CORRELATION,
        )
        self.assertFalse(report.layers[4].passed)

    def test_correlation_layer_accepts_matching_operation(self) -> None:
        report = self.verifier.verify(self._valid_request())

        self.assertTrue(report.layers[4].passed)
        self.assertEqual(
            report.layers[4].layer,
            VerificationLayer.CORRELATION,
        )

    def test_goal_layer_rejects_missing_goal_result(self) -> None:
        valid = self._valid_request()

        request = VerificationRequest(
            action=valid.action,
            action_result=valid.action_result,
            evidence=valid.evidence,
            goal_result=None,
        )

        report = self.verifier.verify(request)

        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "MISSING_GOAL_RESULT")
        self.assertEqual(report.layers[5].layer, VerificationLayer.GOAL)
        self.assertFalse(report.layers[5].passed)

    def test_goal_layer_rejects_failed_goal(self) -> None:
        valid = self._valid_request()

        request = VerificationRequest(
            action=valid.action,
            action_result=valid.action_result,
            evidence=valid.evidence,
            goal_result=GoalResult(
                success=False,
                reason="goal not reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "GOAL_NOT_REACHED")
        self.assertFalse(report.layers[5].passed)

    def test_goal_layer_accepts_reached_goal(self) -> None:
        report = self.verifier.verify(self._valid_request())

        self.assertTrue(report.layers[5].passed)
        self.assertEqual(
            report.layers[5].layer,
            VerificationLayer.GOAL,
        )

    def test_malformed_request_is_rejected_without_exception(self) -> None:
        report = self.verifier.verify(None)  # type: ignore[arg-type]

        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "INVALID_REQUEST")
        self.assertEqual(len(report.layers), 1)
        self.assertEqual(report.layers[0].layer, VerificationLayer.SCHEMA)
        self.assertFalse(report.layers[0].passed)


class TestPhase3SuccessTypeHardening(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = SixLayerVerifier()

    def test_action_success_integer_one_is_rejected(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(success=1, operation_id=1),
            evidence={
                "operation_id": 1,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "INVALID_ACTION_SUCCESS")

    def test_goal_success_integer_one_is_rejected(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(success=True, operation_id=1),
            evidence={
                "operation_id": 1,
                "verified": True,
            },
            goal_result=GoalResult(
                success=1,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "INVALID_GOAL_SUCCESS")


if __name__ == "__main__":
    unittest.main()


class TestPhase3FailClosedIndependence(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = SixLayerVerifier()

    def _valid_request(self) -> VerificationRequest:
        return VerificationRequest(
            action=AgentAction(
                ActionType.TAP,
                {"x": 10, "y": 10},
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
        )

    def test_schema_failure_cannot_be_overridden_by_successful_later_layers(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=0,
            ),
            evidence={
                "operation_id": 0,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "INVALID_OPERATION_ID")
        self.assertFalse(report.layers[0].passed)
        self.assertTrue(all(layer.passed for layer in report.layers[1:]))

    def test_action_failure_cannot_be_overridden_by_valid_evidence_and_goal(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=False,
                operation_id=1,
                error_code="ACTION_FAILED",
                error_message="tap failed",
            ),
            evidence={
                "operation_id": 1,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "ACTION_FAILED")
        self.assertFalse(report.layers[1].passed)

    def test_safety_failure_cannot_be_overridden_by_valid_execution_and_goal(self) -> None:
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

        self.assertFalse(report.accepted)
        self.assertEqual(
            report.decision,
            VerificationDecision.NEED_CONFIRMATION,
        )
        self.assertEqual(report.error_code, "CONFIRMATION_REQUIRED")
        self.assertFalse(report.layers[2].passed)

    def test_evidence_failure_cannot_be_overridden_by_successful_goal(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=1,
            ),
            evidence={
                "operation_id": 1,
                "verified": False,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "EVIDENCE_NOT_VERIFIED")
        self.assertFalse(report.layers[3].passed)

    def test_correlation_failure_cannot_be_overridden_by_successful_goal(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=1,
            ),
            evidence={
                "operation_id": 999,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "OPERATION_MISMATCH")
        self.assertFalse(report.layers[4].passed)

    def test_goal_failure_is_final_rejection_even_when_all_previous_layers_pass(self) -> None:
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

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "GOAL_NOT_REACHED")
        self.assertTrue(all(layer.passed for layer in report.layers[:5]))
        self.assertFalse(report.layers[5].passed)

    def test_accept_requires_exactly_six_passed_layers(self) -> None:
        report = self.verifier.verify(self._valid_request())

        self.assertEqual(report.decision, VerificationDecision.ACCEPT)
        self.assertTrue(report.accepted)
        self.assertEqual(len(report.layers), 6)
        self.assertEqual(len(report.passed_layers), 6)
        self.assertEqual(len(report.failed_layers), 0)

    def test_arbitrary_object_is_rejected_without_exception(self) -> None:
        report = self.verifier.verify(object())  # type: ignore[arg-type]

        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertFalse(report.accepted)
        self.assertEqual(report.error_code, "INVALID_REQUEST")
        self.assertEqual(len(report.layers), 1)
        self.assertEqual(report.layers[0].layer, VerificationLayer.SCHEMA)
        self.assertFalse(report.layers[0].passed)


if __name__ == "__main__":
    unittest.main()


class TestPhase3VerifierInputHardening(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = SixLayerVerifier()

    def _base_request(
        self,
        evidence: object,
    ) -> VerificationRequest:
        return VerificationRequest(
            action=AgentAction(
                ActionType.TAP,
                {"x": 10, "y": 10},
            ),
            action_result=ActionResult(
                success=True,
                operation_id=1,
            ),
            evidence=evidence,  # type: ignore[arg-type]
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

    def test_evidence_verified_string_true_is_not_boolean_true(self) -> None:
        request = self._base_request(
            {
                "operation_id": 1,
                "verified": "true",
            }
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "EVIDENCE_NOT_VERIFIED")

    def test_evidence_verified_integer_one_is_not_boolean_true(self) -> None:
        request = self._base_request(
            {
                "operation_id": 1,
                "verified": 1,
            }
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "EVIDENCE_NOT_VERIFIED")

    def test_evidence_operation_id_string_does_not_match_integer_id(self) -> None:
        request = self._base_request(
            {
                "operation_id": "1",
                "verified": True,
            }
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "OPERATION_MISMATCH")

    def test_evidence_list_is_rejected_without_exception(self) -> None:
        request = self._base_request(["operation_id", 1])

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "INVALID_REQUEST")

    def test_evidence_boolean_is_rejected_without_exception(self) -> None:
        request = self._base_request(True)

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "INVALID_REQUEST")

    def test_negative_operation_id_is_rejected(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=-1,
            ),
            evidence={
                "operation_id": -1,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "INVALID_OPERATION_ID")

    def test_missing_operation_id_cannot_be_successful(self) -> None:
        request = VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=None,
            ),
            evidence={
                "operation_id": None,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "MISSING_OPERATION_ID")

    def test_empty_goal_reason_is_rejected(self) -> None:
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
                success=True,
                reason="   ",
            ),
        )

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "EMPTY_GOAL_REASON")


if __name__ == "__main__":
    unittest.main()


class TestPhase3OperationIdHardening(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = SixLayerVerifier()

    def _request_with_operation_id(self, operation_id):
        return VerificationRequest(
            action=AgentAction(ActionType.TAP, {"x": 10, "y": 10}),
            action_result=ActionResult(
                success=True,
                operation_id=operation_id,
            ),
            evidence={
                "operation_id": operation_id,
                "verified": True,
            },
            goal_result=GoalResult(
                success=True,
                reason="goal reached",
            ),
        )

    def test_action_operation_id_string_is_rejected(self) -> None:
        request = self._request_with_operation_id("1")

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "INVALID_OPERATION_ID")

    def test_action_operation_id_boolean_is_rejected(self) -> None:
        request = self._request_with_operation_id(True)

        report = self.verifier.verify(request)

        self.assertFalse(report.accepted)
        self.assertEqual(report.decision, VerificationDecision.REJECT)
        self.assertEqual(report.error_code, "INVALID_OPERATION_ID")


if __name__ == "__main__":
    unittest.main()
