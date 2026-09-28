from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from agentpro.agent_runner import FakeResponse
from agentpro.agent_v2 import (
    MEASURED_GESTURE_STALL_LIMIT,
    AutonomousAgent,
    V2Limits,
)
from agentpro.llm_planner import FakeLLMClient
from agentpro.planner_v2 import PlanDecision, SubgoalPlanner
from agentpro.screen import ScreenReader
from agentpro.verification import ActionVerification, ActionVerifier


def _list_root(items: Sequence[str], *, scrollable: bool = True) -> dict:
    """A scrollable list, matching the shape measured on the device."""
    root: dict = {
        "class": "android.widget.FrameLayout",
        "package": "com.example.claude",
        "bounds": "[0,0][1080,1832]",
        "children": [
            {
                "class": "android.widget.TextView",
                "text": text,
                "bounds": f"[0,{200 + i * 150}][1080,{300 + i * 150}]",
                "clickable": i > 0,
            }
            for i, text in enumerate(items)
        ],
    }
    if scrollable:
        root["scrollable"] = True
    return root


def _hash_for(offset: int) -> str:
    """Distinct hashes for distinct list positions, as measured on device.

    Real scroll scored a Hamming distance of 32; this keeps that order of
    magnitude instead of flipping a single bit, which no threshold would catch.
    """
    value = (offset * 0x0F0F0F0F0F0F0F0F) & ((1 << 64) - 1)
    return f"{value:016x}"


class StalledScrollBridge:
    """A list that scrolls, then stops responding entirely.

    Reproduces the device session that started the investigation: the swipe
    reported success, the agent kept going, and after enough attempts the
    scrollable node was no longer in the tree at all.
    """

    def __init__(
        self,
        *,
        live_swipes: int = 3,
        disappear_after: int = 3,
        window: int = 3,
    ) -> None:
        self._live = live_swipes
        self._disappear_after = disappear_after
        self._window = window
        self.swipes = 0
        self.hash_calls = 0
        self.visual_hash_broken = False
        self.hash_requests = 0

    @property
    def offset(self) -> int:
        """Window start; frozen once the list stops responding."""
        return min(self.swipes, self._live)

    @property
    def stalled(self) -> bool:
        return self.swipes > self._live

    @property
    def scrollable_vanished(self) -> bool:
        return (
            self._disappear_after > 0
            and self.swipes >= self._live + self._disappear_after
        )

    def _tree(self) -> dict:
        start = self.offset
        return _list_root(
            [f"row {start + i}" for i in range(self._window)],
            scrollable=not self.scrollable_vanished,
        )

    # --- bridge surface -----------------------------------------------
    def health(self) -> FakeResponse:
        return FakeResponse(
            data={"server_running": True, "accessibility_connected": True}
        )

    def ui_dump(self) -> FakeResponse:
        return FakeResponse(data={"root": self._tree()})

    def command(
        self, name: str, args: Mapping[str, Any] | None = None
    ) -> FakeResponse:
        if name == "visual_hash":
            self.hash_requests += 1
            if self.visual_hash_broken:
                raise RuntimeError(
                    "SCREENSHOT_CAPTURE_FAILED: Screenshot requested too "
                    "frequently"
                )
            return FakeResponse(
                data={"hash": _hash_for(self.offset), "operation_id": self.hash_requests}
            )
        if name == "screenshot":
            return FakeResponse(
                data={"base64": "ZmFrZQ==", "width": 1080, "height": 2400}
            )
        return FakeResponse(data={"operation_id": self.hash_requests})

    def tap(self, x: float, y: float) -> FakeResponse:
        return FakeResponse(data={"operation_id": 1})

    def back(self) -> FakeResponse:
        return FakeResponse(data={"operation_id": 2})

    def input_text(self, text: str) -> FakeResponse:
        return FakeResponse(data={"operation_id": 3})

    def clear_text(self) -> FakeResponse:
        return FakeResponse(data={"operation_id": 4})

    def erase_text(self) -> FakeResponse:
        return FakeResponse(data={"operation_id": 5})

    def long_press(self, x: float, y: float, duration_ms: int = 600) -> FakeResponse:
        return FakeResponse(data={"operation_id": 6})

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration_ms: int = 300,
    ) -> FakeResponse:
        self.swipes += 1
        return FakeResponse(data={"operation_id": self.swipes})

    def get_window(self) -> FakeResponse:
        return FakeResponse(
            data={
                "package_name": "com.example.claude",
                "activity": ".Main",
                "width": 1080,
                "height": 2400,
            }
        )


