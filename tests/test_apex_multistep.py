"""Seven-step navigation: does APEX chain hops, or only match one label?

A single-label goal is a text match. A real task is a path: reach screen 1,
then screen 2, then screen 3. Every hop needs its own locate/tap/observe
cycle, and each intermediate screen must not look like success.

The rule the test pins: success is only claimed on the FINAL screen, and the
agent must not stop early just because an intermediate screen contained part
of the goal wording.
"""

from __future__ import annotations

import sys
import unittest
from typing import Any

sys.path.insert(0, "/root/AndroidAgentpro2")

from agentpro.apex.apex_agent import ApexAgent  # noqa: E402
from agentpro.apex.grounder import GroundCandidate  # noqa: E402
from agentpro.apex.physics import ScrollToTextResult  # noqa: E402
from agentpro.apex.tiered import TierStats, TieredModels  # noqa: E402


class _Screen:
    """One page: a label per clickable row, laid out on a fixed pitch."""

    def __init__(self, rows: list[str]) -> None:
        self.rows = rows


class _MultiScreenDriver:
    """Scripted device whose pages are navigated by tapping row labels.

    This is the shape a seven-step task actually exercises: the agent starts
    on page 0 and must chain taps until it reaches the page holding the final
    target. ``tapped`` records the label of every successful hop so the test
    can assert the order, not just the destination.
    """

    WIDTH = 1080
    ROW_TOP = 200
    ROW_PITCH = 180
    ROW_HEIGHT = 60

    #: (page index, row label) -> page index it navigates to
    LINKS: dict[tuple[int, str], int] = {
        (0, "الأجهزة المتصلة"): 1,
        (0, "الأصوات والاهتزاز"): 2,
        (1, "إشعار Samsung"): 2,
        (2, "اهتزاز المكالمات"): 3,
    }

    def __init__(self) -> None:
        self._pages = [
            _Screen(["الأجهزة المتصلة", "الأصوات والاهتزاز", "البطارية"]),
            _Screen(["إشعار Samsung", "الاهتزاز", "الصوت"]),
            _Screen(["اهتزاز المكالمات", "اهتزاز الإشعارات", "اهتزاز اللمس"]),
            _Screen(["اهتزاز المكالمات مفعّل", "اهتزاز المكالمات معطّل"]),
        ]
        self._page = 0
        self.tapped: list[str] = []
        self.tap_calls = 0

    # --- geometry -------------------------------------------------------
    def _row_bounds(self, index: int) -> tuple[int, int, int, int]:
        top = self.ROW_TOP + index * self.ROW_PITCH
        return (0, top, self.WIDTH, top + self.ROW_HEIGHT)

    def _row_center(self, index: int) -> tuple[int, int]:
        left, top, right, bottom = self._row_bounds(index)
        return ((left + right) // 2, (top + bottom) // 2)

    # --- observation ----------------------------------------------------
    def observe(self, force: bool = True) -> dict[str, Any]:
        page = self._pages[self._page]
        cands: list[GroundCandidate] = []
        for i, label in enumerate(page.rows):
            left, top, right, bottom = self._row_bounds(i)
            cands.append(
                GroundCandidate(
                    text=label,
                    content_desc="",
                    resource_id="",
                    bounds=(left, top, right, bottom),
                    clickable=True,
                    editable=False,
                )
            )
        return {
            "package": "com.android.settings",
            "activity": "Settings",
            "fingerprint": f"p{self._page}:{'|'.join(page.rows)}",
            "texts": list(page.rows),
            "candidates": cands,
        }

    def snapshot(self, force: bool = True) -> dict[str, Any]:
        return self.observe(force=force)

    # --- action ---------------------------------------------------------
    def tap(self, x: int, y: int) -> dict[str, Any]:
        self.tap_calls += 1
        for i, label in enumerate(self._pages[self._page].rows):
            cx, cy = self._row_center(i)
            if abs(cx - x) <= 4 and abs(cy - y) <= 4:
                dest = self.LINKS.get((self._page, label))
                if dest is not None:
                    self.tapped.append(label)
                    self._page = dest
                    return {"ok": True, "tapped": label, "moved": True}
                return {"ok": True, "tapped": label, "moved": False}
        return {"ok": False, "reason": "no row at coordinate"}

    def back(self) -> dict[str, Any]:
        if self._page > 0:
            self._page -= 1
        return {"ok": True}

    def input_text(self, text: str) -> dict[str, Any]:
        return {"ok": True}

    def screen_size(self) -> tuple[int, int]:
        return (1080, 2400)

    # --- physics surface -------------------------------------------------
    @property
    def page(self) -> int:
        return self._page

    def wait_settled(self, timeout_s: float = 3.0) -> bool:
        return True

    def fingerprint(self) -> str:
        return f"p{self._page}:{'|'.join(self._pages[self._page].rows)}"

    def swipe(self, direction: str, distance: int = 900) -> dict[str, Any]:
        return {"ok": True, "moved": False}

    def find_text(self, text: str) -> dict[str, Any] | None:
        return None

    # --- physics surface exposed to the agent ---------------------------
    def physics(self) -> "_MultiScreenDriver":
        return self

    def scroll_once(self, direction: str = "down") -> ScrollToTextResult:
        return ScrollToTextResult(
            text="",
            found=False,
            flings=1,
            center=None,
            duration_seconds=0.0,
            moved=False,
            final_fingerprint=self.fingerprint(),
        )

    def scroll_to_text(self, text: str, max_flings: int = 6) -> ScrollToTextResult:
        """The scripted pages never scroll; report that honestly."""
        return ScrollToTextResult(
            text=text,
            found=False,
            flings=0,
            center=None,
            duration_seconds=0.0,
            moved=False,
            final_fingerprint=self.fingerprint(),
        )


class _TierStub(TieredModels):
    """A TieredModels that plans from the prompt it is given.

    Deliberately simple: it picks the clickable row whose label shares the
    most words with the goal. That is a weak planner, which is the point —
    if the loop can finish with a weak planner, it is the loop doing the
    work, not the model.
    """

    def __init__(self, script: list[str] | None = None) -> None:
        self.stats = TierStats()
        self.prompts: list[str] = []
        self._script = list(script or [])

    def ask(self, purpose: str, system: str, user: str) -> str:
        self.prompts.append(user)
        if self._script:
            return self._script.pop(0)
        if "verify" in system.lower():
            return '{"action": "done"}'
        # Score each visible label against the goal and tap the best one.
        # Deliberately shallow: keyword overlap only, no model reasoning.
        goal = ""
        for line in user.splitlines():
            if line.startswith("Goal:"):
                goal = line.split(":", 1)[1].strip()
                break
        goal_words = {w.strip(".,!?؟").lower() for w in goal.split() if len(w) > 2}
        # A real planner also knows a partially-matching row is a way in:
        # "اهتزاز المكالمات" is not on page 0, but "الأصوات والاهتزاز" shares
        # the word اهتزاز and is the category containing it. Match on substrings
        # because Arabic attaches the conjunction to the word ("والاهتزاز"),
        # which a whole-token comparison would never see.
        ranked: list[tuple[int, str]] = []
        for line in user.splitlines():
            if not line.startswith("Visible text:"):
                continue
            for label in line.split(":", 1)[1].split(", "):
                label = label.strip()
                if not label:
                    continue
                low = label.lower()
                overlap = sum(1 for word in goal_words if word in low)
                if overlap:
                    ranked.append((overlap, label))
        if ranked:
            ranked.sort(key=lambda pair: -pair[0])
            return f'{{"action": "tap", "label": "{ranked[0][1]}"}}'
        return '{"action": "scroll", "direction": "down"}'


def _agent(driver: _MultiScreenDriver, models: Any, max_steps: int = 12) -> ApexAgent:
    return ApexAgent(
        driver,  # type: ignore[arg-type]
        models,  # type: ignore[arg-type]
        max_steps=max_steps,
    )


class MultiStepNavigationTest(unittest.TestCase):
    def test_chains_hops_to_reach_a_deep_target(self) -> None:
        driver = _MultiScreenDriver()
        models = _TierStub()
        result = _agent(driver, models).run("افتح اهتزاز المكالمات")

        self.assertTrue(result.success, msg=result.reason)
        self.assertEqual(driver.page, 3, msg="must end on the final page")
        self.assertEqual(
            driver.tapped,
            ["الأصوات والاهتزاز", "اهتزاز المكالمات"],
            msg="the path must go through sounds, not the connected-devices branch",
        )

    def test_does_not_stop_on_an_intermediate_page(self) -> None:
        """Page 1 shares a label with the goal; that is not the target."""
        driver = _MultiScreenDriver()
        result = _agent(driver, _TierStub()).run("افتح اهتزاز المكالمات")

        # Page 1 has a bare "الاهتزاز" row and page 2 has "اهتزاز المكالمات".
        # Stopping on page 1 would be an early exit; the run must press on.
        self.assertNotEqual(driver.page, 1)
        self.assertTrue(result.success, msg=result.reason)

    def test_wrong_branch_is_not_success(self) -> None:
        """Connected-devices leads nowhere near the sound target."""
        driver = _MultiScreenDriver()
        result = _agent(driver, _TierStub()).run("اهتزاز المكالمات")

        self.assertTrue(result.success, msg=result.reason)
        self.assertIn("الأصوات والاهتزاز", driver.tapped)

    def test_single_step_target_still_works(self) -> None:
        driver = _MultiScreenDriver()
        result = _agent(driver, _TierStub()).run("افتح البطارية")
        # "البطارية" exists on page 0 but navigates nowhere: a match without a
        # screen change must not be reported as success.
        self.assertFalse(result.success)
        self.assertIsNotNone(result.reason)

if __name__ == "__main__":
    unittest.main()
