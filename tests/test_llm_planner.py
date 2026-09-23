from __future__ import annotations

import json
import unittest

from agentpro.llm_planner import (
    LLMError,
    LLMPlanner,
    _summarize_ui_tree,
    _extract_json,
)
from agentpro.llm_planner import FakeLLMClient
from agentpro.models import AgentAction, AgentContext, ActionType, Observation


def _ctx(goal: str = "test") -> AgentContext:
    return AgentContext(goal=goal)


def _obs(**kw) -> Observation:
    return Observation(success=True, **kw)


def _resp(action: str, args: dict | None = None, *, done=False, confidence=0.8, thought="t") -> str:
    return json.dumps(
        {
            "thought": thought,
            "action": action,
            "args": args or {},
            "done": done,
            "confidence": confidence,
        }
    )


class LLMPlannerActionTests(unittest.TestCase):
    def _plan(self, script: list[str]) -> list:
        planner = LLMPlanner(FakeLLMClient(script=script))
        cands = planner.generate(_ctx("g"), _obs())
        self.assertEqual(len(cands), 1)
        return list(cands[0].actions)

    def test_tap(self) -> None:
        actions = self._plan([_resp("tap", {"x": 5, "y": 9})])
        self.assertEqual(actions[0].action_type, ActionType.TAP)
        self.assertAlmostEqual(actions[0].arguments["x"], 5.0)
        self.assertAlmostEqual(actions[0].arguments["y"], 9.0)

    def test_input_text(self) -> None:
        actions = self._plan([_resp("input_text", {"text": "hello"})])
        self.assertEqual(actions[0].action_type, ActionType.INPUT_TEXT)
        self.assertEqual(actions[0].arguments["text"], "hello")

    def test_swipe(self) -> None:
        actions = self._plan(
            [_resp("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 500})]
        )
        self.assertEqual(actions[0].action_type, ActionType.SWIPE)
        self.assertEqual(actions[0].arguments["duration_ms"], 500)

    def test_open_url(self) -> None:
        actions = self._plan([_resp("open_url", {"url": "https://x.com"})])
        self.assertEqual(actions[0].action_type, ActionType.OPEN_URL)
        self.assertEqual(actions[0].arguments["url"], "https://x.com")

    def test_key_event(self) -> None:
        actions = self._plan([_resp("key_event", {"keycode": "home"})])
        self.assertEqual(actions[0].action_type, ActionType.KEY_EVENT)
        self.assertEqual(actions[0].arguments["keycode"], "home")

    def test_open_app_alias(self) -> None:
        actions = self._plan([_resp("launch_app", {"package": "com.x"})])
        self.assertEqual(actions[0].action_type, ActionType.OPEN_APP)
        self.assertEqual(actions[0].arguments["package"], "com.x")

    def test_wait(self) -> None:
        actions = self._plan([_resp("wait", {"seconds": 2})])
        self.assertEqual(actions[0].action_type, ActionType.WAIT)
        self.assertAlmostEqual(actions[0].arguments["seconds"], 2.0)

    def test_screenshot(self) -> None:
        actions = self._plan([_resp("screenshot", {})])
        self.assertEqual(actions[0].action_type, ActionType.SCREENSHOT)

    def test_done_returns_finish(self) -> None:
        actions = self._plan([_resp("finish", {}, done=True)])
        self.assertEqual(actions[0].action_type, ActionType.FINISH)

    def test_click_alias_maps_to_tap(self) -> None:
        actions = self._plan([_resp("click", {"x": 1, "y": 2})])
        self.assertEqual(actions[0].action_type, ActionType.TAP)


class LLMPlannerFallbackTests(unittest.TestCase):
    def _candidate(self, script: list[str]):
        planner = LLMPlanner(FakeLLMClient(script=script))
        return planner.generate(_ctx("g"), _obs())

    def test_malformed_json_falls_back_to_wait(self) -> None:
        cands = self._candidate(["not json at all"])
        self.assertEqual(cands[0].actions[0].action_type, ActionType.WAIT)
        self.assertTrue(cands[0].metadata.get("fallback"))

    def test_unknown_action_falls_back_to_wait(self) -> None:
        cands = self._candidate([_resp("teleport", {})])
        self.assertEqual(cands[0].actions[0].action_type, ActionType.WAIT)
        self.assertTrue(cands[0].metadata.get("fallback"))

    def test_invalid_tap_args_falls_back(self) -> None:
        cands = self._candidate([_resp("tap", {"x": "oops", "y": 2})])
        self.assertEqual(cands[0].actions[0].action_type, ActionType.WAIT)
        self.assertTrue(cands[0].metadata.get("fallback"))

    def test_missing_args_falls_back(self) -> None:
        cands = self._candidate([_resp("open_url", {})])
        self.assertEqual(cands[0].actions[0].action_type, ActionType.WAIT)

    def test_llm_error_falls_back(self) -> None:
        # Script with zero entries: first call raises LLMError immediately.
        planner = LLMPlanner(FakeLLMClient(script=[]))
        cands = planner.generate(_ctx("g"), _obs())
        self.assertEqual(cands[0].actions[0].action_type, ActionType.WAIT)
        self.assertTrue(cands[0].metadata.get("fallback"))


class LLMPlannerResponseParsingTests(unittest.TestCase):
    def test_code_fence_json(self) -> None:
        fenced = '```json\n{"action":"back","done":false}\n```'
        obj = _extract_json(fenced)
        self.assertEqual(obj["action"], "back")

    def test_json_with_surrounding_prose(self) -> None:
        noisy = 'Sure! Here is my decision:\n{"action":"back","done":false}\nHope that helps.'
        obj = _extract_json(noisy)
        self.assertEqual(obj["action"], "back")

    def test_non_string_response_raises(self) -> None:
        with self.assertRaises(LLMError):
            _extract_json("no braces here")

    def test_confidence_clamped_high(self) -> None:
        planner = LLMPlanner(FakeLLMClient(script=[_resp("tap", {"x": 1, "y": 2}, confidence=5.0)]))
        cands = planner.generate(_ctx("g"), _obs())
        self.assertLessEqual(cands[0].confidence, 1.0)

    def test_confidence_clamped_low(self) -> None:
        planner = LLMPlanner(FakeLLMClient(script=[_resp("tap", {"x": 1, "y": 2}, confidence=-1.0)]))
        cands = planner.generate(_ctx("g"), _obs())
        self.assertGreaterEqual(cands[0].confidence, 0.0)


class LLMPlannerMessageBuildingTests(unittest.TestCase):
    def test_screenshot_included_when_available(self) -> None:
        fake = FakeLLMClient(script=[_resp("finish", {}, done=True)])
        planner = LLMPlanner(fake, include_screenshot=True)
        planner.generate(_ctx("g"), _obs(screenshot_base64="ZmFrZQ=="))
        messages = fake.calls[0]
        user_msg = messages[1]
        self.assertIsInstance(user_msg["content"], list)
        kinds = [part.get("type") for part in user_msg["content"]]
        self.assertIn("image_url", kinds)

    def test_screenshot_omitted_when_disabled(self) -> None:
        fake = FakeLLMClient(script=[_resp("finish", {}, done=True)])
        planner = LLMPlanner(fake, include_screenshot=False)
        planner.generate(_ctx("g"), _obs(screenshot_base64="ZmFrZQ=="))
        messages = fake.calls[0]
        self.assertIsInstance(messages[1]["content"], str)

    def test_goal_in_user_message(self) -> None:
        fake = FakeLLMClient(script=[_resp("finish", {}, done=True)])
        planner = LLMPlanner(fake)
        planner.generate(_ctx("open the calculator"), _obs())
        self.assertIn("open the calculator", fake.calls[0][1]["content"])


class UiTreeSummaryTests(unittest.TestCase):
    def test_empty_returns_placeholder(self) -> None:
        self.assertIn("no UI tree", _summarize_ui_tree(None))

    def test_renders_node_text(self) -> None:
        tree = {
            "class": "android.widget.TextView",
            "text": "Hello",
            "bounds": "[0,0][100,50]",
            "children": [
                {"class": "android.widget.Button", "text": "OK"},
            ],
        }
        summary = _summarize_ui_tree(tree)
        self.assertIn("TextView", summary)
        self.assertIn('"Hello"', summary)
        self.assertIn("Button", summary)
        self.assertIn('"OK"', summary)


if __name__ == "__main__":
    unittest.main()
