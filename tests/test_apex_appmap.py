"""Unit tests for agentpro.apex.appmap — app-state graph + scroll memory."""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "/root/AndroidAgentpro2")

from agentpro.apex.appmap import AppMap, ScreenNode, ScrollFacts  # noqa: E402


class TestObservation(unittest.TestCase):
    def test_observe_creates_and_counts_visits(self) -> None:
        m = AppMap()
        n1 = m.observe("fp-A", package="com.x", now=1.0)

        self.assertEqual(m.size, 1)
        self.assertEqual(n1.visit_count, 1)
        self.assertEqual(m.current, n1)

        n2 = m.observe("fp-A", package="com.x", now=2.0)
        self.assertIs(n1, n2)
        self.assertEqual(n2.visit_count, 2)
        self.assertEqual(n2.last_seen, 2.0)
        self.assertEqual(m.size, 1)

    def test_observe_replaces_texts_and_keeps_first_seen(self) -> None:
        m = AppMap()
        m.observe("fp-A", visible_texts=("a",), now=1.0)
        m.observe("fp-A", visible_texts=("b", "c"), now=2.0)

        node = m.current
        assert node is not None
        self.assertEqual(node.visible_texts, ("b", "c"))
        self.assertEqual(node.first_seen, 1.0)

    def test_membership_and_iteration(self) -> None:
        m = AppMap()
        m.observe("fp-A")
        m.observe("fp-B")

        self.assertIn("fp-A", m)
        self.assertNotIn("fp-Z", m)
        self.assertEqual(len(m), 2)
        self.assertEqual(sorted(n.fingerprint for n in m), ["fp-A", "fp-B"])


class TestAnchor(unittest.TestCase):
    def test_anchor_defaults_to_current(self) -> None:
        m = AppMap()
        m.observe("fp-A")
        m.mark_anchor()

        anchor = m.anchor
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertEqual(anchor.fingerprint, "fp-A")

    def test_anchor_explicit_and_reset(self) -> None:
        m = AppMap()
        m.observe("fp-A")
        m.observe("fp-B")
        m.mark_anchor("fp-A")

        anchor = m.anchor
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertEqual(anchor.fingerprint, "fp-A")

        m.reset_anchor()
        self.assertIsNone(m.anchor)


class TestScrollMemory(unittest.TestCase):
    def test_downward_fling_that_moved_marks_not_bottom(self) -> None:
        m = AppMap()
        m.observe("fp-list")
        facts = m.record_scroll("down", moved=True)

        self.assertFalse(facts.reached_bottom)
        self.assertEqual(facts.max_depth_seen, 1)

    def test_downward_fling_that_stopped_marks_bottom(self) -> None:
        m = AppMap()
        m.observe("fp-list")
        m.record_scroll("down", moved=True)
        facts = m.record_scroll("down", moved=False)

        self.assertTrue(facts.reached_bottom)
        self.assertEqual(facts.consecutive_no_move, 1)

    def test_upward_fling_that_stopped_marks_top(self) -> None:
        m = AppMap()
        m.observe("fp-list")
        m.record_scroll("up", moved=True)
        facts = m.record_scroll("up", moved=False)

        self.assertTrue(facts.reached_top)

    def test_reached_bottom_query(self) -> None:
        m = AppMap()
        m.observe("fp-list")
        self.assertFalse(m.reached_bottom())

        m.record_scroll("down", moved=False)
        self.assertTrue(m.reached_bottom())
        self.assertTrue(m.reached_bottom("fp-list"))
        self.assertFalse(m.reached_bottom("fp-other"))

    def test_record_scroll_without_current_returns_empty_facts(self) -> None:
        m = AppMap()
        facts = m.record_scroll("down", moved=True)
        self.assertFalse(facts.reached_bottom)

    def test_movement_after_dead_end_clears_flag(self) -> None:
        m = AppMap()
        m.observe("fp-list")
        m.record_scroll("down", moved=False)
        self.assertTrue(m.reached_bottom())

        facts = m.record_scroll("up", moved=True)
        self.assertFalse(facts.reached_bottom)


class TestTransitions(unittest.TestCase):
    def test_records_and_reads_transition(self) -> None:
        m = AppMap()
        m.observe("fp-home")
        m.record_transition("tap:Settings", "fp-settings")

        self.assertEqual(m.known_transition("tap:Settings"), "fp-settings")
        self.assertIsNone(m.known_transition("tap:Nope"))

    def test_known_transition_without_current_is_none(self) -> None:
        m = AppMap()
        self.assertIsNone(m.known_transition("tap:X"))

    def test_visited_counts(self) -> None:
        m = AppMap()
        m.observe("fp-A")
        m.observe("fp-A")
        m.observe("fp-B")

        self.assertEqual(m.visited("fp-A"), 2)
        self.assertEqual(m.visited("fp-B"), 1)
        self.assertEqual(m.visited("fp-Z"), 0)


class TestSerialisation(unittest.TestCase):
    def test_to_dict_roundtrip_shape(self) -> None:
        m = AppMap()
        m.observe("fp-A", package="com.x", visible_texts=("Hello",), now=1.0)
        m.mark_anchor()
        m.record_scroll("down", moved=False)

        d = m.to_dict()

        self.assertEqual(d["size"], 1)
        self.assertEqual(d["current"], "fp-A")
        self.assertEqual(d["anchor"], "fp-A")
        self.assertIn("fp-A", d["nodes"])
        node = d["nodes"]["fp-A"]
        self.assertEqual(node["package"], "com.x")
        self.assertTrue(node["scroll"]["reached_bottom"])
        self.assertEqual(node["visible_texts"], ["Hello"])


class testDataclasses(unittest.TestCase):
    def test_scroll_facts_defaults(self) -> None:
        f = ScrollFacts()
        self.assertTrue(f.reached_top)
        self.assertFalse(f.reached_bottom)
        self.assertIsNone(f.last_direction)

    def test_screen_node_key(self) -> None:
        n = ScreenNode(fingerprint="fp-X")
        self.assertEqual(n.key, "fp-X")
        self.assertEqual(n.visit_count, 0)


if __name__ == "__main__":
    unittest.main()
