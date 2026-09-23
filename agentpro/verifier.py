# File: agentpro/verifier.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .authorization import AuthorizationService, DangerousActionPolicy, OwnerConfirmation
from .models import ActionResult, AgentAction, GoalResult


class VerificationLayer(str, Enum):
    SCHEMA = "SCHEMA"
    ACTION_RESULT = "ACTION_RESULT"
    SAFETY = "SAFETY"
    EVIDENCE = "EVIDENCE"
    CORRELATION = "CORRELATION"
    GOAL = "GOAL"


class VerificationDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NEED_CONFIRMATION = "NEED_CONFIRMATION"


@dataclass(frozen=True)
class LayerResult:
    layer: VerificationLayer
    passed: bool
    code: str
    message: str


@dataclass(frozen=True)
class VerificationRequest:
    action: AgentAction
    action_result: ActionResult
    evidence: Any = None
    goal_result: GoalResult | None = None
    confirmation_granted: bool = False
    authorization: OwnerConfirmation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, AgentAction):
            raise TypeError("action must be an AgentAction")

        if not isinstance(self.action_result, ActionResult):
            raise TypeError("action_result must be an ActionResult")

        if self.goal_result is not None and not isinstance(
            self.goal_result, GoalResult
        ):
            raise TypeError("goal_result must be a GoalResult or None")

        if not isinstance(self.confirmation_granted, bool):
            raise TypeError("confirmation_granted must be bool")
        if self.authorization is not None and not isinstance(self.authorization, OwnerConfirmation):
            raise TypeError("authorization must be OwnerConfirmation or None")


@dataclass(frozen=True)
class VerificationReport:
    decision: VerificationDecision
    accepted: bool
    layers: tuple[LayerResult, ...] = field(default_factory=tuple)
    error_code: str | None = None
    message: str = ""

    @property
    def passed_layers(self) -> tuple[VerificationLayer, ...]:
        return tuple(result.layer for result in self.layers if result.passed)

    @property
    def failed_layers(self) -> tuple[VerificationLayer, ...]:
        return tuple(result.layer for result in self.layers if not result.passed)


