from __future__ import annotations

import unittest

from agentpro.agent_runner import FakeResponse, RecordingBridgeClient
from agentpro.screen import (
    ScreenReader,
    compute_fingerprint,
    extract_elements,
    summarize_screen,
)


def _tree(**overrides: object) -> dict:
    root = {
        "class": "android.widget.FrameLayout",
        "package": "com.example.app",
        "children": [
            {
                "class": "android.widget.TextView",
                "text": "Create new",
                "content_desc": "",
                "bounds": "[100,100][400,160]",
                "clickable": True,
                "checked": False,
                "editable": False,
            },
            {
                "class": "android.widget.EditText",
                "text": "",
                "content_desc": "Name field",
                "bounds": "[100,200][900,300]",
                "clickable": True,
                "editable": True,
                "focused": True,
            },
            {
                "class": "android.widget.TextView",
                "text": "Instructions",
                "content_desc": "",
                "bounds": "[100,320][900,520]",
                "clickable": False,
            },
            {
                "class": "android.widget.ListView",
                "text": "",
                "content_desc": "",
                "bounds": "[0,600][1080,2400]",
                "scrollable": True,
            },
            {
                "class": "android.widget.FrameLayout",
                "text": "",
                "content_desc": "",
                "bounds": "[0,0][1080,2400]",
                "clickable": False,
            },
        ],
    }
    root.update(overrides)
    return root


class ExtractElementsTests(unittest.TestCase):
    def test_filters_and_labels(self) -> None:
        elements = extract_elements(_tree())
        letters = [el.class_name for el in elements]
        self.assertIn("android.widget.EditText", letters)
        self.assertIn("android.widget.ListView", letters)
        self.assertEqual(len(elements), 4)

        create = [el for el in elements if el.text == "Create new"]
        self.assertEqual(len(create), 1)
        self.assertTrue(create[0].clickable)
        self.assertEqual(create[0].bounds, (100, 100, 400, 160))
        self.assertEqual(create[0].center(), (250, 130))

        edit = [el for el in elements if el.editable]
        self.assertEqual(len(edit), 1)
        self.assertEqual(edit[0].content_desc, "Name field")

    def test_empty_tree(self) -> None:
        self.assertEqual(extract_elements(None), [])
        self.assertEqual(extract_elements({}), [])

    def test_bad_bounds_skipped(self) -> None:
        root = {
            "class": "android.widget.TextView",
            "text": "x",
            "bounds": "garbage",
            "clickable": True,
        }
        self.assertEqual(extract_elements(root), [])

    def test_inverted_bounds_skipped(self) -> None:
        root = {
            "class": "android.widget.TextView",
            "text": "x",
            "bounds": "[100,100][50,50]",
            "clickable": True,
        }
        self.assertEqual(extract_elements(root), [])


class FingerprintTests(unittest.TestCase):
    def test_stable_across_runs(self) -> None:
        a = extract_elements(_tree())
        b = extract_elements(_tree())
        self.assertEqual(compute_fingerprint(a), compute_fingerprint(b))
        self.assertTrue(compute_fingerprint(a))

    def test_changes_when_text_changes(self) -> None:
        base = _tree()
        changed = _tree()
        changed["children"][0]["text"] = "Rename"
        f1 = compute_fingerprint(extract_elements(base))
        f2 = compute_fingerprint(extract_elements(changed))
        self.assertNotEqual(f1, f2)


class SummarizeScreenTests(unittest.TestCase):
    def test_editable_prioritized(self) -> None:
        snapshot = ScreenReader(RecordingBridgeClient(ui_root=_tree())).observe()
        text = summarize_screen(snapshot, max_chars=4000)
        lines = text.splitlines()
        editable_idx = next(
            i for i, line in enumerate(lines) if "|editable" in line
        )
        create_idx = next(
            i for i, line in enumerate(lines) if 'text="Create new"' in line
        )
        self.assertLess(editable_idx, create_idx)

    def test_truncation_respected(self) -> None:
        snapshot = ScreenReader(RecordingBridgeClient(ui_root=_tree())).observe()
        text = summarize_screen(snapshot, max_chars=300)
        self.assertLessEqual(len(text), 300)

    def test_unreadable_screen(self) -> None:
        class BrokenClient:
            def ui_dump(self):
                raise RuntimeError("boom")

        reader = ScreenReader(BrokenClient(), include_screenshot=False)
        snapshot = reader.observe()
        self.assertFalse(snapshot.success)
        text = summarize_screen(snapshot)
        self.assertIn("unreadable", text)


class WaitForTests(unittest.TestCase):
    def test_wait_for_text(self) -> None:
        client = RecordingBridgeClient(ui_root=_tree())
        found, _ = client and ScreenReader(
            client, include_screenshot=False
        ).wait_for_text(
            "Create new", timeout_s=1.0, interval_s=0.05
        )
        self.assertTrue(found)

    def test_wait_for_text_timeout(self) -> None:
        client = RecordingBridgeClient(ui_root=_tree())
        found, _ = ScreenReader(
            client, include_screenshot=False
        ).wait_for_text(
            "does not exist", timeout_s=0.2, interval_s=0.05
        )
        self.assertFalse(found)

    def test_wait_for_stable(self) -> None:
        client = RecordingBridgeClient(ui_root=_tree())
        stable, _ = ScreenReader(
            client, include_screenshot=False
        ).wait_for_stable(timeout_s=1.0, stable_count=2, interval_s=0.05)
        self.assertTrue(stable)


