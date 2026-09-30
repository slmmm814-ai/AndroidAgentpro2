"""APEX physics — deterministic device primitives with no model in the loop.

This module is the "spinal cord" of the agent: the operations a human performs
without thinking (fling to the end of a list, wait for the screen to settle,
scroll until a particular row appears). Keeping them out of the planner removes
the two loudest failure modes of the v2 loop:

* the planner re-issuing scroll directions at random because it cannot tell
  whether the list moved, and
* per-step model latency on operations that need no reasoning.

Everything here is expressed against a thin ``PhysicsDriver`` protocol so the
primitives are unit-testable with a fake driver.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

_LOGGER = logging.getLogger("agentpro.apex.physics")


# --------------------------------------------------------------------------- #
# driver protocol (implemented by the real bridge-backed driver, or a fake)
# --------------------------------------------------------------------------- #


class PhysicsDriver(Protocol):
    """The small surface the physics primitives need from the device."""

    def swipe(self, direction: str, distance: int = 900) -> dict[str, Any]:
        """Swipe so that content moves toward ``direction``."""
        ...

    def fingerprint(self) -> str:
        """Return a stable fingerprint of the *current* screen."""
        ...

    def screen_height(self) -> int:
        """Usable screen height in pixels."""
        ...

    def find_text(self, text: str) -> tuple[int, int] | None:
        """Return the center of the first element containing ``text``."""
        ...

    def visible_texts(self) -> list[str]:
        """Return currently visible text labels (order = top to bottom)."""
        ...


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FlingResult:
    """Outcome of a ``fling_until_end`` run."""

    direction: str
    flings: int
    moved_on_last_fling: bool
    settled_fingerprint: str
    duration_seconds: float
    reached_end: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "flings": self.flings,
            "moved_on_last_fling": self.moved_on_last_fling,
            "settled_fingerprint": self.settled_fingerprint,
            "duration_seconds": round(self.duration_seconds, 3),
            "reached_end": self.reached_end,
        }


@dataclass(frozen=True)
class ScrollToTextResult:
    """Outcome of a ``scroll_to_text`` run."""

    text: str
    found: bool
    flings: int
    center: tuple[int, int] | None
    duration_seconds: float
    moved: bool = False
    final_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "found": self.found,
            "flings": self.flings,
            "center": list(self.center) if self.center else None,
            "duration_seconds": round(self.duration_seconds, 3),
            "moved": self.moved,
            "final_fingerprint": self.final_fingerprint,
        }


@dataclass
class PhysicsStats:
    """Counters that let the caller prove the primitives did real work."""

    flings: int = 0
    swipes: int = 0
    fingerprints: int = 0
    waits: int = 0
    last_fling_reached_end: bool | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "flings": self.flings,
            "swipes": self.swipes,
            "fingerprints": self.fingerprints,
            "waits": self.waits,
            "last_fling_reached_end": self.last_fling_reached_end,
        }


# --------------------------------------------------------------------------- #
# the primitive set
# --------------------------------------------------------------------------- #


class Physics:
    """Deterministic, model-free device primitives.

    Parameters mirror the constants a human uses implicitly: how far to fling,
    how long to wait for the list to stop moving, and when to give up.
    """

    def __init__(
        self,
        driver: PhysicsDriver,
        *,
        settle_reads: int = 2,
        settle_interval: float = 0.18,
        max_flings: int = 40,
        fling_distance_ratio: float = 0.72,
    ) -> None:
        self._driver = driver
        self.settle_reads = max(1, settle_reads)
        self.settle_interval = max(0.01, settle_interval)
        self.max_flings = max(1, max_flings)
        self.fling_distance_ratio = max(0.1, min(0.95, fling_distance_ratio))
        self.stats = PhysicsStats()

    # -- private helpers --------------------------------------------------- #

    def _fling_distance(self) -> int:
        height = self._driver.screen_height()
        if height <= 0:
            return 900
        return int(height * self.fling_distance_ratio)

    def _settle(self) -> str:
        """Wait until the fingerprint stops changing; return that fingerprint."""
        self.stats.waits += 1
        last_fp: str | None = None
        hits = 0

        while True:
            fp = self._driver.fingerprint()
            self.stats.fingerprints += 1

            if fp == last_fp:
                hits += 1
                if hits >= self.settle_reads:
                    return fp
            else:
                hits = 0
                last_fp = fp

            time.sleep(self.settle_interval)

    # -- public primitives ------------------------------------------------- #

    def fling_until_end(
        self,
        direction: str = "down",
        *,
        max_flings: int | None = None,
    ) -> FlingResult:
        """Fling until the screen stops producing new content.

        This is the direct replacement for the v2 loop's tendency to re-decide
        the scroll direction every step. ``reached_end`` is True only when a
        fling left the fingerprint unchanged — i.e. the list genuinely cannot
        scroll any further.
        """
        if direction not in ("down", "up"):
            raise ValueError(f"direction must be 'down' or 'up', got {direction!r}")

        limit = self.max_flings if max_flings is None else max(1, max_flings)
        distance = self._fling_distance()
        started = time.monotonic()

        previous_fp = self._settle()
        flings = 0
        moved_on_last = False

        while flings < limit:
            self._driver.swipe(direction, distance)
            self.stats.swipes += 1
            flings += 1
            self.stats.flings += 1

            fp = self._settle()
            moved_on_last = fp != previous_fp

            if not moved_on_last:
                # Two consecutive identical fingerprints after a fling: the
                # list is at its end in this direction.
                break

            previous_fp = fp

        result = FlingResult(
            direction=direction,
            flings=flings,
            moved_on_last_fling=moved_on_last,
            settled_fingerprint=previous_fp,
            duration_seconds=time.monotonic() - started,
            reached_end=not moved_on_last,
        )
        self.stats.last_fling_reached_end = result.reached_end
        _LOGGER.info("fling_until_end(%s): %s", direction, result.to_dict())
        return result

    def scroll_once(self, direction: str = "down") -> FlingResult:
        """Fling a single screen in ``direction`` and settle.

        One deliberate step, for callers that drive their own loop and only
        need the primitive — not the end-of-list search.
        """
        if direction not in ("down", "up"):
            raise ValueError(f"direction must be 'down' or 'up', got {direction!r}")
        started = time.monotonic()
        previous_fp = self._settle()

        self._driver.swipe(direction, self._fling_distance())
        self.stats.swipes += 1
        self.stats.flings += 1

        fp = self._settle()
        result = FlingResult(
            direction=direction,
            flings=1,
            moved_on_last_fling=fp != previous_fp,
            settled_fingerprint=fp,
            duration_seconds=time.monotonic() - started,
            reached_end=fp == previous_fp,
        )
        return result

    def scroll_to_text(
        self,
        text: str,
        *,
        direction: str = "down",
        max_flings: int | None = None,
    ) -> ScrollToTextResult:
        """Fling until ``text`` becomes visible, then return its centre.

        Checks the tree before flinging at all, and between every fling, so a
        row already on screen costs zero round trips.
        """
        if not text:
            raise ValueError("text must be non-empty")

        started = time.monotonic()
        flings = 0
        limit = self.max_flings if max_flings is None else max(1, max_flings)

        before = self._driver.fingerprint()
        center = self._driver.find_text(text)
        if center is not None:
            return ScrollToTextResult(
                text=text,
                found=True,
                flings=0,
                center=center,
                duration_seconds=time.monotonic() - started,
                moved=False,
                final_fingerprint=before,
            )

        last_fp = before
        while flings < limit:
            self._driver.swipe(direction, self._fling_distance())
            self.stats.swipes += 1
            self.stats.flings += 1
            flings += 1

            last_fp = self._settle()
            center = self._driver.find_text(text)
            if center is not None:
                return ScrollToTextResult(
                    text=text,
                    found=True,
                    flings=flings,
                    center=center,
                    duration_seconds=time.monotonic() - started,
                    moved=last_fp != before,
                    final_fingerprint=last_fp,
                )

        return ScrollToTextResult(
            text=text,
            found=False,
            flings=flings,
            center=None,
            duration_seconds=time.monotonic() - started,
            # "not found" says nothing about movement: we may have scrolled
            # the whole list and still not seen the text.
            moved=last_fp != before,
            final_fingerprint=last_fp,
        )

    def wait_settled(self, *, timeout: float = 8.0) -> str:
        """Wait for the screen to become stable and return the fingerprint."""
        deadline = time.monotonic() + max(0.1, timeout)
        last_fp: str | None = None
        hits = 0

        while True:
            fp = self._driver.fingerprint()
            self.stats.fingerprints += 1

            if fp == last_fp:
                hits += 1
                if hits >= self.settle_reads:
                    return fp
            else:
                hits = 0
                last_fp = fp

            if time.monotonic() >= deadline:
                return fp

            time.sleep(self.settle_interval)
