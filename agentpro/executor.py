from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .authorization import (
    AuthorizationDecision,
    AuthorizationService,
    DangerousActionPolicy,
    OwnerConfirmation,
)
from .fsm import AgentFSM, StateLimitExceededError
from .models import (
    ActionResult,
    AgentAction,
    AgentContext,
    AgentState,
    GoalResult,
    Observation,
)
from .trace import TraceRecorder


class Observer(Protocol):
    def observe(self) -> Observation:
        ...


class Planner(Protocol):
    def plan(self, context: AgentContext) -> list[AgentAction]:
        ...


class ActionExecutor(Protocol):
    def execute(self, action: AgentAction) -> ActionResult:
        ...


class GoalVerifier(Protocol):
    def verify(
        self,
        context: AgentContext,
        observation: Observation | None,
    ) -> GoalResult:
        ...


class RecoveryHandler(Protocol):
    def recover(self, context: AgentContext) -> bool:
        ...


class ConfirmationHandler(Protocol):
    def confirm(self, action: AgentAction) -> bool:
        ...


class OwnerConfirmationHandler(Protocol):
    def confirm(
        self,
        action: AgentAction,
        request_id: str,
    ) -> OwnerConfirmation | None:
        ...


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    reason: str


class AgentExecutor:
    def __init__(
        self,
        context: AgentContext,
        fsm: AgentFSM,
        trace: TraceRecorder,
        observer: Observer | Callable[[], Observation],
        planner: Planner | Callable[[AgentContext], list[AgentAction]],
        action_executor: ActionExecutor | Callable[[AgentAction], ActionResult],
        goal_verifier: GoalVerifier
        | Callable[[AgentContext, Observation | None], GoalResult],
        recovery_handler: RecoveryHandler | Callable[[AgentContext], bool],
        confirmation_handler: ConfirmationHandler
        | Callable[[AgentAction], bool]
        | None = None,
        authorization_service: AuthorizationService | None = None,
        owner_confirmation_handler: OwnerConfirmationHandler
        | Callable[[AgentAction, str], OwnerConfirmation | None]
        | None = None,
    ) -> None:
        self.context = context
        self.fsm = fsm
        self.trace = trace
        self.observer = observer
        self.planner = planner
        self.action_executor = action_executor
        self.goal_verifier = goal_verifier
        self.recovery_handler = recovery_handler
        self.confirmation_handler = confirmation_handler or (lambda _action: False)
        self.authorization_service = authorization_service
        self.owner_confirmation_handler = owner_confirmation_handler

    def _trace(self, event: str, **payload: object) -> None:
        self.trace.record(
            event=event,
            state=self.context.state.value,
            step=self.context.step,
            payload=payload,
        )

    def _transition(self, target: AgentState) -> None:
        self.fsm.transition(target)
        self._trace("state_transition", target=target.value)

    def _call_observer(self) -> Observation:
        if callable(self.observer):
            return self.observer()
        return self.observer.observe()

    def _call_planner(self) -> list[AgentAction]:
        if callable(self.planner):
            return list(self.planner(self.context))
        return list(self.planner.plan(self.context))

    def _call_action_executor(self, action: AgentAction) -> ActionResult:
        if callable(self.action_executor):
            return self.action_executor(action)
        return self.action_executor.execute(action)

    def _call_goal_verifier(self) -> GoalResult:
        if callable(self.goal_verifier):
            return self.goal_verifier(
                self.context,
                self.context.observation,
            )
        return self.goal_verifier.verify(
            self.context,
            self.context.observation,
        )

    def _call_recovery(self) -> bool:
        if callable(self.recovery_handler):
            return bool(self.recovery_handler(self.context))
        return bool(self.recovery_handler.recover(self.context))

    def _confirm(self, action: AgentAction) -> bool:
        if callable(self.confirmation_handler):
            return bool(self.confirmation_handler(action))
        return bool(self.confirmation_handler.confirm(action))

    def _authorize_dangerous_action(self, action: AgentAction) -> bool:
        if not DangerousActionPolicy.is_dangerous(action):
            return True

        self._trace(
            "dangerous_action_authorization_started",
            action_type=action.action_type.value,
        )

        service = self.authorization_service
        handler = self.owner_confirmation_handler

        if service is None or handler is None:
            self._trace(
                "dangerous_action_authorization_denied",
                action_type=action.action_type.value,
                reason="authorization_service_or_owner_handler_unavailable",
            )
            return False

        try:
            request = service.create_confirmation_request(action)

            if callable(handler):
                confirmation = handler(action, request.request_id)
            else:
                confirmation = handler.confirm(action, request.request_id)

            result = service.authorize(action, confirmation)

            self._trace(
                "dangerous_action_authorization_result",
                action_type=action.action_type.value,
                decision=result.decision.value,
            )

            return result.decision is AuthorizationDecision.GRANTED

        except Exception as exc:
            self._trace(
                "dangerous_action_authorization_error",
                action_type=action.action_type.value,
                error_type=type(exc).__name__,
            )
            return False

    def _fail(self, reason: str) -> ExecutionResult:
        self.context.failure_reason = reason

        if self.context.state not in {
            AgentState.SUCCESS,
            AgentState.FAILED,
            AgentState.STUCK,
        }:
            try:
                self._transition(AgentState.FAILED)
            except Exception:
                self.context.state = AgentState.FAILED

        self._trace("failed", reason=reason)
        return ExecutionResult(False, reason)

    def _recover(self) -> bool:
        try:
            self._transition(AgentState.RECOVER)
            self._trace("recovery_started")

            recovered = self._call_recovery()

            self._trace(
                "recovery_finished",
                recovered=recovered,
            )
            return recovered

        except StateLimitExceededError as exc:
            self.context.failure_reason = str(exc)
            self._trace("recovery_limit_exceeded", reason=str(exc))
            return False
        except Exception as exc:
            self._trace("recovery_error", error=str(exc))
            return False

    def run(self) -> ExecutionResult:
        self.context.validate()
        self._trace("execution_started", goal=self.context.goal)

        try:
            self._transition(AgentState.DECOMPOSE)
            self._trace("goal_decomposed")

            self._transition(AgentState.OBSERVE)

            while True:
                try:
                    self.context.observation = self._call_observer()

                    if not isinstance(self.context.observation, Observation):
                        raise TypeError("observer must return Observation")

                    self._trace(
                        "observation_completed",
                        success=self.context.observation.success,
                        package_name=self.context.observation.package_name,
                    )

                except Exception as exc:
                    self._trace("observation_error", error=str(exc))

                    if self._recover():
                        self._transition(AgentState.OBSERVE)
                        continue

                    return self._fail(f"Observation failed: {exc}")

                self._transition(AgentState.PLAN)

                try:
                    self.context.plan = self._call_planner()

                    self._trace(
                        "plan_created",
                        action_count=len(self.context.plan),
                    )

                except Exception as exc:
                    self._trace("planner_error", error=str(exc))

                    if self._recover():
                        self._transition(AgentState.OBSERVE)
                        continue

                    return self._fail(f"Planning failed: {exc}")

                if not self.context.plan:
                    verification = self._call_goal_verifier()

                    self._trace(
                        "goal_verified",
                        success=verification.success,
                        reason=verification.reason,
                    )

                    if verification.success:
                        self._transition(AgentState.VERIFY_GOAL)
                        self._transition(AgentState.SUCCESS)
                        return ExecutionResult(True, verification.reason)

                    try:
                        self._transition(AgentState.REPLAN)
                        self._transition(AgentState.OBSERVE)
                    except StateLimitExceededError as exc:
                        return self._fail(str(exc))

                    continue

                restart_observation = False

                for action in self.context.plan:
                    self.context.last_action = action

                    if DangerousActionPolicy.is_dangerous(action):
                        self._transition(AgentState.NEED_CONFIRMATION)

                        if not self._authorize_dangerous_action(action):
                            return self._fail("Dangerous action authorization denied")

                        self._transition(AgentState.EXECUTE)
                    elif action.requires_confirmation:
                        self._transition(AgentState.NEED_CONFIRMATION)

                        if not self._confirm(action):
                            return self._fail("Action confirmation denied")

                        self._trace("action_confirmed")
                        self._transition(AgentState.EXECUTE)
                    else:
                        self._transition(AgentState.EXECUTE)

                    try:
                        self.fsm.register_action()
                        result = self._call_action_executor(action)

                        if not isinstance(result, ActionResult):
                            raise TypeError(
                                "action executor must return ActionResult"
                            )

                        self.context.last_action_result = result

                        self._trace(
                            "action_executed",
                            action=action.action_type.value,
                            success=result.success,
                        )

                    except StateLimitExceededError as exc:
                        return self._fail(str(exc))
                    except Exception as exc:
                        self.context.last_action_result = ActionResult(
                            success=False,
                            error_code="EXECUTION_EXCEPTION",
                            error_message=str(exc),
                        )
                        self._trace("action_error", error=str(exc))

                        if self._recover():
                            self._transition(AgentState.OBSERVE)
                            restart_observation = True
                            break

                        return self._fail(f"Action execution failed: {exc}")

                    self._transition(AgentState.VERIFY_ACTION)

                    if not result.success:
                        self._trace(
                            "action_failed_replan_requested",
                            error_code=result.error_code,
                            error_message=result.error_message,
                        )
                        try:
                            self._transition(AgentState.REPLAN)
                            self._trace(
                                "replan_requested",
                                reason=result.error_message or "Action execution failed",
                            )
                            self._transition(AgentState.OBSERVE)
                        except StateLimitExceededError as exc:
                            return self._fail(str(exc))

                        restart_observation = True
                        break

                    verification = self._call_goal_verifier()

                    self._trace(
                        "action_verified",
                        success=verification.success,
                        reason=verification.reason,
                    )

                    if not verification.success:
                        self._trace(
                            "plan_continuation",
                            remaining_actions=len(self.context.plan)
                            - self.context.action_count,
                        )
                        continue

                    if verification.success:
                        self._transition(AgentState.VERIFY_GOAL)

                        final = self._call_goal_verifier()

                        self._trace(
                            "goal_verified",
                            success=final.success,
                            reason=final.reason,
                        )

                        if final.success:
                            self._transition(AgentState.SUCCESS)
                            return ExecutionResult(True, final.reason)

                    try:
                        self._transition(AgentState.REPLAN)
                        self._transition(AgentState.OBSERVE)
                    except StateLimitExceededError as exc:
                        return self._fail(str(exc))

                    restart_observation = True
                    break

                if restart_observation:
                    if self.context.state == AgentState.PLAN:
                        self._transition(AgentState.OBSERVE)
                    continue

        except StateLimitExceededError as exc:
            return self._fail(str(exc))
        except Exception as exc:
            self._trace("fatal_error", error=str(exc))
            return self._fail(f"Executor failed: {exc}")

        return self._fail("Execution ended without a terminal result")
