from __future__ import annotations

import json
import unittest

from agentpro.agent_runner import (
    AgentRunner,
    BridgeActionExecutor,
    BridgeObserver,
    LastActionGoalVerifier,
    RecordingBridgeClient,
)
from agentpro.llm_planner import FakeLLMClient
from agentpro.models import (
    ActionResult,
    AgentAction,
    AgentContext,
    ActionType,
    GoalResult,
    Observation,
)


def _resp(action: str, args: dict | None = None, *, done=False, confidence=0.8) -> str:
    return json.dumps(
        {"thought": "t", "action": action, "args": args or {}, "done": done, "confidence": confidence}
    )


class BridgeObserverTests(unittest.TestCase):
    def test_observe_returns_ui_root_and_screenshot(self) -> None:
        client = RecordingBridgeClient(screenshot_base64="YWJj")
        obs = BridgeObserver(client).observe()
        self.assertTrue(obs.success)
        self.assertEqual(obs.package_name, "com.android.launcher3")
        self.assertEqual(obs.screenshot_base64, "YWJj")
        self.assertIsNotNone(obs.ui_root)

    def test_observe_without_screenshot(self) -> None:
        client = RecordingBridgeClient()
        obs = BridgeObserver(client, include_screenshot=False).observe()
        self.assertTrue(obs.success)
        self.assertIsNone(obs.screenshot_base64)


class BridgeActionExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = RecordingBridgeClient()
        self.executor = BridgeActionExecutor(self.client)

    def test_tap_dispatches_tap(self) -> None:
        r = self.executor.execute(AgentAction(ActionType.TAP, {"x": 1, "y": 2}))
        self.assertTrue(r.success)
        self.assertEqual(self.client.calls[-1][0], "tap")

    def test_input_text_dispatches(self) -> None:
        r = self.executor.execute(AgentAction(ActionType.INPUT_TEXT, {"text": "hi"}))
        self.assertTrue(r.success)
        self.assertEqual(self.client.calls[-1][0], "input_text")

    def test_open_url_dispatches(self) -> None:
        r = self.executor.execute(AgentAction(ActionType.OPEN_URL, {"url": "https://x.com"}))
        self.assertTrue(r.success)
        self.assertEqual(self.client.calls[-1][0], "open_url")

    def test_finish_is_noop_success(self) -> None:
        r = self.executor.execute(AgentAction(ActionType.FINISH))
        self.assertTrue(r.success)
        # FINISH must not be dispatched to the bridge
        self.assertNotIn("finish", self.client.commands)

    def test_wait_sleeps_and_succeeds(self) -> None:
        r = self.executor.execute(AgentAction(ActionType.WAIT, {"seconds": 0.01}))
        self.assertTrue(r.success)

    def test_install_apk_gated(self) -> None:
        r = self.executor.execute(AgentAction(ActionType.INSTALL_APK))
        self.assertFalse(r.success)
        self.assertEqual(r.error_code, "DANGEROUS_ACTION_GATED")


class LastActionGoalVerifierTests(unittest.TestCase):
    def test_finish_signals_success(self) -> None:
        ctx = AgentContext(goal="g")
        ctx.last_action = AgentAction(ActionType.FINISH)
        result = LastActionGoalVerifier().verify(ctx, None)
        self.assertTrue(result.success)

    def test_non_finish_not_success(self) -> None:
        ctx = AgentContext(goal="g")
        ctx.last_action = AgentAction(ActionType.TAP, {"x": 1, "y": 2})
        result = LastActionGoalVerifier().verify(ctx, None)
        self.assertFalse(result.success)


class AgentRunnerLoopTests(unittest.TestCase):
    def test_calculator_tour_reaches_success(self) -> None:
        from agentpro.demos.calculator_tour import build_calculator_demo

        runner, fake_llm, client = build_calculator_demo(max_actions=10)
        result = runner.run("build me a calculator app")
        self.assertTrue(result.success, msg=f"expected success, got: {result.reason}")
        # The agent must have opened exactly one data: URL.
        self.assertEqual(len(client.open_urls), 1)
        self.assertTrue(client.open_urls[0].startswith("data:text/html"))

    def test_tap_then_finish_loop(self) -> None:
        client = RecordingBridgeClient()
        runner = AgentRunner(
            client,
            FakeLLMClient(script=[_resp("tap", {"x": 5, "y": 5}), _resp("finish", {}, done=True)]),
            max_actions=10,
            trace_path="/tmp/agentpro_test_trace.jsonl",
        )
        result = runner.run("tap the button then finish")
        self.assertTrue(result.success)
        self.assertIn("tap", client.commands)

    def test_malformed_loop_terminates(self) -> None:
        # LLM always returns garbage; the loop must terminate (not hang).
        client = RecordingBridgeClient()
        runner = AgentRunner(
            client,
            FakeLLMClient(responder=lambda _msgs: "totally not json"),
            max_actions=4,
            trace_path="/tmp/agentpro_test_trace2.jsonl",
        )
        result = runner.run("impossible goal")
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
