from __future__ import annotations

import unittest

from agentpro.agent_runner import RecordingBridgeClient
from agentpro.bridge_tools import (
    PermissionLevel,
    Tool,
    ToolError,
    ToolRegistry,
    ToolSpec,
    build_default_registry,
)
from agentpro.models import ActionType
from agentpro.screen import ScreenReader


def _registry_with_recorder() -> tuple[ToolRegistry, RecordingBridgeClient]:
    registry = build_default_registry()
    client = RecordingBridgeClient()
    return registry, client


class RegistryTests(unittest.TestCase):
    def test_default_tools_present(self) -> None:
        registry = build_default_registry()
        names = registry.names()
        for expected in (
            "tap",
            "tap_element",
            "long_press",
            "type_text",
            "clear_text",
            "erase_text",
            "swipe",
            "scroll",
            "back",
            "home",
            "recents",
            "key_event",
            "open_url",
            "open_app",
            "get_window_info",
            "dump_ui",
            "screenshot",
            "wait",
            "wait_for_text",
            "wait_for_screen_stable",
            "install_apk",
            "shell",
        ):
            self.assertIn(expected, names)

    def test_dangerous_closed_by_default(self) -> None:
        registry = build_default_registry()
        self.assertFalse(registry.is_enabled("install_apk"))
        self.assertFalse(registry.is_enabled("shell"))
        install = registry.get("install_apk")
        shell = registry.get("shell")
        self.assertEqual(install.spec.permission, PermissionLevel.OWNER)
        self.assertEqual(shell.spec.permission, PermissionLevel.OWNER)

    def test_action_type_mapping(self) -> None:
        registry = build_default_registry()
        mappings = {
            "tap": ActionType.TAP,
            "tap_element": ActionType.TAP,
            "long_press": ActionType.LONG_PRESS,
            "type_text": ActionType.INPUT_TEXT,
            "clear_text": ActionType.CLEAR_TEXT,
            "erase_text": ActionType.ERASE_TEXT,
            "swipe": ActionType.SWIPE,
            "open_url": ActionType.OPEN_URL,
            "get_window_info": ActionType.GET_WINDOW,
        }
        for name, expected in mappings.items():
            self.assertEqual(registry.get(name).to_action_type(), expected)

    def test_duplicate_registration_rejected(self) -> None:
        registry = ToolRegistry()
        spec = ToolSpec(
            name="dup",
            description="first",
            schema={"type": "object", "properties": {}},
        )
        registry.register(_NoopTool(spec))
        with self.assertRaises(ToolError):
            registry.register(_NoopTool(spec))

    def test_register_non_tool_rejected(self) -> None:
        registry = ToolRegistry()
        with self.assertRaises(ToolError):
            registry.register("tap")  # type: ignore[arg-type]


class _NoopTool(Tool):
    def execute(self, ctx, args):
        from agentpro.bridge_tools import ToolResult

        return ToolResult.ok()


