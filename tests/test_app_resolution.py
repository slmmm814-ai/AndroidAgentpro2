from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from agentpro.app_resolver import (
    AppResolutionError,
    LauncherAppResolver,
    collect_launcher_entries,
    normalize_label,
    pick_entry,
)
from agentpro.bridge_tools import ToolContext, build_default_registry
from python_core.bridge_client import BridgeRemoteError


def _icon(
    label: str,
    *,
    short_id: str = "icon_1",
    left: int = 100,
    top: int = 200,
    right: int = 220,
    bottom: int = 320,
    clickable: bool = True,
    package: str = "com.sec.android.app.launcher",
) -> dict[str, Any]:
    return {
        "class_name": "android.widget.ImageView",
        "package_name": package,
        "view_id_resource_name": f"com.sec.android.app.launcher:id/{short_id}",
        "text": label,
        "content_description": None,
        "clickable": clickable,
        "bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
        "children": [],
    }


def _page(*labels: str, folders: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Lay icons out in a 4-column grid so tap hit-testing is unambiguous."""
    icons: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        col, row = index % 4, index // 4
        icons.append(
            _icon(label, left=col * 250, top=row * 300, right=col * 250 + 220, bottom=row * 300 + 280)
        )
    for offset, folder in enumerate(folders):
        index = len(labels) + offset
        col, row = index % 4, index // 4
        icons.append(
            _icon(
                folder,
                short_id="folder_icon_container",
                left=col * 250,
                top=row * 300,
                right=col * 250 + 220,
                bottom=row * 300 + 280,
            )
        )
    return icons


def _tree(icons: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "root": {
            "class_name": "android.widget.FrameLayout",
            "package_name": "com.sec.android.app.launcher",
            "bounds": {"left": 0, "top": 0, "right": 1080, "bottom": 2340},
            "children": icons,
        },
        "truncated": False,
    }


def _response(data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(ok=True, data=data, error_code=None, error_message=None)


def _no_sleep(_seconds: float) -> None:
    return None


class FakeLauncherClient:
    """Stateful launcher double: pages, folders, gestures, and call records."""

    def __init__(
        self,
        pages: list[list[dict[str, Any]]] | None = None,
        *,
        folders: dict[str, list[dict[str, Any]]] | None = None,
        fail_packages: set[str] | None = None,
    ) -> None:
        self.pages = pages or [[]]
        self.folders = folders or {}
        self.fail_packages = fail_packages or set()
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int]] = []
        self.keys: list[str] = []
        self.launched: list[str] = []
        self.backs = 0
        self.opened: str | None = None
        self._page = 0
        self._folder: str | None = None

    def last_screenshot_dims(self) -> tuple[int, int]:
        return (1080, 2340)

    def key_event(self, keycode: str) -> SimpleNamespace:
        self.keys.append(keycode)
        if keycode == "home":
            self._page = 0
            self._folder = None
        return _response({"keycode": keycode})

    def swipe(self, x1, y1, x2, y2, duration_ms=300) -> SimpleNamespace:
        self.swipes.append((x1, y1, x2, y2))
        if y2 < y1:  # content moves up -> later drawer page
            self._page = min(self._page + 1, len(self.pages) - 1)
        return _response({"dispatched": True})

    def tap(self, x, y) -> SimpleNamespace:
        self.taps.append((int(x), int(y)))
        for icon in self._current_icons():
            bounds = icon["bounds"]
            if not (
                bounds["left"] <= x <= bounds["right"]
                and bounds["top"] <= y <= bounds["bottom"]
            ):
                continue
            short_id = str(icon["view_id_resource_name"]).split("/")[-1]
            label = icon.get("text") or icon.get("content_description") or ""
            if "folder" in short_id and label in self.folders:
                self._folder = label
            else:
                self.opened = label
            break
        return _response({"dispatched": True})

    def back(self) -> SimpleNamespace:
        self.backs += 1
        self._folder = None
        return _response({"dispatched": True})

    def _current_icons(self) -> list[dict[str, Any]]:
        if self._folder is not None:
            return self.folders.get(self._folder, [])
        return self.pages[self._page]

    def ui_dump(self) -> SimpleNamespace:
        return _response(_tree(self._current_icons()))

    def launch_app(self, package: str) -> SimpleNamespace:
        if package in self.fail_packages:
            raise BridgeRemoteError(
                f"PACKAGE_NOT_FOUND: No launchable activity for package {package}",
                error_code="PACKAGE_NOT_FOUND",
                error_message=f"No launchable activity for package {package}",
            )
        self.launched.append(package)
        return _response({"dispatched": True, "package": package})


class NormalizeLabelTests(unittest.TestCase):
    def test_case_and_whitespace_folded(self) -> None:
        self.assertEqual(normalize_label("Calculator"), "calculator")
        self.assertEqual(normalize_label("  Al  Hudur "), "al hudur")

    def test_arabic_letter_shapes_and_diacritics_folded(self) -> None:
        self.assertEqual(normalize_label("إسلام"), normalize_label("اسلام"))
        self.assertEqual(normalize_label("مُحَمَّد"), normalize_label("محمد"))

    def test_notification_suffix_dropped(self) -> None:
        self.assertEqual(normalize_label("Termux، إشعار واحد"), "termux")
        self.assertEqual(normalize_label("Samsung, 1 notification"), "samsung")


class CollectEntriesTests(unittest.TestCase):
    def test_collects_icons_and_skips_chrome(self) -> None:
        icons = [
            _icon("Termux", short_id="icon_1"),
            _icon("بحث", short_id="app_search_edit_text"),
            _icon("Folder", short_id="folder_icon_view"),
        ]
        labels = [e.label for e in collect_launcher_entries(_tree(icons)["root"])]
        self.assertIn("Termux", labels)
        self.assertIn("Folder", labels)
        self.assertNotIn("بحث", labels)

    def test_duplicate_container_and_view_collapsed(self) -> None:
        icons = [_icon("Clock"), _icon("Clock")]
        self.assertEqual(len(collect_launcher_entries(_tree(icons)["root"])), 1)

    def test_folder_flag_and_center(self) -> None:
        icons = [_icon("Samsung", short_id="folder_icon_container", left=0, top=100, right=200, bottom=300)]
        entry = collect_launcher_entries(_tree(icons)["root"])[0]
        self.assertTrue(entry.is_folder)
        self.assertEqual(entry.center, (100, 200))

    def test_launcher_package_is_not_reported_as_app_package(self) -> None:
        entry = collect_launcher_entries(_tree([_icon("Instagram")])["root"])[0]
        self.assertIsNone(entry.package)


class PickEntryTests(unittest.TestCase):
    def _labels(self, *labels: str) -> list[str]:
        entries = collect_launcher_entries(_tree(_page(*labels))["root"])
        match = pick_entry(entries, labels[0] if len(labels) == 1 else "Calculator")
        return [e.label for e in entries if match is e]

    def test_exact_match_wins_over_substring(self) -> None:
        self.assertEqual(self._labels("Calculator", "Calculator Clock"), ["Calculator"])

    def test_case_insensitive_substring_match(self) -> None:
        entries = collect_launcher_entries(_tree(_page("Samsung Internet", "Camera"))["root"])
        labels = [e.label for e in entries if pick_entry(entries, "internet") is e]
        self.assertEqual(labels, ["Samsung Internet"])

    def test_no_match_returns_none(self) -> None:
        entries = collect_launcher_entries(_tree(_page("Camera"))["root"])
        self.assertIsNone(pick_entry(entries, "Spreadsheet"))


class ResolverLaunchTests(unittest.TestCase):
    def _resolver(self, client: FakeLauncherClient) -> LauncherAppResolver:
        return LauncherAppResolver(client, sleep=_no_sleep)

    def test_launches_by_label_with_home_and_drawer_gestures(self) -> None:
        client = FakeLauncherClient([_page("Instagram", "Termux")])
        result = self._resolver(client).launch("Termux")
        self.assertEqual(result["resolved_by"], "launcher_label")
        self.assertEqual(result["label"], "Termux")
        self.assertEqual(client.keys, ["home"])
        self.assertEqual(len(client.taps), 1)
        self.assertEqual(client.opened, "Termux")

    def test_reads_later_drawer_pages(self) -> None:
        client = FakeLauncherClient(
            [_page("Instagram", "Termux"), _page("Camera"), _page("Calculator")]
        )
        result = self._resolver(client).launch("Calculator")
        self.assertEqual(result["label"], "Calculator")

    def test_opens_folder_then_launches_inner_app(self) -> None:
        client = FakeLauncherClient(
            [_page("Instagram", folders=("Samsung",))],
            folders={"Samsung": _page("Calculator", "Clock")},
        )
        result = self._resolver(client).launch("Calculator")
        self.assertEqual(result["folder"], "Samsung")
        self.assertEqual(result["label"], "Calculator")
        self.assertEqual(result["path"], ["Samsung", "Calculator"])
        self.assertEqual(len(client.taps), 2)

    def test_searches_folders_when_label_is_nested(self) -> None:
        client = FakeLauncherClient(
            [_page("Instagram", folders=("Google", "Microsoft"))],
            folders={
                "Google": _page("Gmail", "Maps"),
                "Microsoft": _page("Calculator", "Clock"),
            },
        )
        result = self._resolver(client).launch("Calculator")
        self.assertEqual(result["label"], "Calculator")
        self.assertEqual(result["path"], ["Microsoft", "Calculator"])
        self.assertEqual(client.backs, 1)

    def test_arabic_label_matches_normalized_query(self) -> None:
        client = FakeLauncherClient([_page("الرسائل", "Camera")])
        result = self._resolver(client).launch("الرسايل")
        self.assertEqual(result["label"], "الرسائل")

    def test_missing_label_raises_with_visible_icons(self) -> None:
        client = FakeLauncherClient([_page("Camera")])
        with self.assertRaises(AppResolutionError) as ctx:
            self._resolver(client).launch("Calculator")
        self.assertEqual(ctx.exception.code, "APP_LABEL_NOT_FOUND")
        self.assertIn("Camera", ctx.exception.message)

    def test_unreadable_launcher_raises(self) -> None:
        client = FakeLauncherClient([[]])
        with self.assertRaises(AppResolutionError) as ctx:
            self._resolver(client).launch("Calculator")
        self.assertEqual(ctx.exception.code, "LAUNCHER_UNREADABLE")

    def test_empty_name_rejected(self) -> None:
        client = FakeLauncherClient([_page("Camera")])
        with self.assertRaises(AppResolutionError):
            self._resolver(client).launch("   ")


class OpenAppToolTests(unittest.TestCase):
    def _ctx(self, client: FakeLauncherClient) -> ToolContext:
        return ToolContext(client=client, extra={"launcher_settle_seconds": 0})

    def test_package_path_still_works(self) -> None:
        client = FakeLauncherClient()
        registry = build_default_registry()
        result = registry.execute("open_app", self._ctx(client), {"package": "com.termux"})
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(client.launched, ["com.termux"])
        self.assertEqual(result.data["resolved_by"], "package")

    def test_name_path_uses_launcher(self) -> None:
        client = FakeLauncherClient([_page("Instagram", "Termux")])
        registry = build_default_registry()
        result = registry.execute("open_app", self._ctx(client), {"name": "Instagram"})
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.data["label"], "Instagram")
        self.assertEqual(client.launched, [])

    def test_wrong_package_falls_back_to_name(self) -> None:
        client = FakeLauncherClient(
            [_page("Calculator")],
            fail_packages={"com.sec.android.app.calculator"},
        )
        registry = build_default_registry()
        result = registry.execute(
            "open_app",
            self._ctx(client),
            {"package": "com.sec.android.app.calculator", "name": "Calculator"},
        )
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.data["package_attempt"], "com.sec.android.app.calculator")
        self.assertEqual(result.data["label"], "Calculator")

    def test_requires_package_or_name(self) -> None:
        registry = build_default_registry()
        valid, errors = registry.validate_args("open_app", {})
        self.assertFalse(valid)
        self.assertTrue(any("package" in e for e in errors))
        result = registry.execute("open_app", self._ctx(FakeLauncherClient()), {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_unknown_label_reports_resolver_code(self) -> None:
        client = FakeLauncherClient([_page("Camera")])
        registry = build_default_registry()
        result = registry.execute("open_app", self._ctx(client), {"name": "Calculator"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "APP_LABEL_NOT_FOUND")


class RemoteErrorPropagationTests(unittest.TestCase):
    def test_device_error_code_reaches_the_result(self) -> None:
        client = FakeLauncherClient(fail_packages={"com.nope"})
        registry = build_default_registry()
        result = registry.execute("open_app", ToolContext(client=client), {"package": "com.nope"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "PACKAGE_NOT_FOUND")


class RepeatGuardTests(unittest.TestCase):
    def test_second_identical_failure_is_blocked(self) -> None:
        client = FakeLauncherClient(fail_packages={"com.nope"})
        registry = build_default_registry()
        ctx = ToolContext(client=client)
        first = registry.execute("open_app", ctx, {"package": "com.nope"})
        second = registry.execute("open_app", ctx, {"package": "com.nope"})
        third = registry.execute("open_app", ctx, {"package": "com.nope"})
        self.assertEqual(first.error_code, "PACKAGE_NOT_FOUND")
        self.assertEqual(second.error_code, "PACKAGE_NOT_FOUND")
        self.assertEqual(third.error_code, "REPEAT_BLOCKED")
        self.assertEqual(client.launched, [])

    def test_successful_call_clears_the_record(self) -> None:
        client = FakeLauncherClient(fail_packages={"com.nope"})
        registry = build_default_registry()
        ctx = ToolContext(client=client)
        registry.execute("open_app", ctx, {"package": "com.nope"})
        client.fail_packages.clear()
        self.assertTrue(registry.execute("open_app", ctx, {"package": "com.nope"}).success)
        self.assertTrue(registry.execute("open_app", ctx, {"package": "com.nope"}).success)

    def test_different_args_are_not_blocked(self) -> None:
        client = FakeLauncherClient(fail_packages={"com.a", "com.b"})
        registry = build_default_registry()
        ctx = ToolContext(client=client)
        registry.execute("open_app", ctx, {"package": "com.a"})
        registry.execute("open_app", ctx, {"package": "com.a"})
        other = registry.execute("open_app", ctx, {"package": "com.b"})
        self.assertEqual(other.error_code, "PACKAGE_NOT_FOUND")

    def test_identical_taps_are_allowed(self) -> None:
        """Pressing the same key twice is legitimate, so taps are never blocked."""
        client = FakeLauncherClient()
        registry = build_default_registry()
        ctx = ToolContext(client=client)
        for _ in range(4):
            self.assertTrue(registry.execute("tap", ctx, {"x": 10, "y": 20}).success)
        self.assertEqual(len(client.taps), 4)


if __name__ == "__main__":
    unittest.main()
