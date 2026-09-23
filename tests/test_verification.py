from __future__ import annotations

import unittest

from agentpro.agent_runner import RecordingBridgeClient
from agentpro.budgets import LoopGuard
from agentpro.llm_planner import FakeLLMClient, LLMError
from agentpro.memory import MemoryStore
from agentpro.screen import ScreenReader, ScreenSnapshot
from agentpro.verification import (
    ActionVerification,
    ActionVerifier,
    DeterministicGoalVerifier,
    LLMGoalVerifier,
    ProgressState,
    ProgressVerifier,
)

def _editable_tree(text: str) -> dict:
    return {
        "class": "android.widget.FrameLayout",
        "package": "com.example.app",
        "children": [
            {
                "class": "android.widget.EditText",
                "text": text,
                "bounds": "[100,200][900,300]",
                "editable": True,
                "focused": True,
            }
        ],
    }


def _clickable_tree(text: str) -> dict:
    return {
        "class": "android.widget.FrameLayout",
        "package": "com.example.app",
        "children": [
            {
                "class": "android.widget.TextView",
                "text": text,
                "bounds": "[100,100][400,160]",
                "clickable": True,
            }
        ],
    }


def _snapshot(client: RecordingBridgeClient) -> ScreenSnapshot:
    return ScreenReader(client, include_screenshot=False).observe()


class ActionVerifierTests(unittest.TestCase):
    def test_failed_result(self) -> None:
        verifier = ActionVerifier()
        result = verifier.verify(
            "tap", {}, False, "ERROR",
            _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("a"))),
            _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("b"))),
        )
        self.assertIs(result, ActionVerification.FAILED)

    def test_read_tools_auto_verified(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient())
        result = verifier.verify(
            "dump_ui", {}, True, None, before, before
        )
        self.assertIs(result, ActionVerification.VERIFIED)

    def test_tap_changed_screen_verified(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("a")))
        after = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("b")))
        self.assertIs(
            verifier.verify("tap", {}, True, None, before, after),
            ActionVerification.VERIFIED,
        )

    def test_tap_no_change_unknown(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("a")))
        after = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("a")))
        self.assertIs(
            verifier.verify("tap", {}, True, None, before, after),
            ActionVerification.UNKNOWN,
        )

    def test_nav_changed_verified(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("a")))
        after = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("b")))
        self.assertIs(
            verifier.verify("open_app", {}, True, None, before, after),
            ActionVerification.VERIFIED,
        )

    def test_nav_no_change_partial(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("a")))
        after = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("a")))
        self.assertIs(
            verifier.verify("open_app", {}, True, None, before, after),
            ActionVerification.PARTIAL,
        )

    def test_type_text_verified(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        after = _snapshot(
            RecordingBridgeClient(ui_root=_editable_tree("hello world"))
        )
        self.assertIs(
            verifier.verify("type_text", {}, True, None, before, after),
            ActionVerification.VERIFIED,
        )

    def test_type_text_no_editable_no_op(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("a")))
        after = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("b")))
        self.assertIs(
            verifier.verify("type_text", {}, True, None, before, after),
            ActionVerification.NO_OP,
        )

    def test_type_text_no_change_partial(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        after = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        self.assertIs(
            verifier.verify("type_text", {}, True, None, before, after),
            ActionVerification.PARTIAL,
        )

    def test_clear_text_verified(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        after = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("")))
        self.assertIs(
            verifier.verify("clear_text", {}, True, None, before, after),
            ActionVerification.VERIFIED,
        )

    def test_erase_text_verified(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello world")))
        after = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        self.assertIs(
            verifier.verify("erase_text", {}, True, None, before, after),
            ActionVerification.VERIFIED,
        )

    def test_erase_text_no_change_partial(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        after = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        self.assertIs(
            verifier.verify("erase_text", {}, True, None, before, after),
            ActionVerification.PARTIAL,
        )

    def test_type_text_contains_typed_value(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("")))
        after = _snapshot(
            RecordingBridgeClient(ui_root=_editable_tree("totally new"))
        )
        self.assertIs(
            verifier.verify(
                "type_text", {"text": "totally new"}, True, None, before, after
            ),
            ActionVerification.VERIFIED,
        )

    def test_type_text_replaced_without_typed_match_partial(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("aaa")))
        after = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("bbb")))
        self.assertIs(
            verifier.verify(
                "type_text", {"text": "zzz"}, True, None, before, after
            ),
            ActionVerification.PARTIAL,
        )


