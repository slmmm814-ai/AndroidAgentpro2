from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agentpro.budgets import (
    BudgetExceededError,
    BudgetLimits,
    BudgetTracker,
    KillSwitch,
    KillSwitchEngaged,
    LoopGuard,
    check_repeated_actions,
    signature_for,
)


class BudgetLimitsTests(unittest.TestCase):
    def test_validation(self) -> None:
        with self.assertRaises(TypeError):
            BudgetLimits(max_steps=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            BudgetLimits(max_steps=0)
        with self.assertRaises(ValueError):
            BudgetLimits(max_wall_seconds=0)
        with self.assertRaises(TypeError):
            BudgetLimits(max_wall_seconds=True)  # type: ignore[arg-type]

    def test_ok_limits(self) -> None:
        limits = BudgetLimits(max_steps=5, max_wall_seconds=None)
        self.assertIsNone(limits.max_wall_seconds)


class BudgetTrackerTests(unittest.TestCase):
    def test_steps(self) -> None:
        tracker = BudgetTracker(BudgetLimits(max_steps=2))
        tracker.start()
        tracker.mark_step()
        tracker.mark_step()
        with self.assertRaises(BudgetExceededError):
            tracker.mark_step()

    def test_actions(self) -> None:
        tracker = BudgetTracker(BudgetLimits(max_actions=1))
        tracker.start()
        tracker.mark_action()
        with self.assertRaises(BudgetExceededError):
            tracker.mark_action()

    def test_model_calls(self) -> None:
        tracker = BudgetTracker(BudgetLimits(max_model_calls=1))
        tracker.start()
        tracker.mark_model_call()
        with self.assertRaises(BudgetExceededError):
            tracker.mark_model_call()

    def test_wall_clock(self) -> None:
        now = [0.0]

        def clock() -> float:
            return now[0]

        tracker = BudgetTracker(
            BudgetLimits(max_wall_seconds=10), clock=clock
        )
        tracker.start()
        now[0] = 11.0
        with self.assertRaises(BudgetExceededError):
            tracker.ensure()

    def test_not_started(self) -> None:
        tracker = BudgetTracker(BudgetLimits())
        self.assertFalse(tracker.started)
        self.assertEqual(tracker.elapsed, 0.0)
        tracker.start()
        self.assertTrue(tracker.started)
        self.assertGreaterEqual(tracker.elapsed, 0.0)


class LoopGuardTests(unittest.TestCase):
    def test_repeat_detection(self) -> None:
        guard = LoopGuard(repeat_threshold=3)
        guard.record("s")
        guard.record("s")
        self.assertFalse(guard.is_repeating())
        guard.record("s")
        self.assertTrue(guard.is_repeating())

    def test_no_false_positive(self) -> None:
        guard = LoopGuard(repeat_threshold=3)
        for sig in ("a", "b", "c"):
            guard.record(sig)
        self.assertFalse(guard.is_repeating())

    def test_window_bounded(self) -> None:
        guard = LoopGuard(repeat_threshold=2, window=3)
        for sig in range(5):
            guard.record(str(sig))
        self.assertEqual(len(guard.most_recent()), 3)

    def test_longest_run(self) -> None:
        guard = LoopGuard()
        for sig in ("a", "a", "a", "b", "b"):
            guard.record(sig)
        self.assertEqual(guard.longest_run(), 3)

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            LoopGuard(repeat_threshold=0)
        with self.assertRaises(ValueError):
            LoopGuard(window=0)
        guard = LoopGuard()
        with self.assertRaises(ValueError):
            guard.record("")

    def test_check_repeated_actions(self) -> None:
        self.assertTrue(check_repeated_actions(["x", "x", "x"], threshold=3))
        self.assertFalse(check_repeated_actions(["x", "x"], threshold=3))
        self.assertFalse(check_repeated_actions(["a", "b", "c"], threshold=3))


class KillSwitchTests(unittest.TestCase):
    def test_flag(self) -> None:
        switch = KillSwitch(flag=False)
        self.assertFalse(switch.is_engaged())
        switch.engage()
        self.assertTrue(switch.is_engaged())
        switch.disarm()
        self.assertFalse(switch.is_engaged())

    def test_env_var(self) -> None:
        os.environ["AGENTPRO_KILL_TEST"] = "1"
        switch = KillSwitch(
            env_var="AGENTPRO_KILL_TEST", kill_file=None, flag=False
        )
        self.assertTrue(switch.is_engaged())
        del os.environ["AGENTPRO_KILL_TEST"]
        self.assertFalse(switch.is_engaged())

    def test_kill_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kill_file = Path(tmp) / "KILL"
            switch = KillSwitch(kill_file=str(kill_file))
            self.assertFalse(switch.is_engaged())
            kill_file.touch()
            self.assertTrue(switch.is_engaged())


class SignatureTests(unittest.TestCase):
    def test_signature_stable_and_distinct(self) -> None:
        a = signature_for({"tool": "tap", "args": {"x": 1}}, "fp1")
        b = signature_for({"tool": "tap", "args": {"x": 1}}, "fp1")
        self.assertEqual(a, b)
        self.assertNotEqual(
            a, signature_for({"tool": "tap", "args": {"x": 2}}, "fp1")
        )
        self.assertNotEqual(
            a, signature_for({"tool": "tap", "args": {"x": 1}}, "fp2")
        )

    def test_signature_uses_key_fields(self) -> None:
        by_id = signature_for(
            {"tool": "open_app", "args": {"package": "com.x"}}, None
        )
        by_xy = signature_for({"tool": "tap", "args": {"x": 9}}, None)
        self.assertIn("open_app:com.x", by_id)
        self.assertIn("tap:9", by_xy)


if __name__ == "__main__":
    unittest.main()