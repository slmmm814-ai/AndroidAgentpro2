from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .models import AgentContext, AgentState


class InvalidTransitionError(RuntimeError):
    """Raised when an FSM transition is not permitted."""


class StateLimitExceededError(RuntimeError):
    """Raised when an execution limit is exceeded."""


@dataclass(frozen=True)
class FSMLimits:
    max_steps: int = 100
    max_actions: int = 50
    max_recoveries: int = 5
    max_replans: int = 10

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.max_actions < 1:
            raise ValueError("max_actions must be >= 1")
        if self.max_recoveries < 0:
            raise ValueError("max_recoveries must be >= 0")
        if self.max_replans < 0:
            raise ValueError("max_replans must be >= 0")


class AgentFSM:
    _TRANSITIONS: Final[dict[AgentState, frozenset[AgentState]]] = {
        AgentState.IDLE: frozenset({AgentState.DECOMPOSE}),
        AgentState.DECOMPOSE: frozenset(
            {AgentState.OBSERVE, AgentState.RECOVER, AgentState.FAILED, AgentState.STUCK}
        ),
        AgentState.OBSERVE: frozenset(
            {AgentState.PLAN, AgentState.RECOVER, AgentState.FAILED, AgentState.STUCK}
        ),
        AgentState.PLAN: frozenset(
            {
                AgentState.EXECUTE,
                AgentState.VERIFY_GOAL,
                AgentState.NEED_CONFIRMATION,
                AgentState.RECOVER,
                AgentState.REPLAN,
                AgentState.FAILED,
                AgentState.STUCK,
            }
        ),
        AgentState.EXECUTE: frozenset(
            {
                AgentState.VERIFY_ACTION,
                AgentState.NEED_CONFIRMATION,
                AgentState.RECOVER,
                AgentState.FAILED,
                AgentState.STUCK,
            }
        ),
        AgentState.VERIFY_ACTION: frozenset(
            {
                AgentState.VERIFY_GOAL,
                AgentState.EXECUTE,
                AgentState.PLAN,
                AgentState.RECOVER,
                AgentState.REPLAN,
                AgentState.FAILED,
                AgentState.STUCK,
            }
        ),
        AgentState.VERIFY_GOAL: frozenset(
            {
                AgentState.SUCCESS,
                AgentState.REPLAN,
                AgentState.RECOVER,
                AgentState.FAILED,
                AgentState.STUCK,
            }
        ),
        AgentState.NEED_CONFIRMATION: frozenset(
            {AgentState.EXECUTE, AgentState.PLAN, AgentState.FAILED, AgentState.STUCK}
        ),
        AgentState.RECOVER: frozenset(
            {
                AgentState.OBSERVE,
                AgentState.PLAN,
                AgentState.REPLAN,
                AgentState.FAILED,
                AgentState.STUCK,
            }
        ),
        AgentState.REPLAN: frozenset(
            {AgentState.PLAN, AgentState.OBSERVE, AgentState.FAILED, AgentState.STUCK}
        ),
        AgentState.SUCCESS: frozenset(),
        AgentState.FAILED: frozenset(),
        AgentState.STUCK: frozenset(),
    }

    def __init__(
        self,
        context: AgentContext,
        limits: FSMLimits | None = None,
    ) -> None:
        self.context = context
        self.limits = limits or FSMLimits()
        self.context.validate()

    @property
    def state(self) -> AgentState:
        return self.context.state

    def can_transition(self, target: AgentState) -> bool:
        if not isinstance(target, AgentState):
            raise TypeError("target must be an AgentState")
        return target in self._TRANSITIONS[self.context.state]

    def transition(self, target: AgentState) -> AgentState:
        if not isinstance(target, AgentState):
            raise TypeError("target must be an AgentState")

        current = self.context.state

        if target not in self._TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"Invalid transition: {current.value} -> {target.value}"
            )

        if target == AgentState.RECOVER:
            self.register_recovery()

        if current == AgentState.RECOVER and target == AgentState.OBSERVE:
            if self.context.recovery_count >= self.limits.max_recoveries:
                self.context.state = AgentState.STUCK
                self.context.failure_reason = "Maximum recovery limit exceeded"
                raise StateLimitExceededError(self.context.failure_reason)

        if target == AgentState.REPLAN:
            self.register_replan()

        if target not in {
            AgentState.SUCCESS,
            AgentState.FAILED,
            AgentState.STUCK,
        }:
            if self.context.step >= self.limits.max_steps:
                self.context.state = AgentState.STUCK
                self.context.failure_reason = "Maximum step limit exceeded"
                raise StateLimitExceededError(self.context.failure_reason)

        self.context.state = target
        self.context.step += 1

        if self.context.step > self.limits.max_steps:
            self.context.state = AgentState.STUCK
            self.context.failure_reason = "Maximum step limit exceeded"
            raise StateLimitExceededError(self.context.failure_reason)

        return target

    def register_action(self) -> None:
        if self.context.action_count >= self.limits.max_actions:
            self.context.state = AgentState.STUCK
            self.context.failure_reason = "Maximum action limit exceeded"
            raise StateLimitExceededError(self.context.failure_reason)

        self.context.action_count += 1

    def register_recovery(self) -> None:
        if self.context.recovery_count >= self.limits.max_recoveries:
            self.context.state = AgentState.STUCK
            self.context.failure_reason = "Maximum recovery limit exceeded"
            raise StateLimitExceededError(self.context.failure_reason)

        self.context.recovery_count += 1

    def register_replan(self) -> None:
        if self.context.replan_count >= self.limits.max_replans:
            self.context.state = AgentState.STUCK
            self.context.failure_reason = "Maximum replan limit exceeded"
            raise StateLimitExceededError(self.context.failure_reason)

        self.context.replan_count += 1
