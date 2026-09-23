from __future__ import annotations

import unittest

from agentpro import ActionType, AgentAction
from agentpro.meta_planner import action_type_counts


class NewActionTypesTests(unittest.TestCase):
    def test_new_members_exist(self) -> None:
        self.assertEqual(ActionType.INPUT_TEXT.value, "input_text")
        self.assertEqual(ActionType.SWIPE.value, "swipe")
        self.assertEqual(ActionType.OPEN_URL.value, "open_url")
        self.assertEqual(ActionType.KEY_EVENT.value, "key_event")

    def test_new_members_are_distinct(self) -> None:
        new = {
            ActionType.INPUT_TEXT,
            ActionType.SWIPE,
            ActionType.OPEN_URL,
            ActionType.KEY_EVENT,
        }
        existing = {
            ActionType.OPEN_APP,
            ActionType.TAP,
            ActionType.BACK,
            ActionType.SCREENSHOT,
            ActionType.UI_DUMP,
            ActionType.WAIT,
            ActionType.FINISH,
            ActionType.INSTALL_APK,
        }
        self.assertEqual(new & existing, set())

    def test_agent_action_accepts_new_types(self) -> None:
        for action_type, args in (
            (ActionType.INPUT_TEXT, {"text": "hi"}),
            (ActionType.SWIPE, {"x1": 1, "y1": 2, "x2": 3, "y2": 4}),
            (ActionType.OPEN_URL, {"url": "https://x.com"}),
            (ActionType.KEY_EVENT, {"keycode": "home"}),
        ):
            action = AgentAction(action_type, args)
            self.assertIs(action.action_type, action_type)
            self.assertEqual(dict(action.arguments), args)

    def test_action_type_counts_handles_new_types(self) -> None:
        actions = [
            AgentAction(ActionType.INPUT_TEXT, {"text": "a"}),
            AgentAction(ActionType.INPUT_TEXT, {"text": "b"}),
            AgentAction(ActionType.SWIPE),
            AgentAction(ActionType.OPEN_URL),
        ]
        counts = action_type_counts(actions)
        self.assertEqual(counts[ActionType.INPUT_TEXT], 2)
        self.assertEqual(counts[ActionType.SWIPE], 1)
        self.assertEqual(counts[ActionType.OPEN_URL], 1)


if __name__ == "__main__":
    unittest.main()
