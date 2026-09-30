"""Unit tests for agentpro.apex.grounder — hybrid tree-first targeting."""

from __future__ import annotations

import sys
import unittest
from typing import Any

sys.path.insert(0, "/root/AndroidAgentpro2")

from agentpro.apex.grounder import (  # noqa: E402
    GroundCandidate,
    GroundingResult,
    HybridGrounder,
    VisionGrounder,
)


def _cand(
    text: str = "",
    desc: str = "",
    rid: str = "",
    bounds: tuple[int, int, int, int] = (0, 0, 100, 100),
    clickable: bool = True,
    editable: bool = False,
) -> GroundCandidate:
    return GroundCandidate(text, desc, rid, bounds, clickable, editable)


class _ScriptedVision:
    """Vision backend returning a canned point, recording the call."""

    def __init__(self, point: tuple[int, int] | None) -> None:
        self._point = point
        self.calls: list[tuple[str, int, int]] = []

    def ground(self, screenshot_b64: str, goal: str, *, width: int, height: int) -> tuple[int, int] | None:
        self.calls.append((goal, width, height))
        return self._point


class TestTreeMatching(unittest.TestCase):
    def test_exact_text_wins(self) -> None:
        g = HybridGrounder()
        cands = [
            _cand(text="Team", bounds=(408, 401, 674, 502)),
            _cand(text="Steam", bounds=(0, 0, 10, 10)),
        ]
        hit = g.match_tree("Team", cands)

        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.text, "Team")

    def test_substring_when_no_exact(self) -> None:
        g = HybridGrounder()
        cands = [_cand(text="Open the Team chat", bounds=(0, 0, 10, 10))]
        hit = g.match_tree("Team", cands)

        self.assertIsNotNone(hit)
        assert hit is not None

    def test_content_description_matches(self) -> None:
        g = HybridGrounder()
        cands = [_cand(desc="More options", bounds=(930, 100, 1065, 258))]
        hit = g.match_tree("More options", cands)

        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.content_desc, "More options")

    def test_resource_id_matches(self) -> None:
        g = HybridGrounder()
        cands = [_cand(rid="com.app:id/send_button", bounds=(10, 10, 20, 20))]
        hit = g.match_tree("send_button", cands)

        self.assertIsNotNone(hit)

    def test_prefers_clickable_non_editable(self) -> None:
        g = HybridGrounder()
        cands = [
            _cand(text="Field", bounds=(0, 0, 50, 50), clickable=False, editable=True),
            _cand(text="Field", bounds=(0, 0, 50, 50), clickable=True, editable=False),
        ]
        hit = g.match_tree("Field", cands)

        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertTrue(hit.clickable)
        self.assertFalse(hit.editable)

    def test_no_match_returns_none(self) -> None:
        g = HybridGrounder()
        self.assertIsNone(g.match_tree("Zeta", [_cand(text="Team")]))

    def test_empty_target_returns_none(self) -> None:
        g = HybridGrounder()
        self.assertIsNone(g.match_tree("", [_cand(text="Team")]))


class TestGroundTreeFirst(unittest.TestCase):
    def test_tree_hit_never_calls_vision(self) -> None:
        vision = _ScriptedVision((9999, 9999))
        g = HybridGrounder(vision)
        cands = [_cand(text="Team", bounds=(408, 401, 674, 502))]

        result = g.ground("Team", cands, screenshot_b64="abc")

        self.assertTrue(result.found)
        self.assertEqual(result.method, "tree")
        self.assertEqual(result.center, (541, 451))
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(vision.calls, [])

    def test_missing_tree_and_no_vision_is_none(self) -> None:
        g = HybridGrounder(vision=None)
        result = g.ground("Team", [_cand(text="Other")])

        self.assertFalse(result.found)
        self.assertEqual(result.method, "none")
        self.assertEqual(result.reason, "no tree match and no vision available")


