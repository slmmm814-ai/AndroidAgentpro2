"""Unit tests for agentpro.apex.apex_agent — the integrated loop."""

from __future__ import annotations

import sys
import unittest
from typing import Any

sys.path.insert(0, "/root/AndroidAgentpro2")

from agentpro.apex.apex_agent import (  # noqa: E402
    ApexAgent,
    ApexDriver,
    _build_planner_prompt,
    _extract_target,
    _find_candidate,
)
from agentpro.apex.appmap import AppMap
from agentpro.apex.grounder import GroundCandidate
from agentpro.apex.physics import Physics
from agentpro.apex.tiered import TierConfig, TieredModels


class _FakePhysicsDriver:
    """Scripted scrolling device.

    Row geometry is single-sourced through :meth:`row_bounds` so the
    coordinates reported by ``find_text`` and the candidate bounds handed to
    the agent always agree — exactly as they do on a real device, where both
    are derived from the same UI tree.
    """

    ROW_TOP = 100
    ROW_PITCH = 200
    ROW_HEIGHT = 50
    WIDTH = 1080

    def __init__(self, *, screens: list[list[str]] | None = None, sticky: bool = True) -> None:
        self._screens = screens or [
            ["A", "B", "C"],
            ["D", "E", "F"],
            ["Settings", "H", "I"],
        ]
        self._page = 0
        # sticky: once at the last page the fingerprint stops changing, so the
        # physics settle logic can converge (mirrors a real list end).
        self._sticky = sticky
        self.swipes: list[tuple[str, int]] = []

    def row_bounds(self, index: int) -> tuple[int, int, int, int]:
        top = self.ROW_TOP + index * self.ROW_PITCH
        return (0, top, self.WIDTH, top + self.ROW_HEIGHT)

    def row_center(self, index: int) -> tuple[int, int]:
        left, top, right, bottom = self.row_bounds(index)
        return ((left + right) // 2, (top + bottom) // 2)

    def swipe(self, direction: str, distance: int = 900) -> dict[str, Any]:
        self.swipes.append((direction, distance))
        if direction == "down" and self._page < len(self._screens) - 1:
            self._page += 1
        elif self._sticky and self._page >= len(self._screens) - 1:
            # at the bottom a further swipe changes nothing
            self._page = len(self._screens) - 1
        return {}

    def fingerprint(self) -> str:
        return "|".join(self._screens[self._page])

    def screen_height(self) -> int:
        return 2400

    def find_text(self, text: str) -> tuple[int, int] | None:
        # case-insensitive, mirroring the real BridgePhysicsDriver
        lowered = text.strip().lower()
        page = [t.lower() for t in self._screens[self._page]]
        if lowered in page:
            return self.row_center(page.index(lowered))
        return None

    def visible_texts(self) -> list[str]:
        return list(self._screens[self._page])


class _FakeApexDriver:
    """ApexDriver implementation backed by a scripted physics driver.

    The rows on screen *are* the physics driver's current page, and their
    bounds come from the same geometry ``find_text`` uses, so a coordinate the
    physics layer reports always lands on the row it meant.

    Tapping a row navigates away and shows that row's name, mirroring a real
    device where opening an item puts its name in the new screen.
    """

    def __init__(self, *, texts: list[str], package: str = "com.app") -> None:
        self._texts = texts
        self._package = package
        self._physics_driver = _FakePhysicsDriver(
            screens=[list(texts), ["D", "E", "F"], ["Settings", "H", "I"]]
        )
        self.taps: list[tuple[int, int]] = []
        self.inputs: list[str] = []
        self.backs = 0
        self._navigated = False
        self._opened: str | None = None
        self._physics: Physics | None = None

    def snapshot(self) -> dict[str, Any]:
        # after a tap we navigate away; otherwise the visible rows are the
        # physics driver's current page, so scrolling changes the screen.
        if self._navigated:
            texts = [self._opened or "Opened", "Back"]
        else:
            texts = self._physics_driver.visible_texts()
        return {
            "package": self._package,
            "activity": "MainActivity",
            "fingerprint": (
                f"opened:{self._opened}" if self._navigated
                else self._physics_driver.fingerprint()
            ),
            "texts": texts,
            "candidates": [
                GroundCandidate(
                    text=t,
                    content_desc="",
                    resource_id="",
                    bounds=self._physics_driver.row_bounds(i),
                    clickable=True,
                )
                for i, t in enumerate(texts)
            ],
        }

    def screenshot_b64(self) -> str | None:
        return None

    def screen_size(self) -> tuple[int, int]:
        return (1080, 2400)

    def tap(self, x: int, y: int) -> dict[str, Any]:
        self.taps.append((x, y))
        # Opening an item shows its name in the new screen's toolbar, which is
        # the deterministic evidence the verifier looks for. Resolve the tap
        # back to the row it landed on.
        self._opened = self._row_at(x, y) or "Opened"
        self._navigated = True
        return {"x": x, "y": y}

    def _row_at(self, x: int, y: int) -> str | None:
        state = self.snapshot()
        for cand in state["candidates"]:
            left, top, right, bottom = cand.bounds
            if left <= x <= right and top <= y <= bottom:
                return cand.text
        return None

    def physics(self) -> Physics:
        # a stable instance so scroll state persists across loop iterations
        if self._physics is None:
            self._physics = Physics(self._physics_driver, settle_reads=1, settle_interval=0.0)
        return self._physics

    def back(self) -> dict[str, Any]:
        self.backs += 1
        self._navigated = False
        self._opened = None
        return {}

    def input_text(self, text: str) -> dict[str, Any]:
        self.inputs.append(text)
        return {"len": len(text)}


class _ScriptedModel:
    def __init__(self, answer: str = "") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []
        self.purposes: list[str] = []

    def chat(
        self,
        system: str,
        user: str,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        self.calls.append((system[:12], user[:24]))
        if "verify" in system.lower():
            self.purposes.append("verify_goal")
            return '{"done": false}'
        self.purposes.append("plan")
        return self.answer


def _make_agent(
    driver: _FakeApexDriver,
    answer: str = "",
) -> tuple[ApexAgent, _ScriptedModel]:
    model = _ScriptedModel(answer)
    models = TieredModels(model, TierConfig(fast_model="f", big_model="b"))
    return ApexAgent(driver, models, max_steps=10), model


class TestHelpers(unittest.TestCase):
    def test_extract_target_named(self) -> None:
        self.assertEqual(_extract_target("open the chat named Team"), "team")

    def test_extract_target_called(self) -> None:
        self.assertEqual(_extract_target("message called Ali"), "ali")

    def test_extract_target_fallback_keeps_the_phrase(self) -> None:
        # "menu" alone also matches menus elsewhere; the visible label is the
        # whole phrase, so the phrase is the anchor.
        self.assertEqual(_extract_target("scroll to the settings menu"), "settings menu")

    def test_extract_target_empty(self) -> None:
        self.assertIsNone(_extract_target(""))

    def test_extract_target_tap_marker(self) -> None:
        # Targets are normalised to lower case: matching is case-insensitive,
        # and it keeps the step records and the planner prompt stable.
        self.assertEqual(_extract_target("tap Send"), "send")

    def test_extract_target_drops_leading_filler(self) -> None:
        self.assertEqual(_extract_target("open the item named Battery"), "battery")
        self.assertEqual(_extract_target("افتح صفحة الأصوات والاهتزاز"), "الأصوات والاهتزاز")

    def test_extract_target_keeps_arabic_phrase_whole(self) -> None:
        # "المكالمات" alone matches call-history rows; the label is the phrase.
        self.assertEqual(_extract_target("افتح اهتزاز المكالمات"), "اهتزاز المكالمات")


class TestPlannerLabelTaps(unittest.TestCase):
    """A planner that answers with a label must actually move the finger.

    Coordinates were the only accepted tap payload, so every model replying
    {"action":"tap","label":"..."} was recorded as unparsable and the loop
    spun on the same screen until the repeat guard stopped it. A model can
    read the tree; it cannot measure pixels, so label is the natural form.
    """

    def _driver_with_row(self) -> Any:
        from tests.test_apex_multistep import _MultiScreenDriver

        return _MultiScreenDriver()

    def test_label_tap_is_executed_not_rejected(self) -> None:
        from tests.test_apex_multistep import _TierStub

        driver = self._driver_with_row()
        # goal matches nothing on page 0, so the loop falls through to the
        # planner, which replies with a label that does exist.
        models = _TierStub(script=['{"action": "tap", "label": "الأصوات والاهتزاز"}'] * 3)
        result = ApexAgent(driver, models, max_steps=4).run("افتح اهتزاز المكالمات")

        self.assertIn("الأصوات والاهتزاز", driver.tapped)
        self.assertTrue(
            any(s.result == "tapped_by_label" for s in result.steps),
            msg=f"label tap was not executed: {[(s.action, s.result) for s in result.steps]}",
        )

    def test_unknown_label_is_reported_not_guessed(self) -> None:
        from tests.test_apex_multistep import _TierStub

        driver = self._driver_with_row()
        models = _TierStub(script=['{"action": "tap", "label": "nope-not-here"}'] * 3)
        result = ApexAgent(driver, models, max_steps=4).run("افتح اهتزاز المكالمات")

        self.assertEqual(driver.tapped, [], msg="must not tap an arbitrary coordinate")
        self.assertTrue(any(s.result == "label_not_found" for s in result.steps))

    def test_find_candidate_match(self) -> None:
        cands = [GroundCandidate("Team", "", "", (0, 0, 10, 10), True)]
        self.assertIsNotNone(_find_candidate("Team", cands))

    def test_find_candidate_no_match(self) -> None:
        cands = [GroundCandidate("Other", "", "", (0, 0, 10, 10), True)]
        self.assertIsNone(_find_candidate("Team", cands))

    def test_build_prompt_includes_goal_and_texts(self) -> None:
        m = AppMap()
        m.observe("fp")
        prompt = _build_planner_prompt("do X", "com.app", ["A", "B"], m, 1)
        self.assertIn("Goal: do X", prompt)
        self.assertIn("A, B", prompt)
        self.assertIn("com.app", prompt)


class TestRuleOneTap(unittest.TestCase):
    def test_target_visible_taps_without_model(self) -> None:
        driver = _FakeApexDriver(texts=["Team", "Other"])
        agent, model = _make_agent(driver)

        report = agent.run("open the chat named Team")

        self.assertEqual(len(driver.taps), 1)
        self.assertEqual(model.calls, [])
        self.assertGreater(report.taps, 0)

    def test_tap_records_step(self) -> None:
        driver = _FakeApexDriver(texts=["Team"])
        agent, _ = _make_agent(driver)

        report = agent.run("open the chat named Team")

        self.assertEqual(report.steps[0].action, "tap")
        self.assertEqual(report.steps[0].tier, "none")
        self.assertEqual(report.steps[0].detail, "tree-exact 'team' at (540,125)")


class TestRuleTwoScroll(unittest.TestCase):
    def test_target_below_scrolls_then_taps(self) -> None:
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, model = _make_agent(driver)

        report = agent.run("open the chat named Settings")

        # the fake driver scrolls "Settings" into view then taps it
        self.assertGreater(report.flings, 0)
        self.assertEqual(len(driver.taps), 1)
        self.assertEqual(model.calls, [])

    def test_scroll_records_step(self) -> None:
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, _ = _make_agent(driver)

        report = agent.run("open the chat named Settings")

        actions = [s.action for s in report.steps]
        self.assertIn("scroll", actions)


class TestRuleThreePlanner(unittest.TestCase):
    def test_planner_invoked_when_target_absent(self) -> None:
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, model = _make_agent(driver, answer='{"action": "scroll", "direction": "down"}')

        report = agent.run("open the chat named Zzz")

        self.assertGreater(len(model.calls), 0)
        self.assertTrue(any(s.action.startswith("plan") for s in report.steps))

    def test_planner_chatter_is_not_success(self) -> None:
        """Talking about the goal must not be reported as having done it."""
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, _ = _make_agent(driver, answer="The chat Team is reachable now")

        report = agent.run("open the chat named Team")

        self.assertFalse(report.success)

    def test_planner_executes_scroll_action(self) -> None:
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, _ = _make_agent(
            driver, answer='{"action": "scroll", "direction": "down"}'
        )
        before = len(driver._physics_driver.swipes)

        report = agent.run("open the chat named Zzz")

        self.assertGreater(len(driver._physics_driver.swipes), before)
        self.assertIn("plan:scroll", [s.action for s in report.steps])

    def test_planner_tap_action_reaches_the_device(self) -> None:
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, _ = _make_agent(
            driver, answer='{"action": "tap", "x": 111, "y": 222}'
        )

        report = agent.run("open the chat named Zzz")

        self.assertIn((111, 222), driver.taps)
        self.assertGreaterEqual(report.taps, 1)

    def test_planner_back_action(self) -> None:
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, _ = _make_agent(driver, answer='{"action": "back"}')

        report = agent.run("open the chat named Zzz")

        self.assertGreaterEqual(driver.backs, 1)

    def test_done_action_needs_verifier_agreement(self) -> None:
        """A planner claiming 'done' still has to pass verification."""
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, _ = _make_agent(driver, answer='{"action": "done"}')

        report = agent.run("open the chat named Zzz")

        # the scripted model says nothing true, so the claim is rejected
        self.assertFalse(report.success)


class TestSafetyRails(unittest.TestCase):
    def test_repeat_guard_stops_loop(self) -> None:
        driver = _FakeApexDriver(texts=["A"])
        # physics driver never changes page beyond 0, so fingerprint is stable
        agent, model = _make_agent(driver, answer="nothing useful here")
        agent.max_repeat = 2

        report = agent.run("open the chat named Zzz")

        self.assertFalse(report.success)
        self.assertIn("stuck", report.reason)

    def test_max_steps_bounds_loop(self) -> None:
        driver = _FakeApexDriver(texts=["A"])
        agent, model = _make_agent(driver, answer="nope")
        agent.max_steps = 2

        report = agent.run("open the chat named Zzz")

        self.assertFalse(report.success)
        self.assertIn("max_steps", report.reason)
        # an iteration emits at most a scroll + a plan step, plus a verify step
        # when a tap has to be confirmed, so the bound is 3x the iterations.
        self.assertLessEqual(len(report.steps), agent.max_steps * 3)

    def test_kill_file_stops_loop(self) -> None:
        import os
        import tempfile

        driver = _FakeApexDriver(texts=["A"])
        agent, model = _make_agent(driver, answer="nope")

        with tempfile.NamedTemporaryFile() as tmp:
            agent.kill_file = tmp.name
            report = agent.run("open the chat named Zzz")

        self.assertFalse(report.success)
        self.assertEqual(report.reason, "kill switch triggered")


class TestReportShape(unittest.TestCase):
    def test_report_to_dict_keys(self) -> None:
        driver = _FakeApexDriver(texts=["Team"])
        agent, _ = _make_agent(driver)
        report = agent.run("open the chat named Team")

        d = report.to_dict()
        for key in (
            "goal",
            "success",
            "steps",
            "reason",
            "appmap",
            "model_stats",
            "duration_seconds",
            "flings",
            "taps",
        ):
            self.assertIn(key, d)

    def test_report_records_flings_and_taps(self) -> None:
        driver = _FakeApexDriver(texts=["A", "B"])
        agent, _ = _make_agent(driver)
        report = agent.run("open the chat named Settings")

        self.assertGreaterEqual(report.flings, 0)
        self.assertGreaterEqual(report.taps, 0)


class TestVerification(unittest.TestCase):
    """observe -> act -> verify: a tap alone is never proof of success."""

    def test_tap_verified_by_target_name_on_new_screen(self) -> None:
        driver = _FakeApexDriver(texts=["Team", "Other"])
        agent, _ = _make_agent(driver)

        report = agent.run("open the chat named Team")

        self.assertTrue(report.success)
        self.assertIn("verified", report.reason)

    def test_no_op_tap_is_not_reported_as_success(self) -> None:
        class _Inert(_FakeApexDriver):
            def tap(self, x: int, y: int) -> dict[str, Any]:
                # records the tap but the screen never reacts
                self.taps.append((x, y))
                return {"x": x, "y": y}

        driver = _Inert(texts=["Team", "Other"])
        agent, _ = _make_agent(driver, answer='{"action": "scroll", "direction": "down"}')

        report = agent.run("open the chat named Team")

        self.assertFalse(report.success)
        self.assertGreaterEqual(len(driver.taps), 1)
        verify_steps = [s for s in report.steps if s.result == "unverified"]
        self.assertTrue(verify_steps, "an inert tap must be recorded as unverified")

    def test_verification_is_asked_when_no_name_evidence(self) -> None:
        class _Anonymous(_FakeApexDriver):
            def tap(self, x: int, y: int) -> dict[str, Any]:
                self.taps.append((x, y))
                self._navigated = True
                self._opened = "SomethingElse"  # target name is not shown
                return {"x": x, "y": y}

        driver = _Anonymous(texts=["Team"])
        agent, model = _make_agent(driver)

        report = agent.run("open the chat named Team")

        self.assertIn("verify_goal", model.purposes)
        self.assertFalse(report.success, "a rejected verification is not a success")

    def test_verifier_agreement_can_confirm_success(self) -> None:
        class _Anonymous(_FakeApexDriver):
            def tap(self, x: int, y: int) -> dict[str, Any]:
                self.taps.append((x, y))
                self._navigated = True
                self._opened = "SomethingElse"
                return {"x": x, "y": y}

        driver = _Anonymous(texts=["Team"])
        model = _ScriptedModel("")
        model.chat = _verifying_model('{"done": true}')  # type: ignore[method-assign]
        models = TieredModels(model, TierConfig(fast_model="f", big_model="b"))
        agent = ApexAgent(driver, models, max_steps=4)

        report = agent.run("open the chat named Team")

        self.assertTrue(report.success)


def _verifying_model(done_answer: str):
    """A model that always answers the verifier prompt with ``done_answer``."""

    def chat(system, user, *, model, max_tokens, temperature):
        return done_answer if "verify" in system.lower() else '{"action": "back"}'

    return chat


class TestPhysicsResultFacts(unittest.TestCase):
    def test_scroll_reports_movement_independently_of_found(self) -> None:
        physics = _FakeApexDriver(texts=["A", "B"]).physics()
        result = physics.scroll_to_text("nothing-here", max_flings=3)

        self.assertFalse(result.found)
        self.assertTrue(result.moved, "scrolling the list is movement, even without a hit")
        self.assertTrue(result.final_fingerprint)

    def test_scroll_without_movement_reports_moved_false(self) -> None:
        driver = _FakeApexDriver(texts=["A"])
        driver._physics_driver._sticky = True
        driver._physics_driver._screens = [["A"]]
        physics = driver.physics()

        result = physics.scroll_to_text("zzz", max_flings=2)

        self.assertFalse(result.found)
        self.assertFalse(result.moved)


class TestAppMapTransitions(unittest.TestCase):
    def test_tap_records_the_destination_not_the_source(self) -> None:
        driver = _FakeApexDriver(texts=["Team", "Other"])
        agent, _ = _make_agent(driver)

        agent.run("open the chat named Team")

        node = agent.map.current
        self.assertIsNotNone(node)
        assert node is not None  # narrowed for the type checker
        digest = "tap:team"
        self.assertIn(digest, node.transitions)
        self.assertTrue(node.transitions[digest].startswith("opened"))


class TestCandidateSelection(unittest.TestCase):
    def _cand(self, text: str, *, clickable: bool) -> GroundCandidate:
        return GroundCandidate(
            text=text,
            content_desc="",
            resource_id="",
            bounds=(0, 0, 100, 50),
            clickable=clickable,
        )

    def test_prefers_a_clickable_exact_match(self) -> None:
        cands = [
            self._cand("esc", clickable=False),
            self._cand("esc", clickable=True),
        ]
        hit = _find_candidate("esc", cands)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertTrue(hit.clickable)

    def test_clickable_partial_beats_non_clickable_exact(self) -> None:
        # an exact label on a dead wrapper is worse than a clickable row whose
        # label merely contains the target
        cands = [
            self._cand("esc", clickable=False),
            self._cand("esc interrupt", clickable=True),
        ]
        hit = _find_candidate("esc", cands)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertTrue(hit.clickable)
        self.assertEqual(hit.text, "esc interrupt")

    def test_falls_back_to_a_non_clickable_label(self) -> None:
        cands = [self._cand("Team", clickable=False)]
        hit = _find_candidate("team", cands)
        self.assertIsNotNone(hit)

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(_find_candidate("zzz", [self._cand("Team", clickable=True)]))

    def test_matches_resource_id_too(self) -> None:
        cand = GroundCandidate(
            text="",
            content_desc="",
            resource_id="com.app:id/send_button",
            bounds=(0, 0, 10, 10),
            clickable=True,
        )
        self.assertIsNotNone(_find_candidate("send_button", [cand]))


class TestNoTapHammering(unittest.TestCase):
    def test_a_dead_target_is_tapped_once_then_skipped(self) -> None:
        class _Inert(_FakeApexDriver):
            def tap(self, x: int, y: int) -> dict[str, Any]:
                self.taps.append((x, y))
                return {"x": x, "y": y}

        driver = _Inert(texts=["Team", "Other"])
        agent, _ = _make_agent(driver, answer='{"action": "scroll", "direction": "down"}')
        agent.max_repeat = 5

        agent.run("open the chat named Team")

        self.assertEqual(
            len(driver.taps), 1, "an inert target must not be tapped repeatedly"
        )
        self.assertIn("team", agent._failed_targets)

    def test_a_verified_target_is_not_blacklisted(self) -> None:
        driver = _FakeApexDriver(texts=["Team", "Other"])
        agent, _ = _make_agent(driver)

        report = agent.run("open the chat named Team")

        self.assertTrue(report.success)
        self.assertNotIn("team", agent._failed_targets)


class TestProtocolConformance(unittest.TestCase):
    def test_fake_satisfies_driver_protocol(self) -> None:
        d: ApexDriver = _FakeApexDriver(texts=["A"])
        self.assertEqual(d.screen_size(), (1080, 2400))


if __name__ == "__main__":
    unittest.main()
