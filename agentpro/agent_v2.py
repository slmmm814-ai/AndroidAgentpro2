"""Autonomous agent (v2) — the full self-contained estimate → act → verify loop.

Reuses the battle-tested pieces of the project — :class:`AgentFSM` (step/action/
recovery/replan limits), :class:`TraceRecorder`, and the owner-confirmation
authorization service — and adds the stronger autonomy stack:

    OBSERVE → UNDERSTAND → PLAN → ACT → VERIFY-ACTION
           → PROGRESS CHECK → CONTINUE / REPLAN / RECOVER
           → VERIFY-GOAL → SUCCESS / FAILED / STUCK

On-device verification is evidence-based: action verification uses screen
deltas, progress verification uses repetition/failure heuristics, and goal
verification only consults the model when the planner signals completion.
Hard budgets (steps, actions, model calls, wall time) and the kill switch are
enforced on every iteration, so the loop can never spin forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent_runner import RecordingBridgeClient  # noqa: F401  (re-export)
from .authorization import (
    AuthorizationDecision,
    AuthorizationService,
    DangerousActionPolicy,
    OwnerConfirmation,
)
from .bridge_tools import (
    ToolContext,
    ToolRegistry,
    build_default_registry,
    tool_result_error_summary,
)
from .budgets import (
    BudgetExceededError,
    BudgetLimits,
    BudgetTracker,
    KillSwitch,
    KillSwitchEngaged,
    LoopGuard,
    signature_for,
)
from .fsm import AgentFSM, FSMLimits, StateLimitExceededError
from .fast_driver import FastDriver
from .llm_planner import LLMClient
from .memory import MemoryStore
from .models import (
    ActionResult,
    ActionType,
    AgentAction,
    AgentContext,
    AgentState,
    GoalResult,
    Observation,
)
from .planner_v2 import PlanDecision, SubgoalPlanner
from .screen import ScreenReadError, ScreenReader, ScreenSnapshot
from .trace import TraceRecorder
from .verification import (
    ActionVerification,
    ActionVerifier,
    GoalVerifier,
    LLMGoalVerifier,
    ProgressState,
    ProgressVerifier,
)


@dataclass(frozen=True)
class V2Limits:
    max_steps: int = 400
    max_actions: int = 80
    max_recoveries: int = 6
    max_replans: int = 6
    max_model_calls: int = 150
    max_wall_seconds: float | None = None
    repeat_threshold: int = 4

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_actions", "max_recoveries", "max_replans", "max_model_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int")
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.max_wall_seconds is not None and (
            isinstance(self.max_wall_seconds, bool)
            or not isinstance(self.max_wall_seconds, (int, float))
        ):
            raise TypeError("max_wall_seconds must be numeric or None")
        if self.max_wall_seconds is not None and self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be > 0")


def _gesture_key(tool: str, args: Mapping[str, Any]) -> str:
    """Identifies one specific gesture, so a different swipe gets a fresh budget."""
    parts = ",".join(
        f"{key}={args[key]}"
        for key in sorted(args)
        if isinstance(args[key], (int, float, str, bool))
    )
    return f"{tool}({parts})"


MEASURED_GESTURE_STALL_LIMIT = 3

"""Consecutive measured-useless gestures of the same kind before giving up.

