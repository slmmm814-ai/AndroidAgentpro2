"""Unit tests for agentpro.apex.physics — deterministic primitives.

These run with a fake driver (no device, no network) so the behaviour of the
spinal-cord primitives can be proven exactly.
"""

from __future__ import annotations

import sys
import unittest
from typing import Any

sys.path.insert(0, "/root/AndroidAgentpro2")

from agentpro.apex.physics import (  # noqa: E402
    Physics,
    PhysicsDriver,
)


class _FakePhysicsDriver:
    """Scriptable driver implementing the ``PhysicsDriver`` protocol."""

    def __init__(
        self,
        *,
        screens: list[list[str]] | None = None,
        height: int = 2400,
        sticky: bool = False,
    ) -> None:
        # screens: index 0 is the start screen; each fling "down" advances the
        # page until the list stops changing.
        self._screens = screens or [
            ["A", "B", "C"],
            ["D", "E", "F"],
            ["G", "H", "I"],
        ]
        self._page = 0
        self._height = height
        self._sticky = sticky
        self.swipes: list[tuple[str, int]] = []
        self.fingerprint_reads = 0

    # -- protocol ---------------------------------------------------------- #

    def swipe(self, direction: str, distance: int = 900) -> dict[str, Any]:
        self.swipes.append((direction, distance))
        if direction == "down" and self._page < len(self._screens) - 1:
            self._page += 1
        # "sticky" mode: the last screen never changes no matter what
        if self._sticky and self._page >= len(self._screens) - 1:
            self._page = len(self._screens) - 1
        return {"direction": direction, "distance": distance}

    def fingerprint(self) -> str:
        self.fingerprint_reads += 1
        return "|".join(self._screens[self._page])

    def screen_height(self) -> int:
        return self._height

    def find_text(self, text: str) -> tuple[int, int] | None:
        page = self._screens[self._page]
        if text in page:
            return (540, 100 + page.index(text) * 200)
        return None

    def visible_texts(self) -> list[str]:
        return list(self._screens[self._page])


class TestFlingUntilEnd(unittest.TestCase):
    """The core promise: fling stops as soon as the list stops moving."""

    def test_advances_until_content_stops(self) -> None:
        driver = _FakePhysicsDriver()
        physics = Physics(driver, settle_reads=1, settle_interval=0.0)

        result = physics.fling_until_end("down")

        self.assertTrue(result.reached_end)
        self.assertEqual(result.direction, "down")
        # start page (2 swipes to reach the last page), then one more swipe
        # that changes nothing -> stop.
        self.assertEqual(len(driver.swipes), 3)
        self.assertGreater(result.flings, 0)

    def test_reports_not_reached_when_still_moving(self) -> None:
        # An infinitely-scrolling driver: every swipe reveals new content.
        class _Infinite(_FakePhysicsDriver):
            def __init__(self) -> None:
                super().__init__(height=2000)
                self._counter = 0

            def swipe(self, direction: str, distance: int = 900) -> dict[str, Any]:
                self.swipes.append((direction, distance))
                self._counter += 1
                return {}

            def fingerprint(self) -> str:
                self.fingerprint_reads += 1
                return f"page-{self._counter}"

        physics = Physics(_Infinite(), settle_reads=1, settle_interval=0.0, max_flings=4)
        result = physics.fling_until_end("down")

        self.assertFalse(result.reached_end)
        self.assertEqual(result.flings, 4)
        self.assertTrue(result.moved_on_last_fling)

    def test_already_at_end_does_no_swipes(self) -> None:
        driver = _FakePhysicsDriver(screens=[["only", "one", "page"]])
        physics = Physics(driver, settle_reads=1, settle_interval=0.0)

        result = physics.fling_until_end("down")

        self.assertTrue(result.reached_end)
        self.assertEqual(result.flings, 1)
        # first swipe changes nothing -> exactly one swipe then stop
        self.assertEqual(len(driver.swipes), 1)

    def test_rejects_invalid_direction(self) -> None:
        physics = Physics(_FakePhysicsDriver())
        with self.assertRaises(ValueError):
            physics.fling_until_end("sideways")

    def test_respects_max_flings(self) -> None:
        class _AlwaysNew(_FakePhysicsDriver):
            def __init__(self) -> None:
                super().__init__(height=2000)
                self._n = 0

            def swipe(self, direction: str, distance: int = 900) -> dict[str, Any]:
                self.swipes.append((direction, distance))
                self._n += 1
                return {}

            def fingerprint(self) -> str:
                self.fingerprint_reads += 1
                return f"p{self._n}"

        physics = Physics(_AlwaysNew(), settle_reads=1, settle_interval=0.0, max_flings=3)
        result = physics.fling_until_end("down", max_flings=2)

        self.assertEqual(result.flings, 2)
        self.assertEqual(physics.stats.swipes, 2)


