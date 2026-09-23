from __future__ import annotations

import unittest

from agentpro.memory import MemoryStore


class MemoryCapacityTests(unittest.TestCase):
    def test_working_ring_bounded(self) -> None:
        mem = MemoryStore(working_capacity=3, summarize_threshold=100)
        for i in range(10):
            mem.record_action(i, f"tap {i}", f"sig-{i}", "ok")
        self.assertEqual(len(mem.working), 3)
        self.assertEqual(len(mem.recent_working(8)), 3)

    def test_episodic_bounded(self) -> None:
        mem = MemoryStore(episodic_capacity=2)
        for i in range(5):
            mem.record_episode(i, "event", f"e{i}")
        self.assertEqual(len(mem.episodes), 2)

    def test_failure_bounded(self) -> None:
        mem = MemoryStore(failure_capacity=3)
        for i in range(6):
            mem.record_failure(i, "tap", "ERR", "boom")
        self.assertEqual(len(mem.failures), 3)

    def test_invalid_capacities(self) -> None:
        with self.assertRaises(ValueError):
            MemoryStore(working_capacity=0)
        with self.assertRaises(ValueError):
            MemoryStore(summarize_threshold=0)


class MemoryBehaviourTests(unittest.TestCase):
    def test_subgoal_state(self) -> None:
        mem = MemoryStore()
        mem.subgoal_list = ["open", "fill", "verify"]
        mem.set_subgoal(1, 3, "fill")
        self.assertEqual(mem.subgoal_index, 1)
        self.assertEqual(mem.active_subgoal, "fill")
        self.assertEqual(mem.recent_working(2), [])

    def test_failure_history_and_context(self) -> None:
        mem = MemoryStore()
        mem.set_subgoal(0, 2, "open app")
        mem.record_failure(2, "tap", "TARGET_MISSED", "no change", "back")
        ctx = mem.build_context("install a calculator")
        self.assertIn("GOAL: install a calculator", ctx)
        self.assertIn("SUBCGOAL (1/2): open app", ctx)
        self.assertIn("TARGET_MISSED", ctx)
        self.assertIn("handled by back", ctx)

    def test_recent_failures_summary(self) -> None:
        mem = MemoryStore()
        mem.record_failure(1, "tap", "X", "detail", "back")
        lines = mem.recent_failures()
        self.assertEqual(len(lines), 1)
        self.assertIn("step 1", lines[0])

    def test_summarizer_invoked_at_threshold(self) -> None:
        seen: list[str] = []

        def summarizer(text: str) -> str:
            seen.append(text)
            return "summary"

        mem = MemoryStore(
            working_capacity=10,
            summarize_threshold=2,
            summarizer=summarizer,
        )
        mem.record_action(1, "tap 1", "s1", "ok")
        self.assertEqual(seen, [])
        mem.record_action(2, "tap 2", "s2", "ok")
        self.assertEqual(len(seen), 1)
        self.assertEqual(mem.summary, "summary")
        self.assertIn("summary", mem.build_context("goal"))

    def test_summarizer_exception_safe(self) -> None:
        def bad(_text: str) -> str:
            raise RuntimeError("boom")

        mem = MemoryStore(
            working_capacity=10,
            summarize_threshold=1,
            summarizer=bad,
        )
        mem.record_action(1, "tap", "s", "ok")  # must not raise
        self.assertEqual(mem.summary, "")

    def test_goal_evidence(self) -> None:
        mem = MemoryStore()
        mem.record_goal_evidence("shows 10")
        self.assertEqual(mem.goal_reached_evidence, ["shows 10"])

    def test_clear(self) -> None:
        mem = MemoryStore()
        mem.record_action(1, "tap", "s", "ok")
        mem.set_subgoal(0, 1, "x")
        mem.clear()
        self.assertEqual(mem.working, [])
        self.assertIsNone(mem.active_subgoal)


if __name__ == "__main__":
    unittest.main()