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
import time
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
        (3, "اهتزاز المكالمات مفعّل"): 4,
    }

    def __init__(self) -> None:
        self._pages = [
            _Screen(["الأجهزة المتصلة", "الأصوات والاهتزاز", "البطارية"]),
            _Screen(["إشعار Samsung", "الاهتزاز", "الصوت"]),
            _Screen(["اهتزاز المكالمات", "اهتزاز الإشعارات", "اهتزاز اللمس"]),
            _Screen(["اهتزاز المكالمات مفعّل", "اهتزاز المكالمات معطّل"]),
            # a screen that literally shows the goal text, used to prove that
            # finding the text is not enough when the wrong app is on screen
            _Screen(["serveai", "serveaircargo", "servaical exercise"]),
        ]
        self._page = 0
        self.tapped: list[str] = []
        self.tap_calls = 0
        self.swipes: list[str] = []
        #: how many times the deterministic text search was attempted
        self.search_calls = 0
        #: page index -> foreground package, to simulate leaving the app
        self.pages_package: dict[int, str] = {}

    def screenshot_b64(self) -> str:
        return ""

    # --- geometry -------------------------------------------------------
    def go_to(self, page: int) -> None:
        """Place the device on a given page, as if it had been navigated to."""
        self._page = page

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
            "package": self._package(),
            "activity": "Settings" if self._package() == "com.android.settings" else "MainActivity",
            "fingerprint": f"p{self._page}:{'|'.join(page.rows)}",
            "texts": list(page.rows),
            "candidates": cands,
        }

    def _package(self) -> str:
        """Which app is foreground, so a run can be shown leaving it."""
        return self.pages_package.get(self._page, "com.android.settings")

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
        self.swipes.append(direction)
        return {"ok": True, "moved": False}

    def find_text(self, text: str) -> dict[str, Any] | None:
        return None

    # --- physics surface exposed to the agent ---------------------------
    def physics(self) -> Any:  # type: ignore[override]
        return self

    def scroll_once(self, direction: str = "down") -> ScrollToTextResult:
        return self._fake_fling(direction)

    def scroll_to_text(self, text: str, max_flings: int = 6) -> ScrollToTextResult:
        """Fling until the text shows, like the real physics module.

        The scripted pages do not scroll, so this always fails — but it
        performs the flings, and each costs time, which is what the agent's
        deadline is meant to catch.
        """
        count = max(1, max_flings)
        self.search_calls += 1
        for _ in range(count):
            self._fake_fling("down", flings=1)
        # report the full set of flings as one result, moved=True
        return ScrollToTextResult(
            text=text,
            found=False,
            flings=count,
            center=None,
            duration_seconds=0.002 * count,
            moved=True,
            final_fingerprint=self.fingerprint(),
        )

    def _fake_fling(
        self, direction: str, *, found: bool = False, flings: int = 1
    ) -> ScrollToTextResult:
        self.swipes.append(direction)
        # a real fling takes a measurable moment; keep it small but non-zero
        time.sleep(0.002)
        return ScrollToTextResult(
            text="",
            found=found,
            flings=flings,
            center=None,
            duration_seconds=0.002 * flings,
            moved=flings > 0,
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

    def test_repeating_one_action_is_discouraged(self) -> None:
        """A planner that keeps proposing the same tap must be told to stop.

        On a live device the planner re-issued {"action":"tap","label":
        "serveai.ig"} every step and the loop spun to the repeat guard: the
        prompt described the screen but never said what had already been
        tried, and a fresh look at the same screen invites the same answer.
        """
        driver = _MultiScreenDriver()
        models = _TierStub(
            script=['{"action": "tap", "label": "البطارية"}'] * 6
        )
        result = _agent(driver, models, max_steps=6).run("افتح البطارية")

        # The driver records a tap even when it navigates nowhere, so the
        # same row would be tapped over and over without the memory.
        self.assertLessEqual(
            driver.tap_calls, 3, msg=f"same action repeated: {driver.tap_calls} taps"
        )
        # and the prompt must have told the planner what was already tried
        self.assertTrue(
            any("Already tried" in prompt for prompt in models.prompts),
            msg="the planner was never told which actions failed",
        )

    def test_goal_text_elsewhere_on_screen_is_not_success(self) -> None:
        """A stray copy of the goal string must not verify the goal.

        This is what a live run actually did: the goal "ابحث عن الحساب
        serveai" was printed in the title bar of the very terminal driving
        the phone, so a screen outside the app under test contained the
        target text and the loop reported a confident success while the phone
        was showing something else entirely.
        """
        driver = _MultiScreenDriver()
        driver.go_to(3)
        # pages 3 and 4 are Instagram; page 4 is the impostor window that
        # carries the goal text but is not the app under test
        driver.pages_package[3] = "com.instagram.android"
        driver.pages_package[4] = "com.termux"
        models = _TierStub(
            script=['{"action": "tap", "label": "اهتزاز المكالمات مفعّل"}'] * 2
        )
        agent = ApexAgent(
            driver,  # type: ignore[arg-type]
            models,  # type: ignore[arg-type]
            max_steps=4,
            expect_package="com.instagram.android",
        )
        result = agent.run("serveai")

        self.assertEqual(driver.page, 4, msg="precondition: the run left the app")
        self.assertFalse(result.success, msg="verified outside the app under test")
        self.assertIn("wrong_app", [s.result for s in result.steps])
        # and the loop must not keep working against the wrong app
        self.assertIn("com.termux", result.reason or "")
        self.assertLessEqual(
            driver.tap_calls, 1, msg=f"kept acting after leaving: {driver.tap_calls} taps"
        )

    def test_scroll_search_is_bounded(self) -> None:
        """A never-settling feed must not be flung forever.

        The live run spent 173 seconds and 41 flings scrolling an Instagram
        home feed that was never going to contain the target, because
        scroll_to_text only stopped when the fingerprint stopped changing and
        a feed always produces fresh content.
        """
        driver = _MultiScreenDriver()
        agent = ApexAgent(
            driver,  # type: ignore[arg-type]
            _TierStub(),  # type: ignore[arg-type]
            max_steps=4,
            scroll_flings=3,
            scroll_deadline_seconds=0.0001,
        )
        agent.run("zzz-not-anywhere")
        # the deterministic search must be given up on, not retried forever
        self.assertEqual(
            driver.search_calls, 1, msg=f"search retried {driver.search_calls} times"
        )
        self.assertTrue(
            any(s.result == "scroll_timeout" for s in agent.steps),
            msg=f"no scroll_timeout recorded: {[(s.action, s.result) for s in agent.steps]}",
        )


if __name__ == "__main__":
    unittest.main()