class TestScrollToText(unittest.TestCase):
    def test_finds_row_already_visible_without_flinging(self) -> None:
        driver = _FakePhysicsDriver()
        physics = Physics(driver, settle_reads=1, settle_interval=0.0)

        result = physics.scroll_to_text("A")

        self.assertTrue(result.found)
        self.assertEqual(result.flings, 0)
        self.assertEqual(len(driver.swipes), 0)
        self.assertIsNotNone(result.center)

    def test_flings_until_row_appears(self) -> None:
        driver = _FakePhysicsDriver()
        physics = Physics(driver, settle_reads=1, settle_interval=0.0)

        result = physics.scroll_to_text("H")

        self.assertTrue(result.found)
        self.assertGreaterEqual(result.flings, 1)
        # "H" is on the third page at index 1 -> 100 + 1*200
        self.assertEqual(result.center, (540, 300))

    def test_returns_not_found_within_budget(self) -> None:
        driver = _FakePhysicsDriver()
        physics = Physics(driver, settle_reads=1, settle_interval=0.0)

        result = physics.scroll_to_text("ZETA", max_flings=2)

        self.assertFalse(result.found)
        self.assertEqual(result.flings, 2)
        self.assertIsNone(result.center)

    def test_rejects_empty_text(self) -> None:
        physics = Physics(_FakePhysicsDriver())
        with self.assertRaises(ValueError):
            physics.scroll_to_text("")


class TestWaitSettled(unittest.TestCase):
    def test_returns_fingerprint_once_stable(self) -> None:
        driver = _FakePhysicsDriver()
        physics = Physics(driver, settle_reads=2, settle_interval=0.0)

        fp = physics.wait_settled(timeout=5.0)

        self.assertEqual(fp, "A|B|C")
        self.assertGreater(physics.stats.fingerprints, 0)

    def test_times_out_returning_current_fingerprint(self) -> None:
        class _Flicker(_FakePhysicsDriver):
            def fingerprint(self) -> str:
                self.fingerprint_reads += 1
                return f"unstable-{self.fingerprint_reads}"

        physics = Physics(_Flicker(), settle_reads=5, settle_interval=0.0)
        fp = physics.wait_settled(timeout=0.2)

        self.assertTrue(fp.startswith("unstable-"))


class TestStats(unittest.TestCase):
    def test_counts_swipes_and_flings(self) -> None:
        driver = _FakePhysicsDriver()
        physics = Physics(driver, settle_reads=1, settle_interval=0.0)

        physics.fling_until_end("down")

        d = physics.stats.to_dict()
        self.assertEqual(d["flings"], d["swipes"])
        self.assertGreater(d["flings"], 0)
        self.assertTrue(d["last_fling_reached_end"])


class TestProtocolConformance(unittest.TestCase):
    def test_fake_satisfies_protocol(self) -> None:
        driver: PhysicsDriver = _FakePhysicsDriver()
        # If the fake does not match the protocol, this assignment errors at
        # type-check time; calling proves runtime shape.
        self.assertEqual(driver.screen_height(), 2400)


if __name__ == "__main__":
    unittest.main()
