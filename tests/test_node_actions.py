from __future__ import annotations

import json
import unittest

from python_core.bridge_client import (
    BridgeClient,
    BridgeConfigurationError,
)


class FakeBridgeClient(BridgeClient):
    """BridgeClient with _post overridden to capture outgoing requests."""

    def __init__(self, data: dict | None = None) -> None:
        super().__init__(token="test-token")
        self.requests: list[dict] = []
        self._data = data or {}

    def _post(self, body: bytes) -> bytes:
        payload = json.loads(body.decode("utf-8"))
        self.requests.append(payload)
        rid = payload["request_id"]
        response = {
            "protocol": "ultimate",
            "version": "1.0",
            "ok": True,
            "request_id": rid,
            "data": self._data,
        }
        return json.dumps(response).encode("utf-8")


class ListNodeActionTests(unittest.TestCase):
    def test_list_windows_sends_command(self) -> None:
        client = FakeBridgeClient(data={"windows": []})
        client.list_windows()
        self.assertEqual(client.requests[-1]["command"], "list_windows")

    def test_window_packages_reads_and_dedupes(self) -> None:
        client = FakeBridgeClient(
            data={
                "windows": [
                    {"package_name": "com.instagram.android"},
                    {"package_name": "com.instagram.android"},
                    {"package_name": "dev.blazelight.p4oc"},
                    {"package_name": None},
                ]
            }
        )
        packages = client.window_packages()
        self.assertEqual(
            packages,
            ["com.instagram.android", "dev.blazelight.p4oc"],
        )

    def test_ui_dump_for_package_sends_package_arg(self) -> None:
        client = FakeBridgeClient(data={"root": {}})
        client.ui_dump_for_package("com.instagram.android")
        request = client.requests[-1]
        self.assertEqual(request["command"], "ui_dump")
        self.assertEqual(request["args"]["package"], "com.instagram.android")

    def test_ui_dump_for_package_rejects_empty(self) -> None:
        client = FakeBridgeClient()
        with self.assertRaises(BridgeConfigurationError):
            client.ui_dump_for_package("  ")

    def test_node_action_sends_selector_and_action(self) -> None:
        client = FakeBridgeClient()
        client.node_action(
            "click",
            package="com.instagram.android",
            text="Like",
            match_index=2,
        )
        request = client.requests[-1]
        self.assertEqual(request["command"], "node_action")
        self.assertEqual(request["args"]["action"], "click")
        self.assertEqual(request["args"]["package"], "com.instagram.android")
        self.assertEqual(request["args"]["text"], "Like")
        self.assertEqual(request["args"]["match_index"], 2)
        self.assertNotIn("node_path", request["args"])

    def test_node_action_accepts_path(self) -> None:
        client = FakeBridgeClient()
        client.node_action("scroll_forward", package="p", node_path=[0, 1, 2])
        request = client.requests[-1]
        self.assertEqual(request["args"]["node_path"], [0, 1, 2])

    def test_node_action_requires_path_or_selector(self) -> None:
        client = FakeBridgeClient()
        with self.assertRaises(BridgeConfigurationError):
            client.node_action("click", package="p")

    def test_node_action_rejects_empty_action(self) -> None:
        client = FakeBridgeClient()
        with self.assertRaises(BridgeConfigurationError):
            client.node_action("   ", package="p", text="x")

    def test_node_click_builds_click_action(self) -> None:
        client = FakeBridgeClient()
        client.node_click("p", text="Save")
        self.assertEqual(client.requests[-1]["args"]["action"], "click")

    def test_node_scroll_validates_direction(self) -> None:
        client = FakeBridgeClient()
        with self.assertRaises(BridgeConfigurationError):
            client.node_scroll("p", "sideways", text="x")

    def test_node_scroll_forward_builds_action(self) -> None:
        client = FakeBridgeClient()
        client.node_scroll("p", "forward", text="x")
        self.assertEqual(
            client.requests[-1]["args"]["action"],
            "scroll_forward",
        )


class FeedPathTests(unittest.TestCase):
    """scrollable_node_paths / window_feed_paths walk a dumped tree."""

    def _client_with_tree(self, tree: dict) -> FakeBridgeClient:
        return FakeBridgeClient(data={"root": tree})

    def test_scrollable_node_paths_collects_paths(self) -> None:
        tree = {
            "class_name": "root",
            "scrollable": False,
            "children": [
                {
                    "class_name": "androidx.recyclerview.widget.RecyclerView",
                    "scrollable": True,
                    "children": [
                        {"class_name": "android.view.View", "scrollable": False},
                    ],
                },
                {"class_name": "android.widget.ScrollView", "scrollable": True},
            ],
        }
        client = self._client_with_tree(tree)
        paths = client.scrollable_node_paths("p")
        self.assertEqual(paths, [[0], [1]])

    def test_window_feed_paths_filters_to_recyclerview(self) -> None:
        tree = {
            "class_name": "root",
            "scrollable": False,
            "children": [
                {
                    "class_name": "androidx.recyclerview.widget.RecyclerView",
                    "scrollable": True,
                    "children": [],
                },
                {"class_name": "android.widget.ScrollView", "scrollable": True},
            ],
        }
        client = self._client_with_tree(tree)
        paths = client.window_feed_paths("p")
        self.assertEqual(paths, [[0]])

    def test_window_feed_paths_empty_when_no_root(self) -> None:
        client = FakeBridgeClient(data={})
        self.assertEqual(client.window_feed_paths("p"), [])


if __name__ == "__main__":
    unittest.main()
