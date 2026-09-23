"""Task memory for long-horizon autonomy.

* working memory: recent observations + actions + results (bounded ring)
* episodic memory: important events (screen transitions, goal milestones)
* failure history: what failed, why, and what was tried after
* task state: active subgoal, plan state, summary
* context selection + LLM-assisted summarization when history grows

Memory is deliberately small and inspectable so it can be traced and replayed.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


Summarizer = Callable[[str], str]


@dataclass
class MemoryEvent:
    step: int
    kind: str
    content: str
    at: float = field(default_factory=time.time)


@dataclass
class FailureRecord:
    step: int
    error_code: str
    detail: str
    action: str
    recovered_by: str = ""


class MemoryStore:
    """Thread-unsafe by design; owned by the autonomy loop."""

    def __init__(
        self,
        *,
        working_capacity: int = 40,
        episodic_capacity: int = 12,
        failure_capacity: int = 20,
        summarize_threshold: int = 30,
        summarizer: Summarizer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if working_capacity < 1:
            raise ValueError("working_capacity must be >= 1")
        if episodic_capacity < 1:
            raise ValueError("episodic_capacity must be >= 1")
        if failure_capacity < 1:
            raise ValueError("failure_capacity must be >= 1")
        if summarize_threshold < 1:
            raise ValueError("summarize_threshold must be >= 1")

        self._working_capacity = working_capacity
        self._episodic_capacity = episodic_capacity
        self._failure_capacity = failure_capacity
        self._summarize_threshold = summarize_threshold
        self._summarizer = summarizer
        self._clock = clock

        self._working: deque[str] = deque(maxlen=working_capacity)
        self._episodes: deque[MemoryEvent] = deque(maxlen=episodic_capacity)
        self._failures: deque[FailureRecord] = deque(maxlen=failure_capacity)
        self._signatures: deque[str] = deque(maxlen=working_capacity)

        self.active_subgoal: str | None = None
        self.subgoal_index: int = 0
        self.subgoal_count: int = 0
        self.subgoal_list: list[str] = []
        self.summary: str = ""
        self.goal_reached_evidence: list[str] = []

    # -- records ----------------------------------------------------------

    def record_action(
        self,
        step: int,
        action_line: str,
        signature: str | None,
        result_line: str,
    ) -> None:
        self._working.append(f"[{step}] {action_line} -> {result_line}")
        if signature is not None:
            self._signatures.append(signature)
        if len(self._working) >= self._summarize_threshold:
            self._maybe_summarize()

    def record_episode(self, step: int, kind: str, content: str) -> None:
        self._episodes.append(
            MemoryEvent(step=step, kind=kind, content=content, at=self._clock())
        )

    def record_failure(
        self,
        step: int,
        action_line: str,
        error_code: str,
        detail: str,
        recovered_by: str = "",
    ) -> None:
        self._failures.append(
            FailureRecord(
                step=step,
                error_code=error_code,
                detail=detail,
                action=action_line,
                recovered_by=recovered_by,
            )
        )
        self.record_episode(
            step, "failure", f"{error_code}: {detail or 'no detail'}"
        )

    def record_goal_evidence(self, evidence: str) -> None:
        self.goal_reached_evidence.append(evidence)

    def set_subgoal(self, index: int, total: int, text: str) -> None:
        self.subgoal_index = index
        self.subgoal_count = total
        self.active_subgoal = text

    # -- reads ------------------------------------------------------------

    @property
    def working(self) -> list[str]:
        return list(self._working)

    @property
    def episodes(self) -> list[MemoryEvent]:
        return list(self._episodes)

    @property
    def failures(self) -> list[FailureRecord]:
        return list(self._failures)

    def recent_signatures(self) -> list[str]:
        return list(self._signatures)

    def recent_working(self, limit: int = 8) -> list[str]:
        return list(self._working)[-limit:]

    def recent_episodes(self, limit: int = 4) -> list[str]:
        return [f"{e.kind}: {e.content}" for e in list(self._episodes)[-limit:]]

    def recent_failures(self, limit: int = 3) -> list[str]:
        out = []
        for rec in list(self._failures)[-limit:]:
            detail = rec.error_code
            if rec.detail:
                detail += f": {rec.detail}"
            if rec.recovered_by:
                detail += f" (handled by {rec.recovered_by})"
            out.append(f"step {rec.step} {rec.action} -> {detail}")
        return out

    def last_signature(self) -> str | None:
        return self._signatures[-1] if self._signatures else None

    def signature_history(self, limit: int = 10) -> list[str]:
        return list(self._signatures)[-limit:]

    # -- context building -------------------------------------------------

    def build_context(self, goal: str) -> str:
        parts = [f"GOAL: {goal}"]

        if self.active_subgoal:
            parts.append(
                f"SUBCGOAL ({self.subgoal_index + 1}/{self.subgoal_count}): "
                f"{self.active_subgoal}"
            )

        if self.summary:
            parts.append(f"SUMMARY: {self.summary}")

        recent = self.recent_working(8)
        if recent:
            parts.append("RECENT ACTIONS:\n" + "\n".join(recent))

        fails = self.recent_failures(3)
        if fails:
            parts.append("FAILURES:\n" + "\n".join(fails))

        eps = self.recent_episodes(3)
        if eps and not fails:
            parts.append("NOTABLE EVENTS:\n" + "\n".join(eps))

        return "\n\n".join(parts)

    # -- summarization ----------------------------------------------------

    def _maybe_summarize(self) -> None:
        if self._summarizer is None:
            return
        text = "\n".join(self.working)
        try:
            self.summary = self._summarizer(text)
        except Exception:
            # Summarization is best-effort; never let it crash the loop.
            pass

    def summarize_all(self, summarizer: Summarizer) -> None:
        if not callable(summarizer):
            raise TypeError("summarizer must be callable")
        text = "\n".join(self.working)
        self.summary = summarizer(text)

    def clear(self) -> None:
        self._working.clear()
        self._episodes.clear()
        self._failures.clear()
        self._signatures.clear()
        self.goal_reached_evidence.clear()
        self.active_subgoal = None


def build_context_for_goal(goal: str) -> str:
    """Minimal one-liner context helper used in tests and traces."""
    return f"GOAL: {goal}"