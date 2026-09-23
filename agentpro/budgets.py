"""Loop controls: budgets, repetition guard, and kill switch.

These are the safety rails that guarantee the agent can never spin forever:

* :class:`BudgetTracker` — hard limits on steps, actions, model calls, wall time
* :class:`LoopGuard`  — detects repeated (screen, action) signatures
* :class:`KillSwitch` — owner-visible emergency stop (file, env var, or flag)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence


class BudgetExceededError(RuntimeError):
    """Raised when a hard execution budget is exceeded."""


class KillSwitchEngaged(RuntimeError):
    """Raised when the emergency stop is engaged."""


@dataclass(frozen=True)
class BudgetLimits:
    max_steps: int = 300
    max_actions: int = 80
    max_model_calls: int = 150
    max_wall_seconds: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_steps", self.max_steps),
            ("max_actions", self.max_actions),
            ("max_model_calls", self.max_model_calls),
        ):
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


@dataclass
class BudgetTracker:
    limits: BudgetLimits
    clock: Callable[[], float] = time.monotonic

    steps: int = 0
    actions: int = 0
    model_calls: int = 0

    def __post_init__(self) -> None:
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        self._started: float | None = None

    def start(self) -> None:
        self._started = self.clock()

    @property
    def started(self) -> bool:
        return self._started is not None

    @property
    def elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return max(0.0, self.clock() - self._started)

    def mark_step(self) -> None:
        self.steps += 1
        self.ensure()

    def mark_action(self) -> None:
        self.actions += 1
        self.ensure()

    def mark_model_call(self) -> None:
        self.model_calls += 1
        self.ensure()

    def ensure(self) -> None:
        if self.steps > self.limits.max_steps:
            raise BudgetExceededError(
                f"step budget exceeded ({self.steps} > {self.limits.max_steps})"
            )
        if self.actions > self.limits.max_actions:
            raise BudgetExceededError(
                f"action budget exceeded ({self.actions} > {self.limits.max_actions})"
            )
        if self.model_calls > self.limits.max_model_calls:
            raise BudgetExceededError(
                f"model-call budget exceeded "
                f"({self.model_calls} > {self.limits.max_model_calls})"
            )
        if (
            self.limits.max_wall_seconds is not None
            and self.elapsed > self.limits.max_wall_seconds
        ):
            raise BudgetExceededError(
                f"wall-time budget exceeded "
                f"({self.elapsed:.1f}s > {self.limits.max_wall_seconds}s)"
            )

    def describe(self) -> str:
        return (
            f"steps={self.steps}/{self.limits.max_steps} "
            f"actions={self.actions}/{self.limits.max_actions} "
            f"model_calls={self.model_calls}/{self.limits.max_model_calls} "
            f"elapsed={self.elapsed:.1f}s"
        )


class LoopGuard:
    """Detects the agent repeating the same (state, action) cycle."""

    def __init__(
        self,
        *,
        repeat_threshold: int = 4,
        window: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if repeat_threshold < 1:
            raise ValueError("repeat_threshold must be >= 1")
        if window < 1:
            raise ValueError("window must be >= 1")
        self._threshold = repeat_threshold
        self._window = window
        self._clock = clock
        self._history: list[str] = []

    def record(self, signature: str) -> None:
        if not isinstance(signature, str) or not signature:
            raise ValueError("signature must be a non-empty string")
        self._history.append(signature)
        if len(self._history) > self._window:
            self._history = self._history[-self._window :]

    def is_repeating(self) -> bool:
        if len(self._history) < self._threshold:
            return False
        tail = self._history[-self._threshold :]
        return len(set(tail)) == 1

    def most_recent(self, limit: int = 5) -> list[str]:
        return self._history[-limit:]

    def longest_run(self) -> int:
        if not self._history:
            return 0
        longest = 1
        run = 1
        for prev, curr in zip(self._history, self._history[1:]):
            if curr == prev:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        return longest


@dataclass
class KillSwitch:
    """Emergency stop signal.

    Checks (in order): a hard flag, a kill file, and an environment variable.
    Any of them being set means "stop immediately". Fail-open is not allowed:
    a configured kill file that exists always stops the agent.
    """

    kill_file: str | None = None
    env_var: str = "AGENTPRO_KILL"
    flag: bool = False

    def is_engaged(self) -> bool:
        if self.flag:
            return True
        if self.kill_file:
            try:
                if os.path.exists(self.kill_file) and os.path.isfile(self.kill_file):
                    return True
            except OSError:
                pass
        if self.env_var and os.environ.get(self.env_var, ""):
            return bool(os.environ.get(self.env_var, "").strip().lower() in {"1", "yes", "true", "stop"})
        return False

    def engage(self) -> None:
        self.flag = True

    def disarm(self) -> None:
        self.flag = False


def signature_for(action: Mapping[str, Any], screen_fingerprint: str | None) -> str:
    """Stable signature of one (screen, action) pair for loop detection."""
    action_part = action.get("tool", action.get("action_type", "?"))
    args = action.get("args", {})
    key = args.get("resource_id") or args.get("package") or args.get("x")
    if key is not None:
        action_part = f"{action_part}:{key}"
    return f"{screen_fingerprint or '?'}::{action_part}"


def check_repeated_actions(
    signatures: Sequence[str],
    threshold: int = 4,
) -> bool:
    """Return True when the last ``threshold`` signatures are identical."""
    if len(signatures) < threshold:
        return False
    return len(set(signatures[-threshold:])) == 1