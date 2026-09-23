from __future__ import annotations

import json
import unittest

from python_core.bridge_client import (
    BridgeClient,
    BridgeConfigurationError,
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


if __name__ == "__main__":
    unittest.main()
