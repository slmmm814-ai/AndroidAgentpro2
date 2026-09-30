"""FastDriver — low-latency control layer over the bridge.

Measurements on the live device show the bridge itself is fast:

    ui_dump      ~26 ms      get_window  ~17 ms
    tap         ~135 ms      screenshot  ~138 ms

The slowness in practice comes from *how* the driver is used: re-reading the
whole UI tree before every single tap, sleeping a fixed amount after each
action, and re-observing to confirm each step. This layer removes those
round trips:

  * ``tap_element``     — resolves text/index ONCE from a cached snapshot,
                          then taps without re-reading the tree.
  * ``tap_sequence``    — many taps with ONE observation, no per-tap waits.
  * ``smart_wait``      — polls instead of fixed sleeps, returns as soon as
                          the screen settles.
  * snapshot caching    — one ``observe()`` serves several following actions.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python_core"))

from bridge_client import BridgeClient  # noqa: E402

from agentpro.screen import (  # noqa: E402
    ScreenReader,
    ScreenSnapshot,
    InteractiveElement,
)


class FastDriverError(RuntimeError):
    """FastDriver could not complete the requested action."""


@dataclass
class _CachedSnapshot:
    snapshot: ScreenSnapshot
    taken_at: float


class FastDriver:
    """Low-latency bridge driver with snapshot caching and batched taps.

    Usage::

        d = FastDriver()
        d.tap_element("Close tab")      # uses cached snapshot, no re-read
        d.tap_sequence([(100, 200), (300, 400)])
        d.smart_wait(text="Done")       # poll until it appears
    """

    # A snapshot is reused while it is younger than this (seconds).
    DEFAULT_SNAPSHOT_TTL = 2.0
    # When polling, check this often.
    DEFAULT_POLL_INTERVAL = 0.12
    # ``smart_wait`` gives up after this.
    DEFAULT_WAIT_TIMEOUT = 8.0

    def __init__(
        self,
        client: BridgeClient | None = None,
        *,
        screen_reader: ScreenReader | None = None,
        snapshot_ttl: float = DEFAULT_SNAPSHOT_TTL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    ) -> None:
        self._client = client or BridgeClient()
        self._reader = screen_reader or ScreenReader(self._client)
        self._snapshot_ttl = max(0.0, snapshot_ttl)
        self._poll_interval = max(0.01, poll_interval)
        self._wait_timeout = max(0.5, wait_timeout)
        self._cache: _CachedSnapshot | None = None

    # ------------------------------------------------------------------ #
    # snapshot management
    # ------------------------------------------------------------------ #

    @property
    def client(self) -> BridgeClient:
        return self._client

    @property
    def reader(self) -> ScreenReader:
        return self._reader

    def observe(self, *, force: bool = False) -> ScreenSnapshot:
        """Return a snapshot, reusing the cache when it is still fresh."""
        now = time.monotonic()

        if (
            not force
            and self._cache is not None
            and (now - self._cache.taken_at) <= self._snapshot_ttl
        ):
            return self._cache.snapshot

        snapshot = self._reader.observe()
        self._cache = _CachedSnapshot(snapshot=snapshot, taken_at=now)
        return snapshot

    def invalidate(self) -> None:
        """Drop the cached snapshot so the next observe hits the device."""
        self._cache = None

    # ------------------------------------------------------------------ #
    # element resolution (no device round trip — uses the cache)
    # ------------------------------------------------------------------ #

    def find_element(
        self,
        *,
        text: str | None = None,
        index: int | None = None,
        clickable_only: bool = True,
    ) -> InteractiveElement:
        snapshot = self.observe()
        candidates = list(snapshot.elements)

        if index is not None:
            if not 0 <= index < len(candidates):
                raise FastDriverError(
                    f"element index {index} out of range "
                    f"({len(candidates)} visible)"
                )
            return candidates[index]

        if text is None:
            raise FastDriverError("provide text or index")

        lowered = text.strip().lower()
        exact: list[InteractiveElement] = []
        contains: list[InteractiveElement] = []

        for el in candidates:
            el_text = (el.text or "").strip()
            el_desc = (el.content_desc or "").strip()

            if not el_text and not el_desc:
                continue
            if clickable_only and not el.clickable:
                continue

            hay = (el_text + " " + el_desc).strip().lower()
            if hay == lowered:
                exact.append(el)
            elif lowered in hay:
                contains.append(el)

        pool = exact or contains
        if not pool:
            raise FastDriverError(f"no element matches {text!r}")

        # Prefer a clickable, non-editable, compact element — exactly the
        # semantics the full tool system uses, resolved locally.
        def score(el: InteractiveElement) -> tuple[int, int, int]:
            editable_penalty = 1 if el.editable else 0
            rid_bonus = 1 if el.resource_id else 0
            return (-rid_bonus, editable_penalty, el.area())

        return max(pool, key=score)

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #

    def tap(self, x: float, y: float) -> dict[str, Any]:
        self._client.tap(x, y)
        self.invalidate()
        return {"x": x, "y": y}

    def tap_element(
        self,
        text: str | None = None,
        *,
        index: int | None = None,
        clickable_only: bool = True,
    ) -> dict[str, Any]:
        el = self.find_element(text=text, index=index, clickable_only=clickable_only)
        cx, cy = el.center()
        self._client.tap(cx, cy)
        self.invalidate()
        return {"text": text or f"[{index}]", "x": cx, "y": cy}

    def tap_sequence(
        self,
        points: Sequence[tuple[float, float]],
        *,
        settle: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Tap several coordinates in one pass.

        Reads the snapshot at most once (only if the cache is stale) and
        performs every tap back-to-back. ``settle`` adds a single wait after
        the whole batch, not after each tap.
        """
        if not points:
            return []

        results: list[dict[str, Any]] = []
        for x, y in points:
            self._client.tap(x, y)
            results.append({"x": x, "y": y})

        if settle > 0:
            time.sleep(settle)

        self.invalidate()
        return results

    def tap_elements(self, texts: Sequence[str], *, settle: float = 0.0) -> list[dict[str, Any]]:
        """Resolve every text once, then tap all of them with no re-reads."""
        snapshot = self.observe()
        plan: list[tuple[float, float]] = []
        labels: list[str] = []

        for want in texts:
            el = self.find_element(text=want)
            cx, cy = el.center()
            plan.append((cx, cy))
            labels.append(want)

        for (x, y), label in zip(plan, labels):
            self._client.tap(x, y)

        if settle > 0:
            time.sleep(settle)

        self.invalidate()
        return [
            {"text": label, "x": x, "y": y}
            for label, (x, y) in zip(labels, plan)
        ]

    def input_text(self, text: str) -> dict[str, Any]:
        self._client.input_text(text)
        self.invalidate()
        return {"len": len(text)}

    def key_event(self, keycode: str) -> dict[str, Any]:
        self._client.key_event(keycode)
        self.invalidate()
        return {"keycode": keycode}

    def back(self) -> dict[str, Any]:
        return self.key_event("back")

    def home(self) -> dict[str, Any]:
        return self.key_event("home")

    def open_app(self, package: str) -> dict[str, Any]:
        self._client.launch_app(package)
        self.invalidate()
        return {"package": package}

    # ------------------------------------------------------------------ #
    # waiting (poll instead of sleeping)
    # ------------------------------------------------------------------ #

    def smart_wait(
        self,
        *,
        text: str | None = None,
        package: str | None = None,
        timeout: float | None = None,
        invert: bool = False,
    ) -> bool:
        """Poll until a condition holds. Returns True if it was met.

        ``invert=True`` waits for the condition to *disappear* instead.
        """
        deadline = time.monotonic() + (
            self._wait_timeout if timeout is None else max(0.1, timeout)
        )
        target = None if text is None else text.strip().lower()

        # Fast path: when only the package matters, poll the cheap
        # get_window (~17ms) instead of a full ui_dump (~26ms+).
        if target is None and package is not None:
            while True:
                current = self._reader.get_window_info()[0]
                match = current == package

                if invert:
                    if not match:
                        return True
                elif match:
                    return True

                if time.monotonic() >= deadline:
                    return False

                time.sleep(self._poll_interval)

        while True:
            snapshot = self.observe(force=True)
            matched = False

            if target is not None:
                for el in snapshot.elements:
                    hay = ((el.text or "") + " " + (el.content_desc or "")).lower()
                    if target in hay:
                        matched = True
                        break

            if package is not None:
                matched = matched or (snapshot.package_name == package)

            if invert:
                if not matched:
                    return True
            elif matched:
                return True

            if time.monotonic() >= deadline:
                return False

            time.sleep(self._poll_interval)

    def wait_stable(
        self,
        *,
        timeout: float | None = None,
        stable_reads: int = 2,
    ) -> bool:
        """Wait until the screen stops changing between reads."""
        deadline = time.monotonic() + (
            self._wait_timeout if timeout is None else max(0.5, timeout)
        )
        last_fp: str | None = None
        hits = 0

        while True:
            snapshot = self.observe(force=True)
            fp = snapshot.fingerprint or ""

            if fp == last_fp:
                hits += 1
                if hits >= stable_reads:
                    return True
            else:
                hits = 0
                last_fp = fp

            if time.monotonic() >= deadline:
                return False

            time.sleep(self._poll_interval)
