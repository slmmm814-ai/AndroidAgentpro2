from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .fsm import FSMLimits
from .models import ActionType, AgentAction, AgentContext, Observation


class PlannerValidationError(ValueError):
    """Raised when a candidate plan is structurally invalid."""


class NoValidPlanError(RuntimeError):
    """Raised when no candidate plan can be selected."""


@dataclass(frozen=True)
class PlanCandidate:
    name: str
    actions: tuple[AgentAction, ...]
    confidence: float
    risk: float
    rationale: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise PlannerValidationError("plan name must not be empty")

        if not isinstance(self.actions, tuple):
            raise PlannerValidationError("actions must be a tuple")

        if not self.actions:
            raise PlannerValidationError("plan must contain at least one action")

        if not 0.0 <= self.confidence <= 1.0:
            raise PlannerValidationError(
                "confidence must be between 0.0 and 1.0"
            )

        if not 0.0 <= self.risk <= 1.0:
            raise PlannerValidationError("risk must be between 0.0 and 1.0")

        if not isinstance(self.rationale, str):
            raise PlannerValidationError("rationale must be a string")

        if not isinstance(self.metadata, Mapping):
            raise PlannerValidationError("metadata must be a mapping")

        for action in self.actions:
            if not isinstance(action, AgentAction):
                raise PlannerValidationError(
                    "all plan actions must be AgentAction instances"
                )


@dataclass(frozen=True)
class PlanEvaluation:
    candidate_name: str
    valid: bool
    score: float
    reason: str
    action_count: int
    confirmation_count: int


@dataclass(frozen=True)
class PlannerDecision:
    selected: PlanCandidate
    evaluation: PlanEvaluation
    rejected: tuple[PlanEvaluation, ...]


class CandidateProvider(Protocol):
    def generate(
        self,
        context: AgentContext,
        observation: Observation | None,
    ) -> Sequence[PlanCandidate]:
        ...


