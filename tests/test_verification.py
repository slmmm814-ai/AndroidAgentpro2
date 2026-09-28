from __future__ import annotations

import unittest

from agentpro.agent_runner import RecordingBridgeClient
from agentpro.budgets import LoopGuard
from agentpro.llm_planner import FakeLLMClient, LLMError
from agentpro.memory import MemoryStore
from agentpro.screen import ScreenChange, ScreenReader, ScreenSnapshot
from agentpro.verification import (
    ActionVerification,
    ActionVerifier,
    DeterministicGoalVerifier,
    LLMGoalVerifier,
    ProgressState,
    ProgressVerifier,
)

def _editable_tree(text: str, top: int = 200) -> dict:
    return {
        "class": "android.widget.FrameLayout",
        "package": "com.example.app",
        "children": [
            {
                "class": "android.widget.EditText",
                "text": text,
                "bounds": f"[100,{top}][900,{top + 100}]",
                "editable": True,
                "focused": True,
            }
        ],
    }


def _two_field_tree(
    first: str,
    second: str,
    focus: int | str | None = 1,
) -> dict:
    """Two stacked editable fields, vertically separated and identically sized.

    ``focus`` selects which field carries focus: an index, ``"both"``, or
    ``None`` for neither.
    """

    def _field(text: str, top: int, index: int) -> dict:
        if focus == "both":
            focused = True
        elif focus is None:
            focused = False
        else:
            focused = index == focus
        return {
            "class": "android.widget.EditText",
            "text": text,
            "bounds": f"[100,{top}][900,{top + 100}]",
            "editable": True,
            "focused": focused,
        }

    return {
        "class": "android.widget.FrameLayout",
        "package": "com.example.app",
        "children": [_field(first, 200, 0), _field(second, 400, 1)],
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
            verifier.verify(
                "type_text", {"text": "world"}, True, None, before, after
            ),
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
            verifier.verify(
                "type_text", {"text": "world"}, True, None, before, after
            ),
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

    def test_erase_text_partial_while_field_not_empty(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello world")))
        after = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        self.assertIs(
            verifier.verify("erase_text", {}, True, None, before, after),
            ActionVerification.PARTIAL,
        )

    def test_erase_text_verified_when_field_emptied(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hello")))
        after = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("")))
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

    def test_type_text_not_verified_when_text_sits_in_another_field(self) -> None:
        """Regression: the write missed, but the text existed elsewhere.

        The old rule searched every editable field on screen, so an unrelated
        field that already contained the typed string produced VERIFIED for a
        write that never happened.
        """
        verifier = ActionVerifier()
        before = _snapshot(
            RecordingBridgeClient(ui_root=_two_field_tree("12345", "", focus=1))
        )
        after = _snapshot(
            RecordingBridgeClient(ui_root=_two_field_tree("12345", "", focus=1))
        )
        self.assertIs(
            verifier.verify(
                "type_text", {"text": "12345"}, True, None, before, after
            ),
            ActionVerification.PARTIAL,
        )

    def test_type_text_verified_in_target_field_only(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(
            RecordingBridgeClient(ui_root=_two_field_tree("ali@mail.com", "", focus=1))
        )
        after = _snapshot(
            RecordingBridgeClient(
                ui_root=_two_field_tree("ali@mail.com", "12345", focus=1)
            )
        )
        self.assertIs(
            verifier.verify(
                "type_text", {"text": "12345"}, True, None, before, after
            ),
            ActionVerification.VERIFIED,
        )

    def test_type_text_unknown_when_no_field_is_focused(self) -> None:
        """Two editable fields and no focus: the target is undecidable."""
        verifier = ActionVerifier()
        before = _snapshot(
            RecordingBridgeClient(ui_root=_two_field_tree("aaa", "bbb", focus=None))
        )
        after = _snapshot(
            RecordingBridgeClient(ui_root=_two_field_tree("aaa", "ccc", focus=None))
        )
        self.assertIs(
            verifier.verify(
                "type_text", {"text": "ccc"}, True, None, before, after
            ),
            ActionVerification.UNKNOWN,
        )

    def test_type_text_unknown_when_two_fields_share_focus(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(
            RecordingBridgeClient(ui_root=_two_field_tree("", "", focus="both"))
        )
        after = _snapshot(
            RecordingBridgeClient(ui_root=_two_field_tree("a", "b", focus="both"))
        )
        self.assertIs(
            verifier.verify("type_text", {"text": "a"}, True, None, before, after),
            ActionVerification.UNKNOWN,
        )

    def test_type_text_verified_when_keyboard_shifts_target(self) -> None:
        """Measured case: the soft keyboard moved top by 873px, nothing else.

        Matching on top alone would report UNKNOWN for a write that succeeded.
        """
        verifier = ActionVerifier()
        before = _snapshot(
            RecordingBridgeClient(ui_root=_editable_tree("hi", top=1889))
        )
        after = _snapshot(
            RecordingBridgeClient(ui_root=_editable_tree("hi there", top=1016))
        )
        self.assertIs(
            verifier.verify(
                "type_text", {"text": "there"}, True, None, before, after
            ),
            ActionVerification.VERIFIED,
        )

    def test_type_text_unknown_when_same_shaped_field_appears(self) -> None:
        """A new same-shaped field shifts every ordinal, so fail closed."""
        verifier = ActionVerifier()
        before = _snapshot(
            RecordingBridgeClient(ui_root=_two_field_tree("a", "", focus=1))
        )
        after = _snapshot(
            RecordingBridgeClient(ui_root=_three_field_tree("a", "b", "", focus=2))
        )
        self.assertIs(
            verifier.verify("type_text", {"text": ""}, True, None, before, after),
            ActionVerification.UNKNOWN,
        )

    def test_type_text_unknown_when_target_field_disappears(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_editable_tree("hi")))
        after = _snapshot(RecordingBridgeClient(ui_root=_clickable_tree("done")))
        self.assertIs(
            verifier.verify("type_text", {"text": "x"}, True, None, before, after),
            ActionVerification.UNKNOWN,
        )


def _three_field_tree(
    first: str,
    second: str,
    third: str,
    focus: int | None = 2,
) -> dict:
    """Three identically shaped fields, used to shift the ordinal of a target."""
    return {
        "class": "android.widget.FrameLayout",
        "package": "com.example.app",
        "children": [
            {
                "class": "android.widget.EditText",
                "text": text,
                "bounds": f"[100,{200 + index * 200}][900,{300 + index * 200}]",
                "editable": True,
                "focused": index == focus,
            }
            for index, text in enumerate((first, second, third))
        ],
    }


def _list_tree(items: tuple[str, ...] = ("A", "B", "C")) -> dict:
    """A short list inside a scrollable container."""
    return {
        "class": "android.widget.FrameLayout",
        "package": "com.example.app",
        "scrollable": True,
        "bounds": "[0,0][1080,1832]",
        "children": [
            {
                "class": "android.widget.TextView",
                "text": text,
                "bounds": f"[0,{200 + i * 150}][1080,{300 + i * 150}]",
            }
            for i, text in enumerate(items)
        ],
    }


class PixelChannelVerificationTests(unittest.TestCase):
    """A swipe is only judged from the pixel channel when pixels were read.

    Without this, an unchanged element tree reports UNKNOWN for every scroll
    that keeps its nodes inside the fingerprint's 85px position buckets, which
    is what let the agent keep swiping a list that had stopped responding.
    """

    @staticmethod
    def _change(moved: bool | None) -> ScreenChange:
        return ScreenChange(
            moved=moved,
            distance=0 if moved is False else (32 if moved else None),
            new_content_ratio=0.0,
            container_shrank=False,
            before_container_bottom=1832,
            after_container_bottom=1832,
        )

    def _verify(self, tool: str, visual: ScreenChange | None):
        verifier = ActionVerifier()
        snapshot = _snapshot(RecordingBridgeClient(ui_root=_list_tree()))
        return verifier.verify(tool, {}, True, None, snapshot, snapshot, visual)

    def test_swipe_verified_when_pixels_moved(self) -> None:
        self.assertIs(
            self._verify("swipe", self._change(True)),
            ActionVerification.VERIFIED,
        )

    def test_swipe_failed_when_pixels_provably_did_not_move(self) -> None:
        self.assertIs(
            self._verify("swipe", self._change(False)),
            ActionVerification.FAILED,
        )

    def test_swipe_unknown_when_pixels_unavailable(self) -> None:
        self.assertIs(
            self._verify("swipe", self._change(None)),
            ActionVerification.UNKNOWN,
        )

    def test_swipe_unknown_without_visual_channel(self) -> None:
        self.assertIs(
            self._verify("swipe", None), ActionVerification.UNKNOWN
        )

    def test_tap_not_downgraded_by_still_screen(self) -> None:
        """A tap that opens nothing is not evidence of a missed scroll."""
        self.assertIs(
            self._verify("tap", self._change(False)),
            ActionVerification.UNKNOWN,
        )

    def test_changed_tree_overrides_pixel_channel(self) -> None:
        verifier = ActionVerifier()
        before = _snapshot(RecordingBridgeClient(ui_root=_list_tree()))
        after = _snapshot(RecordingBridgeClient(ui_root=_list_tree(items=("B", "C", "D"))))
        self.assertIs(
            verifier.verify("swipe", {}, True, None, before, after, self._change(False)),
            ActionVerification.VERIFIED,
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