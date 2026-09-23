"""Verification layer: action / progress / goal.

The three levels are deliberately separate:

* :class:`ActionVerifier`  — did the executed action have the intended effect?
* :class:`ProgressVerifier` — is the agent still making progress?
* :class:`GoalVerifier`     — is the original goal actually achieved?

The goal verifier is LLM-assisted and only consults the model when the agent
explicitly signals completion, keeping model calls cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from .budgets import LoopGuard
from .llm_planner import LLMClient
from .memory import MemoryStore
from .models import GoalResult
from .screen import ScreenSnapshot


class ActionVerification(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    NO_OP = "NO_OP"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


_NAV_TOOLS = frozenset(
    {"back", "home", "recents", "open_url", "open_app", "key_event"}
)
_READ_TOOLS = frozenset(
    {"dump_ui", "screenshot", "get_window_info", "wait", "wait_for_text", "wait_for_screen_stable"}
)


def _editable_texts(snapshot: ScreenSnapshot | None) -> list[str]:
    """Sorted editable-field contents for deterministic text checks."""
    if snapshot is None:
        return []
    return [
        (el.text or "")
        for el in sorted(
            snapshot.find(editable=True),
            key=lambda el: (el.top, el.left, el.node_index),
        )
    ]


class ActionVerifier:
    """Deterministic, schema-free action verification via screen deltas."""

    def verify(
        self,
        tool: str,
        args: Mapping[str, Any],
        result_success: bool,
        result_error: str | None,
        before: ScreenSnapshot | None,
        after: ScreenSnapshot | None,
    ) -> ActionVerification:
        if not result_success:
            return ActionVerification.FAILED

        if tool in _READ_TOOLS:
            return ActionVerification.VERIFIED

        before_fp = before.fingerprint if before is not None else None
        after_fp = after.fingerprint if after is not None else None

        if tool in _NAV_TOOLS:
            if after_fp is not None and after_fp != before_fp:
                return ActionVerification.VERIFIED
            if after is not None and not after.success:
                return ActionVerification.UNKNOWN
            # Navigation that does not change the visible tree is suspicious.
            return ActionVerification.PARTIAL

        if tool in {"type_text", "clear_text", "erase_text"}:
            editables_before = (
                len(before.find(editable=True)) if before is not None else 0
            )
            editables_after = (
                len(after.find(editable=True)) if after is not None else 0
            )
            if editables_before == 0 and editables_after == 0:
                return ActionVerification.NO_OP

            texts_before = _editable_texts(before)
            texts_after = _editable_texts(after)
            total_before = sum(len(text) for text in texts_before)
            total_after = sum(len(text) for text in texts_after)

            if tool == "type_text":
                typed = args.get("text")
                if isinstance(typed, str) and typed and any(
                    typed in text for text in texts_after
                ):
                    return ActionVerification.VERIFIED
                if total_after > total_before:
                    return ActionVerification.VERIFIED
                return ActionVerification.PARTIAL

            # clear_text / erase_text: the targeted content must shrink
            # or disappear, not merely change.
            if total_after < total_before or (
                total_before > 0 and total_after == 0
            ):
                return ActionVerification.VERIFIED
            return ActionVerification.PARTIAL

        # tap / long_press / swipe / scroll: expect the tree to change at least
        # somewhere; otherwise the action likely missed its target.
        if after_fp is not None and after_fp != before_fp:
            return ActionVerification.VERIFIED
        return ActionVerification.UNKNOWN


class ProgressState(str, Enum):
    ADVANCING = "ADVANCING"
    DEGRADED = "DEGRADED"
    REPEATING = "REPEATING"
    STALLED = "STALLED"


class ProgressVerifier:
    """Detects stalls and pathological repetition."""

    def __init__(
        self,
        loop_guard: LoopGuard,
        *,
        repeat_threshold: int = 4,
        max_recent_failures: int = 5,
    ) -> None:
        if repeat_threshold < 1:
            raise ValueError("repeat_threshold must be >= 1")
        if max_recent_failures < 1:
            raise ValueError("max_recent_failures must be >= 1")
        self._loop_guard = loop_guard
        self._threshold = repeat_threshold
        self._max_recent_failures = max_recent_failures
        self._recent_failures: list[str] = []

    def record_signature(self, signature: str) -> None:
        self._loop_guard.record(signature)

    def record_failure(self, error_code: str) -> None:
        self._recent_failures.append(error_code)
        if len(self._recent_failures) > self._max_recent_failures:
            self._recent_failures = self._recent_failures[-(self._max_recent_failures):]

    def check(self, memory: MemoryStore) -> ProgressState:
        if self._loop_guard.is_repeating():
            return ProgressState.REPEATING

        if len(memory.failures) >= 3:
            recents = memory.failures[-3:]
            distinct = len({(r.error_code) for r in recents})
            if distinct == 1:
                return ProgressState.STALLED
            return ProgressState.DEGRADED

        return ProgressState.ADVANCING

    def reset(self) -> None:
        self._recent_failures = list(self._loop_guard.most_recent(0))


class GoalVerifier(Protocol):
    def verify(
        self,
        *,
        goal: str,
        snapshot: ScreenSnapshot | None,
        memory: MemoryStore,
    ) -> GoalResult:
        ...


_GOV_SYSTEM_PROMPT = """\
You are the goal-verification module of an Android automation agent. You are \
shown the user's goal and evidence collected so far (current screen summary, \
recent actions, subgoals). Decide whether the goal is FULLY achieved.