class MetaPlanner:
    """
    Deterministic meta-planner.

    The component does not execute actions. It validates and ranks candidate
    plans using deterministic constraints and a stable scoring function.
    """

    def __init__(
        self,
        limits: FSMLimits | None = None,
        *,
        max_plan_actions: int | None = None,
        min_confidence: float = 0.0,
        max_risk: float = 1.0,
    ) -> None:
        self.limits = limits or FSMLimits()

        if max_plan_actions is None:
            max_plan_actions = self.limits.max_actions

        if max_plan_actions < 1:
            raise ValueError("max_plan_actions must be >= 1")

        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0")

        if not 0.0 <= max_risk <= 1.0:
            raise ValueError("max_risk must be between 0.0 and 1.0")

        self.max_plan_actions = max_plan_actions
        self.min_confidence = min_confidence
        self.max_risk = max_risk

    def _validate_context(self, context: AgentContext) -> None:
        context.validate()

    def evaluate(
        self,
        candidate: PlanCandidate,
        context: AgentContext,
    ) -> PlanEvaluation:
        self._validate_context(context)

        action_count = len(candidate.actions)
        confirmation_count = sum(
            1 for action in candidate.actions if action.requires_confirmation
        )

        if action_count > self.max_plan_actions:
            return PlanEvaluation(
                candidate_name=candidate.name,
                valid=False,
                score=float("-inf"),
                reason=(
                    f"Plan contains {action_count} actions; "
                    f"maximum allowed is {self.max_plan_actions}"
                ),
                action_count=action_count,
                confirmation_count=confirmation_count,
            )

        if context.action_count + action_count > self.limits.max_actions:
            return PlanEvaluation(
                candidate_name=candidate.name,
                valid=False,
                score=float("-inf"),
                reason=(
                    "Plan would exceed the remaining action budget"
                ),
                action_count=action_count,
                confirmation_count=confirmation_count,
            )

        if candidate.confidence < self.min_confidence:
            return PlanEvaluation(
                candidate_name=candidate.name,
                valid=False,
                score=float("-inf"),
                reason=(
                    f"Confidence {candidate.confidence:.3f} is below "
                    f"minimum {self.min_confidence:.3f}"
                ),
                action_count=action_count,
                confirmation_count=confirmation_count,
            )

        if candidate.risk > self.max_risk:
            return PlanEvaluation(
                candidate_name=candidate.name,
                valid=False,
                score=float("-inf"),
                reason=(
                    f"Risk {candidate.risk:.3f} exceeds "
                    f"maximum {self.max_risk:.3f}"
                ),
                action_count=action_count,
                confirmation_count=confirmation_count,
            )

        confirmation_penalty = min(
            0.20,
            confirmation_count * 0.05,
        )

        length_penalty = min(
            0.20,
            max(0, action_count - 1) * 0.02,
        )

        score = (
            (candidate.confidence * 0.60)
            + ((1.0 - candidate.risk) * 0.30)
            + (1.0 / max(1, action_count) * 0.10)
            - confirmation_penalty
            - length_penalty
        )

        return PlanEvaluation(
            candidate_name=candidate.name,
            valid=True,
            score=score,
            reason="Candidate satisfies all planner constraints",
            action_count=action_count,
            confirmation_count=confirmation_count,
        )

    def select(
        self,
        candidates: Sequence[PlanCandidate],
        context: AgentContext,
    ) -> PlannerDecision:
        self._validate_context(context)

        if not candidates:
            raise NoValidPlanError("No candidate plans were provided")

        evaluations: list[PlanEvaluation] = []
        valid: list[tuple[PlanCandidate, PlanEvaluation]] = []

        for candidate in candidates:
            if not isinstance(candidate, PlanCandidate):
                raise PlannerValidationError(
                    "all candidates must be PlanCandidate instances"
                )

            evaluation = self.evaluate(candidate, context)
            evaluations.append(evaluation)

            if evaluation.valid:
                valid.append((candidate, evaluation))

        if not valid:
            reasons = "; ".join(
                f"{item.candidate_name}: {item.reason}"
                for item in evaluations
            )
            raise NoValidPlanError(
                f"No valid plan candidates: {reasons}"
            )

        # Stable deterministic ordering:
        # 1. highest score
        # 2. highest confidence
        # 3. lowest risk
        # 4. shortest plan
        # 5. lexical plan name
        valid.sort(
            key=lambda item: (
                -item[1].score,
                -item[0].confidence,
                item[0].risk,
                len(item[0].actions),
                item[0].name,
            )
        )

        selected, selected_evaluation = valid[0]

        selected_evaluation_id = id(selected_evaluation)
        rejected = tuple(
            evaluation
            for evaluation in evaluations
            if id(evaluation) != selected_evaluation_id
        )

        return PlannerDecision(
            selected=selected,
            evaluation=selected_evaluation,
            rejected=rejected,
        )

    def plan(
        self,
        context: AgentContext,
        observation: Observation | None,
        provider: CandidateProvider,
    ) -> list[AgentAction]:
        if not hasattr(provider, "generate"):
            raise TypeError("provider must implement generate()")

        candidates = provider.generate(context, observation)
        decision = self.select(candidates, context)

        return list(decision.selected.actions)


class StaticCandidateProvider:
    """Small deterministic provider useful for local integration and tests."""

    def __init__(self, candidates: Sequence[PlanCandidate]) -> None:
        self._candidates = tuple(candidates)

    def generate(
        self,
        context: AgentContext,
        observation: Observation | None,
    ) -> Sequence[PlanCandidate]:
        context.validate()
        return self._candidates


def make_candidate(
    name: str,
    actions: Sequence[AgentAction],
    *,
    confidence: float,
    risk: float,
    rationale: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> PlanCandidate:
    return PlanCandidate(
        name=name,
        actions=tuple(actions),
        confidence=confidence,
        risk=risk,
        rationale=rationale,
        metadata=dict(metadata or {}),
    )


def action_type_counts(
    actions: Sequence[AgentAction],
) -> dict[ActionType, int]:
    counts: dict[ActionType, int] = {}

    for action in actions:
        if not isinstance(action, AgentAction):
            raise TypeError("all actions must be AgentAction instances")

        counts[action.action_type] = counts.get(action.action_type, 0) + 1

    return counts