class SwipeOnlyPlanner:
    """Always asks for the same scroll.

    Injected instead of a real planner so the test measures the loop's reaction
    to a swipe, not whether a model can format JSON. With an LLM in the loop
    the agent never reached the swipe at all, so the assertions below passed
    without exercising anything.
    """

    def __init__(self) -> None:
        self.decisions = 0

    def ensure_subgoals(self, goal: str) -> None:
        return None

    def replan(self, goal: str) -> None:
        return None

    def decide(
        self, context: Any, snapshot: Any, *, last_tool_result: str | None = None
    ) -> PlanDecision:
        self.decisions += 1
        return PlanDecision(
            tool="swipe",
            args={"x1": 540, "y1": 1500, "x2": 540, "y2": 400},
            thought="scroll down",
            confidence=0.9,
        )


class StalledScrollTests(unittest.TestCase):
    """The pixel channel must turn a dead swipe into a verdict, not silence."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bridge = StalledScrollBridge()
        self.reader = ScreenReader(
            self.bridge, visual_interval_s=0.0, visual_threshold=6
        )
        self.verifier = ActionVerifier()

    def _swipe_and_measure(self):
        """One swipe through the real reader/verifier path."""
        before = self.reader.observe()
        before_hash = self.reader.capture_visual(force=True)
        self.bridge.swipe(540, 1500, 540, 400)
        after = self.reader.observe()
        change = self.reader.measure_change(
            before, after, previous_visual=before_hash
        )
        verdict = self.verifier.verify("swipe", {}, True, None, before, after, change)
        return change, verdict

    def _drain_live_swipes(self) -> None:
        while not self.bridge.stalled:
            self._swipe_and_measure()

    def test_real_scroll_verifies(self) -> None:
        for _ in range(self.bridge._live):
            change, verdict = self._swipe_and_measure()
            self.assertIsNotNone(change.moved)
            self.assertTrue(change.moved, "a real scroll must move pixels")
            self.assertIsNotNone(change.distance)
            assert change.distance is not None
            self.assertGreater(change.distance, 6)
            self.assertIs(verdict, ActionVerification.VERIFIED)

    def test_stalled_swipe_fails_instead_of_looking_untouched(self) -> None:
        self._drain_live_swipes()
        for _ in range(3):
            change, verdict = self._swipe_and_measure()
            self.assertFalse(change.moved, "a dead swipe must not move pixels")
            self.assertEqual(change.distance, 0)
            self.assertIs(verdict, ActionVerification.FAILED)

    def test_stalled_swipe_stays_unknown_without_the_pixel_channel(self) -> None:
        """The tree alone cannot tell; that silence is what caused the loop."""
        self._drain_live_swipes()
        before = self.reader.observe()
        self.bridge.swipe(540, 1500, 540, 400)
        after = self.reader.observe()
        self.assertIs(
            self.verifier.verify("swipe", {}, True, None, before, after, None),
            ActionVerification.UNKNOWN,
        )

    def test_unreadable_hash_never_becomes_success_or_failure(self) -> None:
        self._drain_live_swipes()
        self.bridge.visual_hash_broken = True
        before = self.reader.observe()
        before_hash = self.reader.capture_visual(force=True)
        self.bridge.swipe(540, 1500, 540, 400)
        after = self.reader.observe()
        change = self.reader.measure_change(
            before, after, previous_visual=before_hash
        )
        self.assertIsNone(change.moved)
        self.assertIsNone(change.distance)
        self.assertFalse(change.settled, "an unknown channel is never 'settled'")
        self.assertIs(
            self.verifier.verify("swipe", {}, True, None, before, after, change),
            ActionVerification.UNKNOWN,
        )

    def test_stale_hash_is_not_replayed_as_no_change(self) -> None:
        """A capture that fails must not look like a perfectly still screen."""
        self._drain_live_swipes()
        good = self.reader.capture_visual(force=True)
        self.bridge.visual_hash_broken = True
        self.assertIsNone(self.reader.capture_visual(force=True))
        self.assertNotEqual(self.reader._last_visual_hash, None)
        self.assertEqual(self.reader._last_visual_hash, good)

    def test_agent_stops_after_three_measured_dead_swipes(self) -> None:
        """The reported symptom, with a recovery budget large enough to hide it.

        With max_recoveries=10 the old loop kept swiping until the scrollable
        node left the tree, because each Back press changed the screen and
        reset the repetition signal. The stall counter is independent of that.
        """
        # A later disappearance, so the assertion is about the agent stopping
        # early rather than about where the fake's own timeline happens to fall.
        bridge = StalledScrollBridge(live_swipes=3, disappear_after=8)
        reader = ScreenReader(bridge, visual_interval_s=0.0, visual_threshold=6)
        agent = AutonomousAgent(
            bridge,
            FakeLLMClient(responder=lambda messages: "{}"),
            model="test",
            limits=V2Limits(max_steps=60, max_recoveries=10),
            planner=cast(SubgoalPlanner, SwipeOnlyPlanner()),
            screen_reader=reader,
            action_verifier=ActionVerifier(),
        )
        report = agent.run("read the message near the end of the conversation")

        self.assertEqual(
            bridge.swipes - bridge._live,
            MEASURED_GESTURE_STALL_LIMIT,
            "must stop after exactly the stall limit, not keep swiping",
        )
        self.assertIn("did not move the screen", report.reason)
        self.assertFalse(report.success)
        self.assertFalse(
            bridge.scrollable_vanished,
            "the scrollable node must still be on screen when the agent gives up",
        )

    def test_unmeasured_swipes_are_never_counted_as_a_stall(self) -> None:
        """A hash we could not read must not stop the agent on false evidence."""
        agent = AutonomousAgent(
            self.bridge,
            FakeLLMClient(responder=lambda messages: "{}"),
            model="test",
            limits=V2Limits(max_steps=60, max_recoveries=10),
            planner=cast(SubgoalPlanner, SwipeOnlyPlanner()),
            screen_reader=self.reader,
            action_verifier=self.verifier,
        )
        self.reader.capture_visual = lambda **kwargs: None  # type: ignore[method-assign]

        report = agent.run("read the message near the end of the conversation")

        self.assertNotIn("did not move the screen", report.reason)
        self.assertGreater(
            self.bridge.swipes,
            self.bridge._live + MEASURED_GESTURE_STALL_LIMIT,
            "without a measurement the agent must not claim the screen is stuck",
        )

    def test_measured_dead_swipe_is_recorded_as_failed_in_the_trace(self) -> None:
        """The pixel channel's real effect: an honest verdict, not fewer steps.

        Measured: the swipe count is the same with and without the channel, so
        the channel is *not* what stops the loop. What it changes is the verdict
        that reaches the trace, which is what a later diagnosis is read from.
        """
        trace = Path(self._tmp.name) / "trace.jsonl"
        agent = AutonomousAgent(
            self.bridge,
            FakeLLMClient(responder=lambda messages: "{}"),
            model="test",
            limits=V2Limits(max_steps=40, max_recoveries=3),
            planner=cast(SubgoalPlanner, SwipeOnlyPlanner()),
            screen_reader=self.reader,
            action_verifier=self.verifier,
            trace_path=trace,
        )
        report = agent.run("read the message near the end of the conversation")

        events = [
            json.loads(line)
            for line in trace.read_text().splitlines()
            if line.strip()
        ]
        verdicts = [
            (event.get("payload") or {}).get("result")
            for event in events
            if event.get("event") == "v2_action_verified"
            and (event.get("payload") or {}).get("tool") == "swipe"
        ]

        self.assertGreater(self.bridge.swipes, 0, "no swipe was ever issued")
        self.assertIn(
            "FAILED",
            verdicts,
            "a swipe that provably did not move the screen must be recorded "
            "as FAILED, not as an unmeasured UNKNOWN",
        )
        self.assertNotIn(
            "VERIFIED",
            verdicts[self.bridge._live :],
            "no swipe after the list stalled may be recorded as VERIFIED",
        )
        self.assertFalse(report.success)

    def test_without_the_pixel_channel_the_dead_swipe_is_only_unknown(self) -> None:
        """Baseline: the tree alone cannot call a dead swipe. Locks in the gap."""
        self._drain_live_swipes()
        before = self.reader.observe()
        before_hash = self.reader.capture_visual(force=True)
        self.bridge.swipe(540, 1500, 540, 400)
        after = self.reader.observe()
        change = self.reader.measure_change(
            before, after, previous_visual=before_hash
        )
        measured = self.verifier.verify("swipe", {}, True, None, before, after, change)
        unmeasured = self.verifier.verify("swipe", {}, True, None, before, after, None)
        self.assertIs(measured, ActionVerification.FAILED)
        self.assertIs(unmeasured, ActionVerification.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
