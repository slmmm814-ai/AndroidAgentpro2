"""APEX tiered models — one fast model per step, a big one for hard moments.

The v2 loop uses a single model for everything, so every step pays the latency
of the most capable (and slowest) model. APEX splits the workload the way a
human team would:

* **Fast model** — the default brain for routine steps: "what changed, what do
  I tap next". A flash-class model is enough, and it is 3–10x cheaper in time.
* **Big model** — invoked only for the moments that genuinely need it: initial
  goal decomposition, replanning after a failure, and final goal verification.

``TieredModels`` tracks call counts and latency per tier, so the speed-up is
measurable rather than claimed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

_LOGGER = logging.getLogger("agentpro.apex.tiered")


# --------------------------------------------------------------------------- #
# model client protocol
# --------------------------------------------------------------------------- #


class ChatModel(Protocol):
    """A minimal chat-completion client used by both tiers.

    ``model`` is the concrete model name chosen by the router. It must be
    honoured by the client: a tier split that never reaches the provider is
    just a config field that lies.
    """

    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        ...


# --------------------------------------------------------------------------- #
# config + stats
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TierConfig:
    """Which model to use for each tier, and its generation defaults."""

    fast_model: str
    big_model: str
    fast_max_tokens: int = 400
    big_max_tokens: int = 1200
    fast_temperature: float = 0.2
    big_temperature: float = 0.3


@dataclass
class TierStats:
    """Per-tier counters so latency savings are provable."""

    fast_calls: int = 0
    big_calls: int = 0
    fast_seconds: float = 0.0
    big_seconds: float = 0.0
    fast_failures: int = 0
    big_failures: int = 0
    big_routes: list[str] = field(default_factory=list)

    @property
    def total_calls(self) -> int:
        return self.fast_calls + self.big_calls

    @property
    def total_seconds(self) -> float:
        return self.fast_seconds + self.big_seconds

    @property
    def avg_fast_seconds(self) -> float:
        return self.fast_seconds / self.fast_calls if self.fast_calls else 0.0

    @property
    def avg_big_seconds(self) -> float:
        return self.big_seconds / self.big_calls if self.big_calls else 0.0

    @property
    def fast_share(self) -> float:
        """Fraction of calls served by the fast tier (higher = cheaper)."""
        return self.fast_calls / self.total_calls if self.total_calls else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fast_calls": self.fast_calls,
            "big_calls": self.big_calls,
            "fast_seconds": round(self.fast_seconds, 3),
            "big_seconds": round(self.big_seconds, 3),
            "avg_fast_seconds": round(self.avg_fast_seconds, 3),
            "avg_big_seconds": round(self.avg_big_seconds, 3),
            "fast_share": round(self.fast_share, 3),
            "fast_failures": self.fast_failures,
            "big_failures": self.big_failures,
            "big_routes": list(self.big_routes),
        }


# --------------------------------------------------------------------------- #
# tier router
# --------------------------------------------------------------------------- #


class TieredModels:
    """Routes each request to the cheapest model that can answer it.

    ``route`` is the only decision point: callers tag a request with a purpose
    and the router decides the tier. The rules are deliberately conservative —
    anything uncertain goes to the big model, because correctness beats speed.
    """

    # Purposes that always need the big model.
    BIG_PURPOSES = frozenset(
        {
            "plan",
            "decompose",
            "replan",
            "recover",
            "verify_goal",
            "summarize",
        }
    )
    # Purposes the fast model handles well.
    FAST_PURPOSES = frozenset(
        {
            "next_action",
            "classify",
            "extract",
            "label",
            "grounding",
        }
    )

    def __init__(
        self,
        client: ChatModel,
        config: TierConfig,
        *,
        fallback_to_big: bool = True,
    ) -> None:
        self._client = client
        self._config = config
        self.fallback_to_big = fallback_to_big
        self.stats = TierStats()

    @property
    def config(self) -> TierConfig:
        return self._config

    # -- routing ----------------------------------------------------------- #

    def route(self, purpose: str) -> str:
        """Return 'fast' or 'big' for a request purpose."""
        purpose = purpose.strip().lower()
        if purpose in self.BIG_PURPOSES:
            return "big"
        if purpose in self.FAST_PURPOSES:
            return "fast"
        # unknown purpose -> conservative: use the big model
        return "big"

    # -- calls -------------------------------------------------------------- #

    def ask(self, purpose: str, system: str, user: str) -> str:
        """Send a request on the tier chosen for ``purpose``."""
        tier = self.route(purpose)
        model = self._config.fast_model if tier == "fast" else self._config.big_model
        max_tokens = (
            self._config.fast_max_tokens if tier == "fast" else self._config.big_max_tokens
        )
        temperature = (
            self._config.fast_temperature
            if tier == "fast"
            else self._config.big_temperature
        )

        started = time.monotonic()
        try:
            text = self._client.chat(
                system,
                user,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception:
            if tier == "fast":
                self.stats.fast_failures += 1
                if self.fallback_to_big:
                    # retry once on the big tier
                    return self._call_big(system, user)
                raise
            self.stats.big_failures += 1
            raise

        elapsed = time.monotonic() - started
        if tier == "fast":
            self.stats.fast_calls += 1
            self.stats.fast_seconds += elapsed
        else:
            self.stats.big_calls += 1
            self.stats.big_seconds += elapsed
            self.stats.big_routes.append(purpose)
        _LOGGER.debug("tiered ask(%s) -> %s in %.2fs", purpose, tier, elapsed)
        return text

    def ask_fast(self, system: str, user: str) -> str:
        """Force the fast tier."""
        started = time.monotonic()
        try:
            text = self._client.chat(
                system,
                user,
                model=self._config.fast_model,
                max_tokens=self._config.fast_max_tokens,
                temperature=self._config.fast_temperature,
            )
        except Exception:
            self.stats.fast_failures += 1
            raise
        self.stats.fast_calls += 1
        self.stats.fast_seconds += time.monotonic() - started
        return text

    def ask_big(self, system: str, user: str) -> str:
        """Force the big tier."""
        started = time.monotonic()
        try:
            text = self._client.chat(
                system,
                user,
                model=self._config.big_model,
                max_tokens=self._config.big_max_tokens,
                temperature=self._config.big_temperature,
            )
        except Exception:
            self.stats.big_failures += 1
            raise
        self.stats.big_calls += 1
        self.stats.big_seconds += time.monotonic() - started
        return text

    def _call_big(self, system: str, user: str) -> str:
        started = time.monotonic()
        text = self._client.chat(
            system,
            user,
            model=self._config.big_model,
            max_tokens=self._config.big_max_tokens,
            temperature=self._config.big_temperature,
        )
        self.stats.big_calls += 1
        self.stats.big_seconds += time.monotonic() - started
        return text

    # -- health -------------------------------------------------------------- #

    def summary(self) -> str:
        s = self.stats
        return (
            f"fast={s.fast_calls}x{s.avg_fast_seconds:.1f}s "
            f"big={s.big_calls}x{s.avg_big_seconds:.1f}s "
            f"fast_share={s.fast_share:.0%}"
        )


# --------------------------------------------------------------------------- #
# provider adapter
# --------------------------------------------------------------------------- #


class OpenAIChatModel:
    """Adapts the shared OpenAI-compatible client to :class:`ChatModel`.

    The router chooses the model name per tier, so this adapter overrides the
    client's own default model with the one the router asked for. Without that
    override both tiers would silently hit the same model.
    """

    def __init__(self, client: Any, *, timeout: float = 30.0) -> None:
        if not hasattr(client, "complete"):
            raise TypeError("client must implement complete()")
        self._client = client
        self._timeout = timeout

    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        previous = getattr(self._client, "_model", None)
        try:
            self._client._model = model
            return self._client.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                timeout=self._timeout,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        finally:
            # restore so the adapter is safe to reuse across tiers
            if previous is not None:
                self._client._model = previous