class TestVisionFallback(unittest.TestCase):
    """Vision fallback only triggers for pixel-only targets (no tree match).

    To exercise it we name the target something the tree cannot match, which is
    exactly the real-world case this layer exists for (canvas/image buttons).
    """

    def test_point_on_clickable_node_uses_it(self) -> None:
        vision = _ScriptedVision((541, 451))
        g = HybridGrounder(vision)
        cands = [_cand(text="", desc="", bounds=(408, 401, 674, 502))]

        result = g.ground("the image button", cands, screenshot_b64="abc")

        self.assertTrue(result.found)
        self.assertEqual(result.method, "vision")
        self.assertEqual(result.center, (541, 451))
        self.assertLess(result.confidence, 1.0)

    def test_point_near_node_snaps(self) -> None:
        # model points ~250px above the only clickable node; no tree match.
        vision = _ScriptedVision((541, 150))
        g = HybridGrounder(vision, snap_threshold_px=400.0)
        cands = [_cand(text="", bounds=(408, 401, 674, 502))]

        result = g.ground("the image button", cands, screenshot_b64="abc")

        self.assertTrue(result.found)
        self.assertEqual(result.method, "vision_snapped")
        self.assertEqual(result.center, (541, 451))
        self.assertEqual(result.vision_point, (541, 150))

    def test_point_far_off_beyond_snap_is_none(self) -> None:
        vision = _ScriptedVision((541, 150))
        g = HybridGrounder(vision, snap_threshold_px=10.0, max_snap_px=50.0)
        cands = [_cand(text="", bounds=(408, 401, 674, 502))]

        result = g.ground("the image button", cands, screenshot_b64="abc")

        self.assertFalse(result.found)
        self.assertEqual(result.method, "none")
        self.assertIn("beyond snap limit", result.reason)

    def test_vision_returns_none_is_miss(self) -> None:
        vision = _ScriptedVision(None)
        g = HybridGrounder(vision)

        result = g.ground("invisible target", [_cand(text="X")], screenshot_b64="abc")

        self.assertFalse(result.found)
        self.assertEqual(result.method, "none")
        self.assertEqual(result.reason, "vision returned no point")

    def test_no_clickables_is_none_even_with_point(self) -> None:
        vision = _ScriptedVision((500, 500))
        g = HybridGrounder(vision)

        result = g.ground("thing", [_cand(text="X", clickable=False)], screenshot_b64="abc")

        self.assertFalse(result.found)
        self.assertEqual(result.method, "none")

    def test_max_snap_bounds(self) -> None:
        vision = _ScriptedVision((541, 150))
        g = HybridGrounder(vision, snap_threshold_px=100.0, max_snap_px=1000.0)
        cands = [_cand(text="", bounds=(408, 401, 674, 502))]

        result = g.ground("the image button", cands, screenshot_b64="abc")

        self.assertTrue(result.found)
        self.assertEqual(result.method, "vision_snapped")


class TestStats(unittest.TestCase):
    def test_counts_tree_hit(self) -> None:
        g = HybridGrounder()
        g.ground("Team", [_cand(text="Team")])
        self.assertEqual(g.stats["tree_hits"], 1)

    def test_counts_vision_snap(self) -> None:
        vision = _ScriptedVision((541, 150))
        g = HybridGrounder(vision, snap_threshold_px=400.0)
        g.ground("the image button", [_cand(text="", bounds=(408, 401, 674, 502))], screenshot_b64="x")
        self.assertEqual(g.stats["vision_snapped"], 1)


class TestProtocolConformance(unittest.TestCase):
    def test_scripted_satisfies_protocol(self) -> None:
        v: VisionGrounder = _ScriptedVision((1, 1))
        self.assertEqual(v.ground("x", "g", width=1, height=1), (1, 1))


class TestResultShape(unittest.TestCase):
    def test_result_to_dict(self) -> None:
        r = GroundingResult(
            target="Team",
            method="tree",
            center=(541, 451),
            confidence=1.0,
            reason="ok",
            element_text="Team",
            element_bounds=(408, 401, 674, 502),
        )
        d = r.to_dict()
        self.assertEqual(d["method"], "tree")
        self.assertEqual(d["center"], [541, 451])
        self.assertTrue(r.found)

    def test_none_result_not_found(self) -> None:
        r = GroundingResult("x", "none", None, 0.0, "nope")
        self.assertFalse(r.found)
        self.assertIsNone(r.to_dict()["center"])


if __name__ == "__main__":
    unittest.main()