Three is not a guess about screens; it is the smallest number that separates a
gesture that has genuinely stopped responding from a list that needs a moment,
while still ending far sooner than the sixteen swipes that used to run until
the scrollable node was gone.
"""

_GESTURE_TOOLS = frozenset({"swipe", "scroll"})


@dataclass
class AgentReport:
    success: bool
    reason: str
    steps: int = 0
    actions: int = 0
    model_calls: int = 0
    recoveries: int = 0
    replans: int = 0
    failures: int = 0
    subgoals: list[str] = field(default_factory=list)
    trace_path: str | None = None
    elapsed_s: float = 0.0
    goal_evidence: list[str] = field(default_factory=list)
    active_subgoal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "reason": self.reason,
            "steps": self.steps,
            "actions": self.actions,
            "model_calls": self.model_calls,
            "recoveries": self.recoveries,
            "replans": self.replans,
            "failures": self.failures,
            "subgoals": list(self.subgoals),
            "trace_path": self.trace_path,
            "elapsed_s": round(self.elapsed_s, 3),
            "goal_evidence": list(self.goal_evidence),
            "active_subgoal": self.active_subgoal,
        }


class MeasuredGestureStall:
    """Stops a gesture that is *measured* to have done nothing, repeatedly.

    The recovery budget alone is not enough: each recovery presses Back, which
    changes the screen, which resets the repetition signal that would otherwise
    have caught the stall. That is how a dead list kept being swiped until the
    scrollable node disappeared from the tree.

    Only a measurement counts. ``moved=False`` means pixels were read and the
    screen did not change, so repeating the same gesture is provably pointless.
    ``moved=None`` means the hash could not be read, and an unmeasured silence
    never counts towards the limit.
    """

    def __init__(self, limit: int = MEASURED_GESTURE_STALL_LIMIT) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._key: str | None = None
        self._count = 0

    def record(
        self, key: str, *, measured_no_effect: bool
    ) -> int | None:
        """Update the counter, returning the streak when the limit is reached."""
        if not measured_no_effect:
            self.reset()
            return None
        if key != self._key:
            self._key = key
            self._count = 0
        self._count += 1
        return self._count if self._count >= self._limit else None

    def reset(self) -> None:
        self._key = None
        self._count = 0

    @property
    def count(self) -> int:
        return self._count


class SmartRecovery:
    """Rule-based recovery: back first; escalate to home when repeated."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def recover(
        self,
        context: AgentContext,
        memory: MemoryStore,
        snapshot: ScreenSnapshot | None,
    ) -> bool:
        del snapshot  # fingerprint-based escalation handled by the loop
        performed: list[str] = []

        try:
            self._client.back()
            performed.append("back")
        except Exception:
            pass

        memory.record_episode(
            context.step, "recovery", "back pressed during recovery"
        )

        if context.recovery_count >= 3:
            try:
                self._client.key_event("home")
                performed.append("home")
                memory.record_episode(
                    context.step, "recovery", "home pressed"
                )
            except Exception:
                pass

        return True