class SixLayerVerifier:
    """Fail-closed deterministic verifier for Phase 3."""

    _REQUIRED_EVIDENCE_KEYS = frozenset(("operation_id", "verified"))

    def __init__(self, authorization_service: AuthorizationService | None = None) -> None:
        self._authorization_service = authorization_service or AuthorizationService()
        self._dangerous_action_policy = DangerousActionPolicy()

    def verify(self, request: VerificationRequest | None) -> VerificationReport:
        if not isinstance(request, VerificationRequest):
            return VerificationReport(
                decision=VerificationDecision.REJECT,
                accepted=False,
                layers=(
                    LayerResult(
                        VerificationLayer.SCHEMA,
                        False,
                        "INVALID_REQUEST",
                        "verification request is missing or invalid",
                    ),
                ),
                error_code="INVALID_REQUEST",
                message="verification request is invalid",
            )

        if request.evidence is not None and not isinstance(
            request.evidence, Mapping
        ):
            return VerificationReport(
                decision=VerificationDecision.REJECT,
                accepted=False,
                layers=(
                    LayerResult(
                        VerificationLayer.SCHEMA,
                        False,
                        "INVALID_REQUEST",
                        "evidence must be a mapping or None",
                    ),
                ),
                error_code="INVALID_REQUEST",
                message="verification request contains invalid evidence type",
            )

        try:
            layers = (
                self._schema_layer(request),
                self._action_result_layer(request),
                self._safety_layer(request),
                self._evidence_layer(request),
                self._correlation_layer(request),
                self._goal_layer(request),
            )
        except Exception as exc:
            return VerificationReport(
                decision=VerificationDecision.REJECT,
                accepted=False,
                layers=(
                    LayerResult(
                        VerificationLayer.SCHEMA,
                        False,
                        "VERIFIER_INTERNAL_ERROR",
                        f"verification failed safely: {type(exc).__name__}",
                    ),
                ),
                error_code="VERIFIER_INTERNAL_ERROR",
                message="verification failed safely without accepting the request",
            )

        failed = next((layer for layer in layers if not layer.passed), None)

        if failed is not None:
            decision = (
                VerificationDecision.NEED_CONFIRMATION
                if (
                    failed.layer is VerificationLayer.SAFETY
                    and failed.code == "CONFIRMATION_REQUIRED"
                )
                else VerificationDecision.REJECT
            )

            return VerificationReport(
                decision=decision,
                accepted=False,
                layers=layers,
                error_code=failed.code,
                message=failed.message,
            )

        return VerificationReport(
            decision=VerificationDecision.ACCEPT,
            accepted=True,
            layers=layers,
            message="all six verification layers passed",
        )

    def _schema_layer(self, request: VerificationRequest) -> LayerResult:
        operation_id = request.action_result.operation_id

        if operation_id is not None:
            if isinstance(operation_id, bool) or not isinstance(operation_id, int):
                return LayerResult(
                    VerificationLayer.SCHEMA,
                    False,
                    "INVALID_OPERATION_ID",
                    "operation_id must be a positive integer when supplied",
                )

            if operation_id <= 0:
                return LayerResult(
                    VerificationLayer.SCHEMA,
                    False,
                    "INVALID_OPERATION_ID",
                    "operation_id must be positive when supplied",
                )

        if request.goal_result is not None:
            reason = request.goal_result.reason

            if not isinstance(reason, str) or not reason.strip():
                return LayerResult(
                    VerificationLayer.SCHEMA,
                    False,
                    "EMPTY_GOAL_REASON",
                    "goal verification reason must be a non-empty string",
                )

        return LayerResult(
            VerificationLayer.SCHEMA,
            True,
            "SCHEMA_VALID",
            "verification request has valid typed fields",
        )

    def _action_result_layer(self, request: VerificationRequest) -> LayerResult:
        result = request.action_result

        if not isinstance(result.success, bool):
            return LayerResult(
                VerificationLayer.ACTION_RESULT,
                False,
                "INVALID_ACTION_SUCCESS",
                "action result success must be a boolean",
            )

        if not result.success:
            return LayerResult(
                VerificationLayer.ACTION_RESULT,
                False,
                result.error_code or "ACTION_FAILED",
                result.error_message or "action execution failed",
            )

        return LayerResult(
            VerificationLayer.ACTION_RESULT,
            True,
            "ACTION_SUCCEEDED",
            "action result reports successful execution",
        )

    def _safety_layer(self, request: VerificationRequest) -> LayerResult:
        if self._dangerous_action_policy.is_dangerous(request.action):
            if request.authorization is None:
                return LayerResult(
                    VerificationLayer.SAFETY,
                    False,
                    "CONFIRMATION_REQUIRED",
                    "owner confirmation is required for dangerous actions",
                )

            authorization = self._authorization_service.authorize(
                request.action,
                request.authorization,
            )

            if authorization.decision.value != "GRANTED":
                return LayerResult(
                    VerificationLayer.SAFETY,
                    False,
                    "INVALID_AUTHORIZATION",
                    authorization.message,
                )

            return LayerResult(
                VerificationLayer.SAFETY,
                True,
                "SAFETY_CLEARED",
                "exact owner authorization is valid for the dangerous action",
            )

        if request.action.requires_confirmation and not request.confirmation_granted:
            return LayerResult(
                VerificationLayer.SAFETY,
                False,
                "CONFIRMATION_REQUIRED",
                "explicit confirmation is required",
            )

        return LayerResult(
            VerificationLayer.SAFETY,
            True,
            "SAFETY_CLEARED",
            "confirmation requirements are satisfied",
        )

    def _evidence_layer(self, request: VerificationRequest) -> LayerResult:
        evidence = request.evidence

        if evidence is None:
            return LayerResult(
                VerificationLayer.EVIDENCE,
                False,
                "MISSING_EVIDENCE",
                "execution evidence is required",
            )

        if not isinstance(evidence, Mapping):
            return LayerResult(
                VerificationLayer.EVIDENCE,
                False,
                "INVALID_EVIDENCE",
                "execution evidence must be a mapping",
            )

        missing = self._REQUIRED_EVIDENCE_KEYS.difference(evidence.keys())

        if missing:
            return LayerResult(
                VerificationLayer.EVIDENCE,
                False,
                "INCOMPLETE_EVIDENCE",
                "required evidence fields are missing: "
                + ", ".join(sorted(missing)),
            )

        if evidence.get("verified") is not True:
            return LayerResult(
                VerificationLayer.EVIDENCE,
                False,
                "EVIDENCE_NOT_VERIFIED",
                "execution evidence is not explicitly verified",
            )

        return LayerResult(
            VerificationLayer.EVIDENCE,
            True,
            "EVIDENCE_VERIFIED",
            "execution evidence is explicitly verified",
        )

    def _correlation_layer(self, request: VerificationRequest) -> LayerResult:
        operation_id = request.action_result.operation_id
        evidence = request.evidence

        if operation_id is None:
            return LayerResult(
                VerificationLayer.CORRELATION,
                False,
                "MISSING_OPERATION_ID",
                "successful action must provide an operation_id",
            )

        if isinstance(operation_id, bool) or not isinstance(operation_id, int):
            return LayerResult(
                VerificationLayer.CORRELATION,
                False,
                "INVALID_OPERATION_ID",
                "operation_id must be an integer",
            )

        if not isinstance(evidence, Mapping):
            return LayerResult(
                VerificationLayer.CORRELATION,
                False,
                "INVALID_EVIDENCE",
                "evidence cannot be correlated because it is not a mapping",
            )

        evidence_operation_id = evidence.get("operation_id")

        if isinstance(evidence_operation_id, bool) or not isinstance(
            evidence_operation_id, int
        ):
            return LayerResult(
                VerificationLayer.CORRELATION,
                False,
                "OPERATION_MISMATCH",
                "evidence operation_id must be an integer matching the action",
            )

        if evidence_operation_id != operation_id:
            return LayerResult(
                VerificationLayer.CORRELATION,
                False,
                "OPERATION_MISMATCH",
                "evidence does not belong to the current operation",
            )

        return LayerResult(
            VerificationLayer.CORRELATION,
            True,
            "OPERATION_CORRELATED",
            "evidence belongs to the current operation",
        )

    def _goal_layer(self, request: VerificationRequest) -> LayerResult:
        goal_result = request.goal_result

        if goal_result is None:
            return LayerResult(
                VerificationLayer.GOAL,
                False,
                "MISSING_GOAL_RESULT",
                "goal verification result is required",
            )

        if not isinstance(goal_result.success, bool):
            return LayerResult(
                VerificationLayer.GOAL,
                False,
                "INVALID_GOAL_SUCCESS",
                "goal result success must be a boolean",
            )

        if not goal_result.success:
            return LayerResult(
                VerificationLayer.GOAL,
                False,
                "GOAL_NOT_REACHED",
                goal_result.reason,
            )

        return LayerResult(
            VerificationLayer.GOAL,
            True,
            "GOAL_REACHED",
            goal_result.reason,
        )