class WindowInfoTests(unittest.TestCase):
    def test_window_info_via_get_window(self) -> None:
        client = RecordingBridgeClient(ui_root=_tree())
        reader = ScreenReader(client, include_screenshot=False)
        pkg, activity, title = reader.get_window_info()
        self.assertEqual(pkg, "com.android.launcher3")
        self.assertEqual(activity, "launcher")
        self.assertEqual(title, "Launcher")


class KotlinBridgeContractTests(unittest.TestCase):
    """Regression tests for the Kotlin->Python observation contract."""

    _KOTLIN_TREE = {
        "class_name": "android.widget.FrameLayout",
        "package_name": "com.android.settings",
        "view_id_resource_name": "com.android.settings:id/content",
        "text": "",
        "content_description": "Settings home",
        "clickable": False,
        "enabled": True,
        "focused": False,
        "scrollable": False,
        "selected": False,
        "checked": False,
        "editable": False,
        "visible_to_user": True,
        "bounds": {"left": 0, "top": 0, "right": 1080, "bottom": 2400},
        "children": [
            {
                "class_name": "android.widget.EditText",
                "package_name": "com.android.settings",
                "view_id_resource_name": "com.android.settings:id/search",
                "text": "Search here",
                "content_description": "",
                "clickable": True,
                "enabled": True,
                "editable": True,
                "focused": True,
                "visible_to_user": True,
                "bounds": {"left": 20, "top": 100, "right": 900, "bottom": 200},
                "children": [],
            },
            {
                "class_name": "android.widget.TextView",
                "package_name": "com.android.settings",
                "view_id_resource_name": None,
                "text": "hidden",
                "content_description": "",
                "clickable": False,
                "enabled": True,
                "visible_to_user": False,
                "bounds": {"left": 0, "top": 500, "right": 100, "bottom": 560},
                "children": [],
            },
            {
                "class_name": "android.widget.ImageButton",
                "package_name": "com.android.settings",
                "view_id_resource_name": "com.android.settings:id/button",
                "text": None,
                "content_description": "Search settings",
                "clickable": True,
                "enabled": False,
                "focused": False,
                "scrollable": False,
                "visible_to_user": True,
                "bounds": {"left": 10, "top": 50, "right": 90, "bottom": 120},
                "children": [],
            },
        ],
    }

    def test_kotlin_field_names_parsed(self) -> None:
        elements = extract_elements(self._KOTLIN_TREE)

        edit = next(el for el in elements if el.editable)
        self.assertEqual(
            edit.class_name, "android.widget.EditText"
        )
        self.assertEqual(
            edit.resource_id, "com.android.settings:id/search"
        )
        self.assertTrue(edit.enabled)

        button = next(el for el in elements if el.clickable and not el.editable)
        self.assertEqual(
            button.class_name, "android.widget.ImageButton"
        )
        self.assertEqual(
            button.resource_id, "com.android.settings:id/button"
        )
        self.assertEqual(button.content_desc, "Search settings")
        self.assertFalse(button.enabled)

    def test_invisible_nodes_filtered(self) -> None:
        elements = extract_elements(self._KOTLIN_TREE)
        labels = [el.text for el in elements]
        self.assertIn("Search here", labels)
        self.assertNotIn("hidden", labels)

    def test_disabled_flag_in_summary(self) -> None:
        from agentpro.screen import ScreenReader, summarize_screen

        snapshot = ScreenReader(
            RecordingBridgeClient(ui_root=self._KOTLIN_TREE),
            include_screenshot=False,
        ).observe()
        text = summarize_screen(snapshot)
        self.assertIn("disabled", text)

    def test_backward_compat_string_bounds(self) -> None:
        root = {
            "class": "android.widget.FrameLayout",
            "package": "com.example",
            "children": [
                {
                    "class": "android.widget.TextView",
                    "text": "old",
                    "bounds": "[10,10][110,60]",
                    "clickable": True,
                }
            ],
        }
        elements = extract_elements(root)
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0].text, "old")

    def test_truncated_flag_observed(self) -> None:
        class _Client(RecordingBridgeClient):
            def ui_dump(self) -> FakeResponse:
                self.calls.append(("ui_dump", {}))
                return FakeResponse(
                    data={"root": self._ui_root, "truncated": True}
                )

        client = _Client(ui_root=self._KOTLIN_TREE)
        snapshot = ScreenReader(client, include_screenshot=False).observe()
        self.assertTrue(snapshot.tree_truncated)
        text = summarize_screen(snapshot)
        self.assertIn("tree truncated", text)


if __name__ == "__main__":
    unittest.main()