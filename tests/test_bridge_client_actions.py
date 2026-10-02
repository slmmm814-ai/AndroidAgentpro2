from __future__ import annotations

import json
import unittest

from python_core.bridge_client import (
    BridgeClient,
    BridgeConfigurationError,
    BridgeRemoteError,
    BridgeResponse,
)


class FakeBridgeClient(BridgeClient):
    """BridgeClient with _post overridden to avoid sockets and capture requests."""

    def __init__(self) -> None:
        super().__init__(token="test-token")
        self.last_request: dict | None = None

    def _post(self, body: bytes) -> bytes:
        payload = json.loads(body.decode("utf-8"))
        self.last_request = payload
        rid = payload["request_id"]
        response = {
            "protocol": "ultimate",
            "version": "1.0",
            "ok": True,
            "request_id": rid,
            "data": {"operation_id": 42},
        }
        return json.dumps(response).encode("utf-8")


class ScreenshotRateLimitedClient(BridgeClient):
    """Device that refuses captures closer together than the 250 ms limit."""

    def __init__(self, refusals: int = 1) -> None:
        super().__init__(token="test-token")
        self.refusals = refusals
        self.attempts = 0
        self.slept: list[float] = []

    def _post(self, body: bytes) -> bytes:
        payload = json.loads(body.decode("utf-8"))
        rid = payload["request_id"]
        self.attempts += 1
        if self.attempts <= self.refusals:
            return json.dumps({
                "protocol": "ultimate",
                "version": "1.0",
                "ok": False,
                "request_id": rid,
                "error": {
                    "code": "SCREENSHOT_CAPTURE_FAILED",
                    "message": "Screenshot requested too frequently",
                },
            }).encode("utf-8")
        return json.dumps({
            "protocol": "ultimate",
            "version": "1.0",
            "ok": True,
            "request_id": rid,
            "data": {"base64": "QUJD"},
        }).encode("utf-8")


class ScreenshotRateLimitTests(unittest.TestCase):
    def test_screenshot_sends_the_right_command(self) -> None:
        client = FakeBridgeClient()
        response = client.screenshot()
        self.assertTrue(response.ok)
        self.assertEqual(client.last_request["command"], "screenshot")

    def test_screenshot_retries_after_a_rate_limit_refusal(self) -> None:
        client = ScreenshotRateLimitedClient(refusals=1)
        response = client.screenshot()
        self.assertTrue(response.ok, "a rate-limited capture must be retried, not lost")
        self.assertEqual(response.data["base64"], "QUJD")
        self.assertEqual(client.attempts, 2)

    def test_screenshot_gives_up_after_the_retry_budget(self) -> None:
        client = ScreenshotRateLimitedClient(refusals=99)
        # exhausting the budget surfaces the device's own reason rather than
        # an endless retry loop
        with self.assertRaises(BridgeRemoteError) as ctx:
            client.screenshot(retries=1)
        self.assertIn("too frequently", str(ctx.exception))
        self.assertEqual(client.attempts, 2)

    def test_backoff_wait_matches_the_device_interval(self) -> None:
        # ScreenshotEngine.MIN_CAPTURE_INTERVAL_MS is 250 ms; waiting less
        # would just be refused again.
        self.assertGreaterEqual(BridgeClient.SCREENSHOT_MIN_INTERVAL_S, 0.25)


class BridgeClientNewActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeBridgeClient()

    def test_input_text_sends_correct_command(self) -> None:
        resp = self.client.input_text("hello world")
        self.assertIsInstance(resp, BridgeResponse)
        self.assertTrue(resp.ok)
        self.assertEqual(self.client.last_request["command"], "input_text")
        self.assertEqual(self.client.last_request["args"]["text"], "hello world")

    def test_swipe_sends_correct_args(self) -> None:
        self.client.swipe(1, 2, 3, 4, duration_ms=500)
        args = self.client.last_request["args"]
        self.assertEqual(self.client.last_request["command"], "swipe")
        self.assertEqual(args["x1"], 1)
        self.assertEqual(args["y1"], 2)
        self.assertEqual(args["x2"], 3)
        self.assertEqual(args["y2"], 4)
        self.assertEqual(args["duration_ms"], 500)

    def test_open_url_prepends_https_for_bare_domain(self) -> None:
        self.client.open_url("example.com")
        self.assertEqual(self.client.last_request["args"]["url"], "https://example.com")

    def test_open_url_keeps_https(self) -> None:
        self.client.open_url("https://x.com")
        self.assertEqual(self.client.last_request["args"]["url"], "https://x.com")

    def test_open_url_keeps_data_url(self) -> None:
        url = "data:text/html;charset=utf-8,%3Chtml%3Ehi%3C/html%3E"
        self.client.open_url(url)
        self.assertEqual(self.client.last_request["args"]["url"], url)

    def test_key_event_sends_keycode(self) -> None:
        self.client.key_event("home")
        self.assertEqual(self.client.last_request["command"], "key_event")
        self.assertEqual(self.client.last_request["args"]["keycode"], "home")

    def test_launch_app_sends_package(self) -> None:
        self.client.launch_app("com.android.chrome")
        self.assertEqual(self.client.last_request["command"], "launch_app")
        self.assertEqual(
            self.client.last_request["args"]["package"], "com.android.chrome"
        )

    def test_clear_text_sends_command(self) -> None:
        self.client.clear_text()
        self.assertEqual(self.client.last_request["command"], "clear_text")

    def test_erase_text_sends_command(self) -> None:
        self.client.erase_text()
        self.assertEqual(self.client.last_request["command"], "erase_text")

    def test_take_photo_sends_correct_command(self) -> None:
        resp = self.client.take_photo("front")
        self.assertIsInstance(resp, BridgeResponse)
        self.assertTrue(resp.ok)
        self.assertEqual(self.client.last_request["command"], "take_photo")
        self.assertEqual(self.client.last_request["args"]["facing"], "front")


class BridgeClientValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeBridgeClient()

    def test_input_text_empty_rejected(self) -> None:
        with self.assertRaises(BridgeConfigurationError):
            self.client.input_text("")

    def test_swipe_non_numeric_rejected(self) -> None:
        with self.assertRaises(BridgeConfigurationError):
            self.client.swipe("a", 2, 3, 4)

    def test_swipe_bad_duration_rejected(self) -> None:
        with self.assertRaises(BridgeConfigurationError):
            self.client.swipe(1, 2, 3, 4, duration_ms=0)

    def test_open_url_empty_rejected(self) -> None:
        with self.assertRaises(BridgeConfigurationError):
            self.client.open_url("")

    def test_key_event_empty_rejected(self) -> None:
        with self.assertRaises(BridgeConfigurationError):
            self.client.key_event("")

    def test_launch_app_empty_rejected(self) -> None:
        with self.assertRaises(BridgeConfigurationError):
            self.client.launch_app("")

    def test_take_photo_bad_facing_rejected(self) -> None:
        with self.assertRaises(BridgeConfigurationError):
            self.client.take_photo("invalid_facing")

    def test_shizuku_shell_empty_rejected(self) -> None:
        with self.assertRaises(BridgeConfigurationError):
            self.client.shizuku_shell("")

    def test_shizuku_shell_sends_command(self) -> None:
        self.client.shizuku_shell("ls")
        self.assertEqual(self.client.last_request["command"], "shizuku_shell")
        self.assertEqual(self.client.last_request["args"]["command"], "ls")


if __name__ == "__main__":
    unittest.main()
