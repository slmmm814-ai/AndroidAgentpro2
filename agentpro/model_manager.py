"""Model Manager — an independent LLM layer.

Responsibilities:

* retries on transient failures (with a configurable fallback client)
* robust JSON extraction + optional schema validation (self-correction loop)
* structured-output assistance for planners and verifiers
* counts every call so the autonomy layer can enforce model-call budgets

The layer is deliberately model-agnostic: clients only need to satisfy the
:class:`agentpro.llm_planner.LLMClient` protocol.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .llm_planner import LLMClient, LLMError, _extract_json


JsonValidator = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class ModelCall:
    """One recorded model call (used for budgets and diagnostics)."""

    started_at: float
    duration_s: float
    ok: bool
    attempt: int
    error: str | None = None


class ModelError(RuntimeError):
    """Raised when the model layer cannot produce a valid result."""


def _coerce_json_object(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ModelError("model JSON root must be an object")
    return obj


class ModelManager:
    """Call orchestrator with retries, fallback, and validation."""

    def __init__(
        self,
        client: LLMClient,
        *,
        fallback_client: LLMClient | None = None,
        retries: int = 2,
        timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not hasattr(client, "complete"):
            raise TypeError("client must implement complete()")
        if fallback_client is not None and not hasattr(
            fallback_client, "complete"
        ):
            raise TypeError("fallback_client must implement complete()")
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise TypeError("retries must be an int")
        if retries < 0:
            raise ValueError("retries must not be negative")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if not callable(clock):
            raise TypeError("clock must be callable")

        self._client = client
        self._fallback = fallback_client
        self._retries = retries
        self._timeout = timeout
        self._clock = clock
        self._calls: list[ModelCall] = []

    @property
    def calls(self) -> list[ModelCall]:
        return list(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def _record(self, started: float, ok: bool, attempt: int, error: str | None) -> None:
        self._calls.append(
            ModelCall(
                started_at=started,
                duration_s=max(0.0, self._clock() - started),
                ok=ok,
                attempt=attempt,
                error=error,
            )
        )

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Run a chat completion with retries over the primary then fallback."""
        last_error: Exception | None = None
        started = self._clock()
        attempt = 0

        clients = [self._client]
        if self._fallback is not None:
            clients.append(self._fallback)

        for client in clients:
            for _ in range(self._retries + 1):
                attempt += 1
                try:
                    raw = client.complete(
                        list(messages),
                        json_mode=json_mode,
                        timeout=timeout or self._timeout,
                    )
                    if not isinstance(raw, str):
                        raise LLMError("LLM client must return a string")
                    self._record(started, True, attempt, None)
                    return raw
                except Exception as exc:  # noqa: BLE001 - layer must not crash
                    last_error = exc
                    self._record(started, False, attempt, str(exc))

        raise ModelError(f"model calls exhausted: {last_error}") from last_error

    def complete_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        validator: JsonValidator | None = None,
        attempts: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Return a parsed+validated JSON object, self-correcting on failure."""
        if validator is not None and not callable(validator):
            raise TypeError("validator must be callable")

        max_attempts = attempts if attempts is not None else max(2, self._retries + 1)
        if max_attempts < 1:
            raise ValueError("attempts must be >= 1")

        working = list(messages)
        errors: list[str] = []

        for _ in range(max_attempts):
            raw = self.complete(
                working,
                json_mode=True,
                timeout=timeout,
            )
            try:
                obj = _coerce_json_object(_extract_json(raw))
            except (ModelError, LLMError) as exc:
                errors.append(str(exc))
                working = self._feedback_messages(working, f"JSON parse error: {exc}")
                continue

            if validator is not None:
                try:
                    validator(obj)
                except (ValueError, TypeError) as exc:
                    errors.append(str(exc))
                    working = self._feedback_messages(working, f"Validation error: {exc}")
                    continue

            return obj

        raise ModelError(
            "model could not produce valid JSON: " + "; ".join(errors[-3:])
        )

    @staticmethod
    def _feedback_messages(
        messages: Sequence[Mapping[str, Any]],
        feedback: str,
    ) -> list[Mapping[str, Any]]:
        working = list(messages)
        working.append(
            {
                "role": "user",
                "content": (
                    "Your previous response was rejected. Fix it and respond "
                    f"again with only valid JSON. Reason: {feedback}"
                ),
            }
        )
        return working

    @classmethod
    def require(
        cls,
        obj: dict[str, Any],
        *required: str,
    ) -> None:
        """Built-in validator helper for required keys."""
        missing = [name for name in required if name not in obj]
        if missing:
            raise ValueError(
                f"response missing required fields: {', '.join(missing)}"
            )

    @classmethod
    def word_ratio(cls, text: str) -> float:
        """Heuristic fraction of tokens that look like real words.

        Used to spot verbose/hallucinated prose versus symbol-heavy JSON
        garbage: tokens that contain letters and at least one vowel count as
        words; runs of real letters are almost always language.
        """
        if not text:
            return 0.0
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_'-]*", text)
        if not tokens:
            return 0.0
        real = [t for t in tokens if re.search(r"[aeiouAEIOU]", t)]
        return len(real) / len(tokens)