Respond with ONLY a JSON object with exactly:
{"done": <bool>, "evidence": "<one short factual sentence>", "confidence": <0..1>}

Set done=true ONLY when the goal's observable outcome is verified by the \
screen evidence. Do not claim success from intent or from a single "finish" \
token; success must be supported by the current screen state.
"""


class LLMGoalVerifier:
    """LLM-assisted goal verification, only consulted when the agent
    signals completion via the ``finish`` action."""

    def __init__(
        self,
        client: LLMClient,
        *,
        timeout: float = 30.0,
        model: str | None = None,
        confidence_threshold: float = 0.5,
    ) -> None:
        if not hasattr(client, "complete"):
            raise TypeError("client must implement complete()")
        if isinstance(confidence_threshold, bool) or not isinstance(
            confidence_threshold, (int, float)
        ):
            raise TypeError("confidence_threshold must be numeric")
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be in [0, 1]")
        self._client = client
        self._timeout = timeout
        self._model = model
        self._threshold = confidence_threshold

    def verify(
        self,
        *,
        goal: str,
        snapshot: ScreenSnapshot | None,
        memory: MemoryStore,
    ) -> GoalResult:
        screen_parts = []
        if snapshot is not None and snapshot.success:
            screen_parts.append(_snapshot_summary_for_goal(snapshot))
        else:
            screen_parts.append("(no readable screen)")

        user_text = (
            f"GOAL: {goal}\n\n"
            f"CURRENT SCREEN:\n{' '.join(screen_parts)}\n\n"
            f"AGENT HISTORY:\n{memory.build_context(goal)}\n"
        )

        from .model_manager import ModelError

        messages = [
            {"role": "system", "content": _GOV_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]

        try:
            raw = self._client.complete(messages, json_mode=True, timeout=self._timeout)
        except Exception as exc:
            return GoalResult(
                success=False,
                reason=f"goal verification model error: {exc}",
            )

        try:
            from .llm_planner import _extract_json

            obj = _extract_json(raw)
            done = bool(obj.get("done"))
            confidence = float(obj.get("confidence", 0.0))
        except Exception:
            return GoalResult(
                success=False,
                reason="goal verification response was unparseable",
            )

        if done and confidence >= self._threshold:
            evidence = str(obj.get("evidence", "goal verified"))
            memory.record_goal_evidence(evidence)
            return GoalResult(success=True, reason=evidence)
        if done:
            return GoalResult(
                success=False,
                reason=f"goal reported achieved but confidence too low ({confidence:.2f})",
            )
        return GoalResult(
            success=False,
            reason=str(obj.get("evidence", "goal not achieved")),
        )


def _snapshot_summary_for_goal(snapshot: ScreenSnapshot) -> str:
    try:
        from .screen import summarize_screen

        return summarize_screen(snapshot, max_chars=1200)
    except Exception:
        return "(screen summary unavailable)"


class DeterministicGoalVerifier:
    """Non-LLM fallback: succeed when the agent emits finish AND an
    explicit expected-outcome predicate is satisfied."""

    def __init__(self, predicate=None) -> None:
        self._predicate = predicate

    def verify(
        self,
        *,
        goal: str,
        snapshot: ScreenSnapshot | None,
        memory: MemoryStore,
    ) -> GoalResult:
        if self._predicate is not None:
            try:
                if self._predicate(snapshot):
                    return GoalResult(
                        success=True, reason="deterministic predicate satisfied"
                    )
                return GoalResult(
                    success=False, reason="deterministic predicate not satisfied"
                )
            except Exception as exc:
                return GoalResult(
                    success=False, reason=f"predicate error: {exc}"
                )
        return GoalResult(
            success=False, reason="deterministic goal verifier: no predicate set"
        )