class AutonomousAgent:
    """The v2 autonomy loop. Injectable components make it fully testable."""

    def __init__(
        self,
        client: Any,
        llm_client: LLMClient,
        *,
        model: str | None = None,
        trace_path: str | Path | None = None,
        limits: V2Limits = V2Limits(),
        include_screenshot: bool = True,
        registry: ToolRegistry | None = None,
        memory: MemoryStore | None = None,
        planner: SubgoalPlanner | None = None,
        goal_verifier: GoalVerifier | None = None,
        screen_reader: ScreenReader | None = None,
        action_verifier: ActionVerifier | None = None,
        progress_verifier: ProgressVerifier | None = None,
        kill_switch: KillSwitch | None = None,
        authorization_service: AuthorizationService | None = None,
        owner_confirmation_handler: Callable[[AgentAction, str], OwnerConfirmation | None] | None = None,
        confirmation_handler: Callable[[AgentAction], bool] | None = None,
        model_manager: Any = None,
        use_mcp: bool = True,
    ) -> None:
        if not hasattr(client, "ui_dump") or not hasattr(client, "tap"):
            raise TypeError("client must implement the bridge surface")
        if not hasattr(llm_client, "complete"):
            raise TypeError("llm_client must implement complete()")

        self._client = client
        self._llm_client = llm_client
        self._model = model
        self._limits = limits
        self._trace_path = (
            Path(trace_path)
            if trace_path
            else Path.home() / ".agentpro" / "trace_v2.jsonl"
        )

        from .model_manager import ModelManager

        self._mm = model_manager or ModelManager(
            llm_client,
            retries=2,
            timeout=30.0,
        )

        self.registry = registry or build_default_registry()
        self.mcp: Any = None
        if registry is None and use_mcp:
            from .mcp_tools import attach_mcp_from_env

            self.mcp = attach_mcp_from_env(self.registry)
        self.memory = memory or MemoryStore()
        screen = screen_reader or ScreenReader(
            client,
            include_screenshot=include_screenshot,
        )
        self.screen_reader = screen
        self.tool_context = ToolContext(
            client=client,
            screen_reader=screen,
            fast=FastDriver(client, screen_reader=screen),
        )
        self.planner = planner or SubgoalPlanner(
            self._mm,
            self.registry,
            self.memory,
        )
        self.goal_verifier = goal_verifier or LLMGoalVerifier(self._mm)
        self.action_verifier = action_verifier or ActionVerifier()
        loop_guard = LoopGuard(repeat_threshold=limits.repeat_threshold)
        self.loop_guard = loop_guard
        self.progress_verifier = progress_verifier or ProgressVerifier(
            loop_guard,
            repeat_threshold=limits.repeat_threshold,
        )
        self.kill_switch = kill_switch or KillSwitch(
            kill_file=str(Path.home() / ".agentpro" / "KILL"),
            env_var="AGENTPRO_KILL",
        )
        self.recovery = SmartRecovery(client)
        self._gesture_stall = MeasuredGestureStall()
        self.authorization_service = authorization_service
        self.owner_confirmation_handler = owner_confirmation_handler
        self.confirmation_handler = confirmation_handler or (lambda _action: False)

        self._trace: TraceRecorder | None = None

    # -- internals --------------------------------------------------------

    def _ensure_trace(self) -> TraceRecorder:
        if self._trace is None:
            self._trace = TraceRecorder(self._trace_path)
        return self._trace

    def _t(self, event: str, **payload: Any) -> None:
        trace = self._ensure_trace()
        trace.record(
            event=event,
            state=getattr(self.context, "state", AgentState.IDLE).value,
            step=getattr(self.context, "step", 0),
            payload=payload,
        )

    def _observe(self) -> ScreenSnapshot:
        snapshot = self.screen_reader.observe()
        if not snapshot.success:
            raise ScreenReadError(
                snapshot.error_message or snapshot.error_code or "observe failed"
            )
        self.context.observation = snapshot.observation
        self.context.current_fingerprint = snapshot.fingerprint
        return snapshot

    def _fail(self, reason: str) -> AgentReport:
        self.context.failure_reason = reason
        try:
            if self.context.state not in {
                AgentState.SUCCESS,
                AgentState.FAILED,
                AgentState.STUCK,
            }:
                self._fsm.transition(AgentState.FAILED)
        except Exception:
            self.context.state = AgentState.FAILED
        self._t("v2_failed", reason=reason)
        return self._build_report(False, reason)

    def _build_report(self, success: bool, reason: str) -> AgentReport:
        return AgentReport(
            success=success,
            reason=reason,
            steps=self.context.step,
            actions=self.context.action_count,
            model_calls=self._mm.call_count,
            recoveries=self.context.recovery_count,
            replans=self.context.replan_count,
            failures=len(self.memory.failures),
            subgoals=list(self.memory.subgoal_list),
            trace_path=str(self._trace_path),
            elapsed_s=round(self._budget.elapsed, 3),
            goal_evidence=list(self.memory.goal_reached_evidence),
            active_subgoal=self.memory.active_subgoal,
        )

    def _authorize_dangerous(self, action: AgentAction) -> bool:
        if not DangerousActionPolicy.is_dangerous(action):
            return True

        service = self.authorization_service
        handler = self.owner_confirmation_handler
        if service is None or handler is None:
            self._t(
                "v2_dangerous_denied",
                action=action.action_type.value,
                reason="no authorization service",
            )
            return False

        try:
            request = service.create_confirmation_request(action)
            if callable(handler):
                confirmation = handler(action, request.request_id)
            else:
                confirmation = handler.confirm(action, request.request_id)
            result = service.authorize(action, confirmation)
            self._t(
                "v2_dangerous_decision",
                action=action.action_type.value,
                decision=result.decision.value,
            )
            return result.decision is AuthorizationDecision.GRANTED
        except Exception as exc:
            self._t(
                "v2_dangerous_error",
                action=action.action_type.value,
                error=str(exc),
            )
            return False

    def _confirm_action(self, action: AgentAction) -> bool:
        try:
            return bool(self.confirmation_handler(action))
        except Exception:
            return False

    def _recover(self, reason: str, snapshot: ScreenSnapshot | None) -> None:
        self._t("v2_recover_started", reason=reason)
        self._fsm.transition(AgentState.RECOVER)
        self.recovery.recover(self.context, self.memory, snapshot)
        self._t("v2_recovered")
        self._fsm.transition(AgentState.OBSERVE)

    def _sync_model_budget(self) -> None:
        self._budget.model_calls = self._mm.call_count
        self._budget.ensure()

    # -- main loop --------------------------------------------------------

    def run(self, goal: str) -> AgentReport:
        context = AgentContext(goal=goal)
        self.context = context
        limits = FSMLimits(
            max_steps=self._limits.max_steps,
            max_actions=self._limits.max_actions,
            max_recoveries=self._limits.max_recoveries,
            max_replans=self._limits.max_replans,
        )
        self._fsm = AgentFSM(context, limits)
        budget_limits = BudgetLimits(
            max_steps=self._limits.max_steps,
            max_actions=self._limits.max_actions,
            max_model_calls=self._limits.max_model_calls,
            max_wall_seconds=self._limits.max_wall_seconds,
        )
        self._budget = BudgetTracker(budget_limits)
        self._budget.start()

        self._t("v2_execution_started", goal=goal)

        try:
            self._fsm.transition(AgentState.DECOMPOSE)
            self.planner.ensure_subgoals(goal)
            self._t(
                "v2_decomposed",
                subgoals=self.memory.subgoal_list,
            )
            self._fsm.transition(AgentState.OBSERVE)

            snapshot = self._observe()
            last_tool_result: str | None = None

            while True:
                self._budget.mark_step()
                if self.kill_switch.is_engaged():
                    raise KillSwitchEngaged("kill switch engaged")

                self._t(
                    "v2_observe",
                    success=snapshot.success,
                    package=snapshot.package_name,
                    fingerprint=snapshot.fingerprint,
                )

                progress = self.progress_verifier.check(self.memory)
                if progress in {
                    ProgressState.REPEATING,
                    ProgressState.STALLED,
                }:
                    self._recover(
                        f"progress state {progress.value}",
                        snapshot,
                    )
                    snapshot = self._observe()
                    continue

                self._fsm.transition(AgentState.PLAN)
                decision: PlanDecision = self.planner.decide(
                    context,
                    snapshot,
                    last_tool_result=last_tool_result,
                )
                self._sync_model_budget()

                self._t(
                    "v2_plan",
                    tool=decision.tool,
                    goal_done=decision.goal_done,
                    replan=decision.replan,
                    thought=decision.thought,
                    confidence=decision.confidence,
                )

                if decision.replan:
                    self._t("v2_replan_subgoals")
                    self._fsm.transition(AgentState.REPLAN)
                    self.planner.replan(goal)
                    self._fsm.transition(AgentState.OBSERVE)
                    snapshot = self._observe()
                    continue

                if decision.is_finish:
                    self._fsm.transition(AgentState.VERIFY_GOAL)
                    goal_result = self.goal_verifier.verify(
                        goal=goal,
                        snapshot=snapshot,
                        memory=self.memory,
                    )
                    self._sync_model_budget()
                    self._t(
                        "v2_goal_verified",
                        success=goal_result.success,
                        reason=goal_result.reason,
                    )
                    if goal_result.success:
                        self._fsm.transition(AgentState.SUCCESS)
                        self._t("v2_success", evidence=self.memory.goal_reached_evidence)
                        return self._build_report(True, goal_result.reason)

                    self.memory.record_failure(
                        context.step,
                        action_line="finish (goal check)",
                        error_code="GOAL_NOT_VERIFIED",
                        detail=goal_result.reason,
                        recovered_by="replan",
                    )
                    self._fsm.transition(AgentState.REPLAN)
                    self.planner.replan(goal)
                    self._fsm.transition(AgentState.OBSERVE)
                    snapshot = self._observe()
                    continue

                # --- act -------------------------------------------------
                assert decision.tool is not None
                tool = self.registry.get(decision.tool)
                if tool is None:
                    context.failure_reason = f"planner chose unknown tool {decision.tool}"
                    self._fsm.transition(AgentState.FAILED)
                    self._t("v2_unknown_tool", tool=decision.tool)
                    return self._build_report(False, context.failure_reason)

                action = AgentAction(tool.to_action_type(), dict(decision.args))

                self._fsm.transition(AgentState.EXECUTE)

                if DangerousActionPolicy.is_dangerous(action) or action.requires_confirmation:
                    self._fsm.transition(AgentState.NEED_CONFIRMATION)
                    if DangerousActionPolicy.is_dangerous(action):
                        if not self._authorize_dangerous(action):
                            return self._fail("dangerous action authorization denied")
                    elif not self._confirm_action(action):
                        return self._fail("action confirmation denied")
                    self._t("v2_action_confirmed", action=action.action_type.value)
                    self._fsm.transition(AgentState.EXECUTE)

                self._fsm.register_action()
                self.tool_context.snapshot = snapshot
                visual_before = self.screen_reader.capture_visual()
                result = self.registry.execute(
                    decision.tool,
                    self.tool_context,
                    dict(decision.args),
                )
                self.context.last_action = action
                self.context.last_action_result = ActionResult(
                    success=result.success,
                    data=result.data,
                    error_code=result.error_code,
                    error_message=result.error_message,
                )
                self._fsm.transition(AgentState.VERIFY_ACTION)

                signature = signature_for(
                    {"tool": decision.tool, "args": dict(decision.args)},
                    snapshot.fingerprint,
                )
                self.progress_verifier.record_signature(signature)
                self.memory.record_action(
                    context.step,
                    action_line=f"{decision.tool} {dict(decision.args)}",
                    signature=signature,
                    result_line=tool_result_error_summary(result),
                )

                self._t(
                    "v2_executed",
                    tool=decision.tool,
                    success=result.success,
                    error_code=result.error_code,
                )

                # --- verify action ----------------------------------------
                try:
                    after = self._observe()
                except ScreenReadError:
                    self.memory.record_failure(
                        context.step,
                        action_line=decision.tool,
                        error_code="VERIFY_OBSERVE_FAILED",
                        detail="screen unreadable after action",
                        recovered_by="back",
                    )
                    self._recover("screen unreadable after action", snapshot)
                    snapshot = self._observe()
                    continue

                change = self.screen_reader.measure_change(
                    snapshot,
                    after,
                    previous_visual=visual_before,
                )
                verification = self.action_verifier.verify(
                    decision.tool,
                    dict(decision.args),
                    result.success,
                    result.error_code,
                    snapshot,
                    after,
                    change,
                )
                self._t(
                    "v2_action_verified",
                    tool=decision.tool,
                    result=verification.value,
                    moved=change.moved,
                    distance=change.distance,
                )

                if verification is ActionVerification.FAILED:
                    self.memory.record_failure(
                        context.step,
                        action_line=decision.tool,
                        error_code=result.error_code or "ACTION_FAILED",
                        detail=result.error_message or "tool execution failed",
                        recovered_by="back",
                    )

                    streak = self._gesture_stall.record(
                        _gesture_key(decision.tool, decision.args),
                        measured_no_effect=(
                            decision.tool in _GESTURE_TOOLS
                            and result.success
                            and change.moved is False
                        ),
                    )
                    if streak is not None:
                        reason = (
                            f"{decision.tool} did not move the screen "
                            f"{streak} times in a row; the target is not "
                            f"responding to gestures"
                        )
                        self.memory.record_failure(
                            context.step,
                            action_line=decision.tool,
                            error_code="GESTURE_NO_EFFECT",
                            detail=reason,
                            recovered_by="stop",
                        )
                        self._t(
                            "v2_gesture_stalled",
                            tool=decision.tool,
                            streak=streak,
                        )
                        return self._fail(reason)

                    self._recover("action failed", after)
                    snapshot = self._observe()
                    continue

                if verification in {
                    ActionVerification.NO_OP,
                    ActionVerification.UNKNOWN,
                }:
                    self.memory.record_episode(
                        context.step,
                        "action_missed",
                        f"{decision.tool} had no observable effect",
                    )
                    if (
                        self.progress_verifier.check(self.memory)
                        is ProgressState.REPEATING
                    ):
                        self._recover("repeated no-op", after)
                        snapshot = self._observe()
                        continue

                snapshot = after

                if decision.subgoal_done:
                    self.planner.advance_subgoal()
                    self._t(
                        "v2_subgoal_done",
                        subgoal=self.memory.active_subgoal,
                    )

                last_tool_result = tool_result_error_summary(result)
                if verification is ActionVerification.PARTIAL:
                    self.memory.record_episode(
                        context.step,
                        "action_partial",
                        f"{decision.tool} only partially verified",
                    )

        except (BudgetExceededError, KillSwitchEngaged, StateLimitExceededError) as exc:
            return self._fail(str(exc))
        except ScreenReadError as exc:
            return self._fail(f"screen unreadable: {exc}")
        except Exception as exc:  # noqa: BLE001 - loop must terminate
            self._t("v2_fatal_error", error=str(exc))
            return self._fail(f"autonomous agent error: {exc}")

        return self._fail("autonomy loop ended without a terminal result")


def build_v2_runner(
    client: Any,
    llm_client: LLMClient,
    **kwargs: Any,
) -> AutonomousAgent:
    """Factory mirroring :func:`agentpro.agent_runner.build_runner`."""
    return AutonomousAgent(client, llm_client, **kwargs)