class ToolExecutionTests(unittest.TestCase):
    def test_tap_execution(self) -> None:
        registry, client = _registry_with_recorder()
        from agentpro.bridge_tools import ToolContext

        ctx = ToolContext(client=client)
        result = registry.execute("tap", ctx, {"x": 10, "y": 20})
        self.assertTrue(result.success)
        self.assertIn("tap", client.commands)
        tap_call = [args for cmd, args in client.calls if cmd == "tap"][0]
        self.assertEqual(tap_call, {"x": 10, "y": 20})

    def test_tap_element_by_index(self) -> None:
        from agentpro.bridge_tools import ToolContext
        from agentpro.screen import ScreenReader

        registry, client = _registry_with_recorder()
        snapshot = ScreenReader(
            RecordingBridgeClient(
                ui_root={
                    "class": "android.widget.FrameLayout",
                    "package": "com.example",
                    "children": [
                        {
                            "class": "android.widget.TextView",
                            "text": "Save",
                            "bounds": "[100,100][300,160]",
                            "clickable": True,
                        },
                    ],
                }
            ),
            include_screenshot=False,
        ).observe()
        ctx = ToolContext(client=client, snapshot=snapshot)
        result = registry.execute("tap_element", ctx, {"index": 0})
        self.assertTrue(result.success)
        self.assertEqual(result.data.get("element_index"), 0)
        tap_call = [args for cmd, args in client.calls if cmd == "tap"][-1]
        self.assertEqual(tap_call["x"], 200)
        self.assertEqual(tap_call["y"], 130)

    def test_tap_element_by_text(self) -> None:
        from agentpro.bridge_tools import ToolContext
        from agentpro.screen import ScreenReader

        registry, client = _registry_with_recorder()
        snapshot = ScreenReader(
            RecordingBridgeClient(
                ui_root={
                    "class": "android.widget.FrameLayout",
                    "package": "com.example",
                    "children": [
                        {
                            "class": "android.widget.TextView",
                            "text": "Save",
                            "bounds": "[100,100][300,160]",
                            "clickable": True,
                        },
                        {
                            "class": "android.widget.TextView",
                            "text": "Cancel",
                            "bounds": "[100,200][300,260]",
                            "clickable": True,
                        },
                    ],
                }
            ),
            include_screenshot=False,
        ).observe()
        ctx = ToolContext(client=client, snapshot=snapshot)
        result = registry.execute("tap_element", ctx, {"text": "cancel"})
        self.assertTrue(result.success)
        tap_call = [args for cmd, args in client.calls if cmd == "tap"][-1]
        self.assertEqual(tap_call["y"], 230)

    def test_tap_element_text_prefers_clickable(self) -> None:
        from agentpro.bridge_tools import ToolContext
        from agentpro.screen import ScreenReader

        registry, client = _registry_with_recorder()
        snapshot = ScreenReader(
            RecordingBridgeClient(
                ui_root={
                    "class": "android.widget.FrameLayout",
                    "package": "com.example",
                    "children": [
                        {
                            "class": "android.widget.EditText",
                            "text": "5",
                            "bounds": "[0,0][980,100]",
                            "editable": True,
                        },
                        {
                            "class": "android.widget.TextView",
                            "text": "5",
                            "bounds": "[100,500][340,650]",
                            "clickable": True,
                        },
                    ],
                }
            ),
            include_screenshot=False,
        ).observe()
        ctx = ToolContext(client=client, snapshot=snapshot)
        result = registry.execute("tap_element", ctx, {"text": "5"})
        self.assertTrue(result.success)
        tap_call = [args for cmd, args in client.calls if cmd == "tap"][-1]
        self.assertGreater(tap_call["y"], 400)

    def test_tap_element_missing_target(self) -> None:
        from agentpro.bridge_tools import ToolContext
        from agentpro.screen import ScreenReader

        registry, client = _registry_with_recorder()
        snapshot = ScreenReader(
            RecordingBridgeClient(
                ui_root={
                    "class": "android.widget.FrameLayout",
                    "package": "com.example",
                    "children": [],
                }
            ),
            include_screenshot=False,
        ).observe()
        ctx = ToolContext(client=client, snapshot=snapshot)
        result = registry.execute("tap_element", ctx, {"text": "nope"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_TARGET_NOT_FOUND")

    def test_tap_element_requires_target(self) -> None:
        registry, client = _registry_with_recorder()
        from agentpro.bridge_tools import ToolContext

        result = registry.execute(
            "tap_element", ToolContext(client=client), {}
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_erase_text_execution(self) -> None:
        registry, client = _registry_with_recorder()
        from agentpro.bridge_tools import ToolContext

        result = registry.execute(
            "erase_text", ToolContext(client=client), {}
        )
        self.assertTrue(result.success)
        self.assertIn("erase_text", client.commands)

    def test_scroll_dims_from_snapshot(self) -> None:
        registry, client = _registry_with_recorder()
        from agentpro.bridge_tools import ToolContext
        from agentpro.screen import ScreenReader

        snapshot = ScreenReader(
            RecordingBridgeClient(
                ui_root={
                    "class": "android.widget.FrameLayout",
                    "package": "com.example",
                    "children": [
                        {
                            "class": "android.widget.TextView",
                            "text": "tall",
                            "bounds": "[0,0][500,1000]",
                            "scrollable": True,
                        },
                    ],
                }
            ),
            include_screenshot=False,
        ).observe()
        ctx = ToolContext(client=client, snapshot=snapshot)
        waiter = registry.execute(
            "scroll",
            ctx,
            {"direction": "down", "distance": 400},
        )
        self.assertTrue(waiter.success)
        swipe_args = [args for cmd, args in client.calls if cmd == "swipe"]
        self.assertEqual(len(swipe_args), 1)
        self.assertEqual(swipe_args[0]["x1"], 250)
        self.assertEqual(swipe_args[0]["y1"], 300)
        self.assertEqual(swipe_args[0]["y2"], 700)

    def test_invalid_arguments_blocked(self) -> None:
        registry, client = _registry_with_recorder()
        from agentpro.bridge_tools import ToolContext

        ctx = ToolContext(client=client)
        result = registry.execute("tap", ctx, {"x": "bad"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")
        self.assertNotIn("tap", client.commands)

    def test_missing_required_argument(self) -> None:
        registry, _client = _registry_with_recorder()
        valid, errors = registry.validate_args("open_app", {})
        self.assertFalse(valid)
        self.assertTrue(any("package" in e for e in errors))

    def test_dangerous_gated(self) -> None:
        registry, client = _registry_with_recorder()
        from agentpro.bridge_tools import ToolContext

        ctx = ToolContext(client=client)
        result = registry.execute("shell", ctx, {"command": "id"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_GATED")

    def test_enable_then_gated(self) -> None:
        registry, _client = _registry_with_recorder()
        registry.enable("shell", enabled=True)
        self.assertTrue(registry.is_enabled("shell"))

    def test_unknown_tool(self) -> None:
        registry, client = _registry_with_recorder()
        from agentpro.bridge_tools import ToolContext

        result = registry.execute("does_not_exist", ToolContext(client=client), {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNKNOWN_TOOL")

    def test_scroll_uses_dims(self) -> None:
        registry, client = _registry_with_recorder()
        from agentpro.bridge_tools import ToolContext

        ctx = ToolContext(client=client, extra={"screenshot_dims": (1080, 2400)})
        waiter = registry.execute("scroll", ctx, {"direction": "down"})
        self.assertTrue(waiter.success)
        swipe_args = [args for cmd, args in client.calls if cmd == "swipe"]
        self.assertEqual(len(swipe_args), 1)


class ToolResultTests(unittest.TestCase):
    def test_ok_and_error(self) -> None:
        from agentpro.bridge_tools import ToolResult

        ok = ToolResult.ok(a=1)
        self.assertTrue(ok.success)
        self.assertEqual(ok.data, {"a": 1})

        err = ToolResult.error("CODE", "msg")
        self.assertFalse(err.success)
        self.assertEqual(err.error_code, "CODE")


if __name__ == "__main__":
    unittest.main()