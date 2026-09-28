from __future__ import annotations

import pathlib
import unittest

from agentpro.agent_runner import FakeResponse, RecordingBridgeClient
from agentpro.screen import (
    InteractiveElement,
    ScreenReader,
    compute_fingerprint,
    difference_hash,
    extract_elements,
    hamming_distance,
    new_content_ratio,
    scroll_container_bounds,
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


class ChangeChannelTests(unittest.TestCase):
    """The two channels answer different questions and must stay separate."""

    def _screen(self, texts: list[str], scrollable: bool = False) -> list:
        root = {
            "class": "android.widget.FrameLayout",
            "package": "com.example.app",
            "children": [
                {
                    "class": "android.widget.TextView",
                    "text": text,
                    "bounds": f"[0,{200 + i * 150}][1080,{300 + i * 150}]",
                }
                for i, text in enumerate(texts)
            ],
        }
        if scrollable:
            root["children"].insert(
                0,
                {
                    "class": "android.view.View",
                    "bounds": "[0,0][1080,1832]",
                    "scrollable": True,
                },
            )
        return extract_elements(root)

    def test_scroll_repositions_without_new_content(self) -> None:
        before = self._screen(["a", "b", "c"], scrollable=True)
        after = [
            InteractiveElement(
                node_index=el.node_index,
                class_name=el.class_name,
                package=el.package,
                resource_id=el.resource_id,
                text=el.text,
                content_desc=el.content_desc,
                bounds=(el.left, el.top - 600, el.right, el.bottom - 600),
                clickable=el.clickable,
                scrollable=el.scrollable,
            )
            for el in before
        ]
        self.assertEqual(new_content_ratio(before, after), 0.0)
        self.assertNotEqual(
            compute_fingerprint(before), compute_fingerprint(after)
        )

    def test_new_page_reports_new_content(self) -> None:
        before = self._screen(["a", "b"], scrollable=True)
        after = self._screen(["c", "d"], scrollable=True)
        self.assertEqual(new_content_ratio(before, after), 1.0)

    def test_partially_new_content(self) -> None:
        before = self._screen(["a", "b"], scrollable=True)
        after = self._screen(["a", "b", "c", "d"], scrollable=True)
        self.assertAlmostEqual(new_content_ratio(before, after), 0.5)

    def test_container_bounds_detects_keyboard(self) -> None:
        without_keyboard = self._screen(["a"], scrollable=True)
        with_keyboard = [
            InteractiveElement(
                node_index=el.node_index,
                class_name=el.class_name,
                package=el.package,
                resource_id=el.resource_id,
                text=el.text,
                content_desc=el.content_desc,
                bounds=el.bounds if not el.scrollable else (0, 0, 1080, 959),
                scrollable=el.scrollable,
            )
            for el in without_keyboard
        ]
        without_keyboard_bounds = scroll_container_bounds(without_keyboard)
        with_keyboard_bounds = scroll_container_bounds(with_keyboard)
        self.assertIsNotNone(without_keyboard_bounds)
        self.assertIsNotNone(with_keyboard_bounds)
        assert without_keyboard_bounds is not None
        assert with_keyboard_bounds is not None
        self.assertEqual(without_keyboard_bounds[3], 1832)
        self.assertEqual(with_keyboard_bounds[3], 959)

    def test_container_bounds_absent_without_scrollable(self) -> None:
        self.assertIsNone(scroll_container_bounds(self._screen(["a"])))

    def test_hamming_distance_basics(self) -> None:
        self.assertEqual(hamming_distance(0b1010, 0b1010), 0)
        self.assertEqual(hamming_distance(0b1010, 0b0101), 4)

    def test_difference_hash_survives_jpeg_recompression(self) -> None:
        """The real noise source: JPEG re-encoding of a static dark screen.

        Measured on device: two idle screenshots of the same screen had
        different JPEG md5 and a naive per-pixel diff of 0.65, while dHash
        reported 0. Re-encoding the same frame at low quality reproduces that
        condition without needing a device.
        """
        import io

        from PIL import Image, ImageDraw

        def dark_frame() -> Image.Image:
            image = Image.new("L", (240, 480), color=24)
            draw = ImageDraw.Draw(image)
            draw.rectangle([20, 40, 220, 90], fill=180)
            draw.rectangle([20, 120, 150, 150], fill=90)
            return image

        def as_jpeg(image: Image.Image, quality: int) -> Image.Image:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            return Image.open(io.BytesIO(buffer.getvalue()))

        original = dark_frame()
        baseline = difference_hash(as_jpeg(original, 80))
        recompressed = as_jpeg(original, 45)

        self.assertNotEqual(
            original.tobytes(), recompressed.tobytes()
        )
        self.assertEqual(difference_hash(recompressed), baseline)

    def test_difference_hash_detects_real_change(self) -> None:
        from PIL import Image, ImageDraw

        plain = Image.new("L", (240, 480), color=24)
        marked = Image.new("L", (240, 480), color=24)
        ImageDraw.Draw(marked).rectangle([10, 10, 230, 470], fill=220)
        self.assertGreater(
            hamming_distance(difference_hash(plain), difference_hash(marked)), 6
        )


class VisualHashChannelTests(unittest.TestCase):
    """The bridge hashes on device; a failure must never read as 'no change'."""

    class _HashClient:
        def __init__(self, hashes: list[object]) -> None:
            self._hashes = list(hashes)
            self.calls = 0

        def command(self, name: str, args: dict) -> object:
            self.calls += 1
            value = self._hashes.pop(0) if self._hashes else None

            class Response:
                def __init__(self, data: object) -> None:
                    self.data = data

            if isinstance(value, Exception):
                raise value
            if value is None:
                return Response({"hash": None})
            return Response({"hash": value})

    def test_reads_bridge_hash(self) -> None:
        client = self._HashClient(["ff00ff00"])
        reader = ScreenReader(client)
        self.assertEqual(reader.capture_visual(), 0xFF00FF00)

    def test_failed_capture_returns_none(self) -> None:
        client = self._HashClient([RuntimeError("bridge down")])
        self.assertIsNone(ScreenReader(client).capture_visual())

    def test_malformed_hash_returns_none(self) -> None:
        client = self._HashClient(["not-hex"])
        self.assertIsNone(ScreenReader(client).capture_visual())

    def test_stale_hash_is_not_replayed_after_failure(self) -> None:
        """The core anti-false-negative rule, at the channel level."""
        client = self._HashClient(["abcdef01", RuntimeError("timeout")])
        reader = ScreenReader(client, visual_interval_s=0.0)
        self.assertEqual(reader.capture_visual(), 0xABCDEF01)
        self.assertIsNone(reader.capture_visual(force=True))

    def test_change_unknown_without_previous_visual(self) -> None:
        client = self._HashClient(["00000000"])
        reader = ScreenReader(client, visual_interval_s=0.0)
        change = reader.measure_change(None, None)
        self.assertIsNone(change.moved)
        self.assertIsNone(change.distance)
        self.assertFalse(change.settled)

    def test_change_measured_against_previous_visual(self) -> None:
        client = self._HashClient(["0000ffff"])
        reader = ScreenReader(client, visual_interval_s=0.0, visual_threshold=6)
        change = reader.measure_change(
            None, None, previous_visual=0x0000FFFF ^ 0x00FF00FF
        )
        self.assertEqual(change.distance, 16)
        self.assertTrue(change.moved)
        self.assertTrue(change.moved_without_new_content)
        self.assertFalse(change.settled)

    def test_unchanged_screen_below_threshold(self) -> None:
        client = self._HashClient(["0000ffff"])
        reader = ScreenReader(client, visual_interval_s=0.0, visual_threshold=6)
        change = reader.measure_change(
            None, None, previous_visual=0x0000FFFF ^ 0x00000003
        )
        self.assertEqual(change.distance, 2)
        self.assertFalse(change.moved)
        self.assertTrue(change.settled)

    def test_interval_serves_cached_hash(self) -> None:
        client = self._HashClient(["0000ffff"])
        reader = ScreenReader(client, visual_interval_s=60.0)
        self.assertEqual(reader.capture_visual(), 0x0000FFFF)
        self.assertEqual(reader.capture_visual(), 0x0000FFFF)
        self.assertEqual(client.calls, 1)

    def test_rate_limited_bridge_reads_as_unknown_not_still(self) -> None:
        client = self._HashClient(
            ["0000ffff", RuntimeError("Screenshot requested too frequently")]
        )
        reader = ScreenReader(client, visual_interval_s=0.0)
        first = reader.capture_visual(force=True)
        self.assertEqual(first, 0x0000FFFF)
        change = reader.measure_change(
            None, None, previous_visual=first
        )
        self.assertIsNone(change.moved)
        self.assertFalse(change.settled)


class VisualHashBridgeContractTests(unittest.TestCase):
    """Python depends on a Kotlin-only command, so its presence is asserted.

    Nothing at runtime checks that ``visual_hash`` still exists on the Android
    side: if the Kotlin command were dropped, the pixel channel would silently
    report "unknown" forever instead of failing loudly.
    """

    _KOTLIN_DIR = (
        pathlib.Path(__file__).resolve().parent.parent
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "ai"
        / "agentpro"
    )

    def test_bridge_dispatches_visual_hash(self) -> None:
        source = (self._KOTLIN_DIR / "BridgeServer.kt").read_text()
        self.assertIn('"visual_hash" -> executeVisualHash(request)', source)

    def test_visual_hash_implementation_present(self) -> None:
        self.assertTrue((self._KOTLIN_DIR / "VisualHash.kt").exists())

    def test_visual_hash_shape_matches_python_hash_size(self) -> None:
        source = (self._KOTLIN_DIR / "VisualHash.kt").read_text()
        self.assertIn("fun compute(bitmap: Bitmap, size: Int = 16)", source)

    def test_python_sends_the_command(self) -> None:
        source = (
            pathlib.Path(__file__).resolve().parent.parent
            / "agentpro"
            / "screen.py"
        ).read_text()
        self.assertIn('self._client.command("visual_hash", {})', source)


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