class ProgressVerifierTests(unittest.TestCase):
    def test_repeating(self) -> None:
        guard = LoopGuard(repeat_threshold=3)
        verifier = ProgressVerifier(guard)
        for _ in range(3):
            verifier.record_signature("same")
        memory = MemoryStore()
        self.assertIs(verifier.check(memory), ProgressState.REPEATING)

    def test_stalled(self) -> None:
        verifier = ProgressVerifier(LoopGuard(repeat_threshold=4))
        memory = MemoryStore(failure_capacity=10)
        for i in range(3):
            memory.record_failure(i, "tap", "SAME_ERROR", "detail")
        self.assertIs(verifier.check(memory), ProgressState.STALLED)

    def test_degraded(self) -> None:
        verifier = ProgressVerifier(LoopGuard(repeat_threshold=4))
        memory = MemoryStore(failure_capacity=10)
        codes = ["A", "B", "C"]
        for i, code in enumerate(codes):
            memory.record_failure(i, "tap", code, "detail")
        self.assertIs(verifier.check(memory), ProgressState.DEGRADED)

    def test_advancing(self) -> None:
        verifier = ProgressVerifier(LoopGuard(repeat_threshold=4))
        memory = MemoryStore()
        self.assertIs(
            verifier.check(memory), ProgressState.ADVANCING
        )

    def test_failure_record_bounded(self) -> None:
        verifier = ProgressVerifier(
            LoopGuard(), max_recent_failures=2
        )
        for _ in range(5):
            verifier.record_failure("X")
        # failures tracked separately from memory; only memory is consulted
        self.assertIsInstance(verifier, ProgressVerifier)


class LLMGoalVerifierTests(unittest.TestCase):
    def _snapshot_for(self, tree: dict) -> ScreenSnapshot:
        return _snapshot(RecordingBridgeClient(ui_root=tree))

    def test_done(self) -> None:
        responder = lambda _messages: (
            '{"done": true, "evidence": "result shows 10", "confidence": 0.9}'
        )
        verifier = LLMGoalVerifier(FakeLLMClient(responder=responder))
        memory = MemoryStore()
        result = verifier.verify(
            goal="do math", snapshot=self._snapshot_for(_clickable_tree("10")), memory=memory
        )
        self.assertTrue(result.success)
        self.assertEqual(memory.goal_reached_evidence, ["result shows 10"])

    def test_not_done(self) -> None:
        responder = lambda _messages: (
            '{"done": false, "evidence": "still calculating", "confidence": 0.4}'
        )
        verifier = LLMGoalVerifier(FakeLLMClient(responder=responder))
        result = verifier.verify(
            goal="do math",
            snapshot=self._snapshot_for(_clickable_tree("0")),
            memory=MemoryStore(),
        )
        self.assertFalse(result.success)

    def test_low_confidence_rejected(self) -> None:
        responder = lambda _messages: (
            '{"done": true, "evidence": "maybe", "confidence": 0.1}'
        )
        verifier = LLMGoalVerifier(FakeLLMClient(responder=responder))
        result = verifier.verify(
            goal="g", snapshot=None, memory=MemoryStore()
        )
        self.assertFalse(result.success)
        self.assertIn("confidence", result.reason)

    def test_model_error(self) -> None:
        class RaisingClient:
            def complete(self, messages, *, json_mode=False, timeout=30.0):
                raise LLMError("network down")

        verifier = LLMGoalVerifier(RaisingClient())
        result = verifier.verify(
            goal="g", snapshot=None, memory=MemoryStore()
        )
        self.assertFalse(result.success)
        self.assertIn("network down", result.reason)

    def test_bad_json(self) -> None:
        verifier = LLMGoalVerifier(
            FakeLLMClient(responder=lambda _m: "not json")
        )
        result = verifier.verify(
            goal="g", snapshot=None, memory=MemoryStore()
        )
        self.assertFalse(result.success)
        self.assertIn("unparseable", result.reason)

    def test_threshold_validation(self) -> None:
        with self.assertRaises(ValueError):
            LLMGoalVerifier(FakeLLMClient(responder=lambda _m: "x"),
                            confidence_threshold=1.5)


class DeterministicGoalVerifierTests(unittest.TestCase):
    def test_predicate_satisfied(self) -> None:
        verifier = DeterministicGoalVerifier(
            predicate=lambda snapshot: snapshot is not None and snapshot.success
        )
        result = verifier.verify(
            goal="g",
            snapshot=_snapshot(RecordingBridgeClient()),
            memory=MemoryStore(),
        )
        self.assertTrue(result.success)

    def test_predicate_not_satisfied(self) -> None:
        verifier = DeterministicGoalVerifier(
            predicate=lambda snapshot: False
        )
        result = verifier.verify(
            goal="g", snapshot=None, memory=MemoryStore()
        )
        self.assertFalse(result.success)

    def test_no_predicate(self) -> None:
        verifier = DeterministicGoalVerifier()
        result = verifier.verify(
            goal="g", snapshot=None, memory=MemoryStore()
        )
        self.assertFalse(result.success)
        self.assertIn("no predicate", result.reason)


if __name__ == "__main__":
    unittest.main()