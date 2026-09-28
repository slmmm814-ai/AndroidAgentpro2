from __future__ import annotations

import unittest

from agentpro.bridge_tools import ToolContext, ToolRegistry, build_default_registry
from agentpro.mcp_client import MCPClient, MCPResponse, MCPTransport, MCPToolResult
from agentpro.mcp_tools import (
    ENV_MCP_TIMEOUT,
    ENV_MCP_URL,
    mcp_client_from_env,
    normalize_schema,
    register_mcp_tools,
    sanitize_tool_name,
)
from agentpro.models import ActionType

REMOTE_TOOLS = [
    {
        "name": "use-after-effects",
        "description": "Create motion design in After Effects",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "what to build"},
                "duration": {"type": "number", "minimum": 1},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "marketing/adapt-video",
        "description": "Turn a reference video into an ad",
        "inputSchema": {
            "type": "object",
            "properties": {"reference": {"type": "string"}},
            "required": ["reference"],
            "$schema": "https://json-schema.org/draft/2020-12/schema",
        },
    },
    {"name": "ping", "description": "Liveness probe"},
]


class ScriptedTransport(MCPTransport):
    def __init__(self, call_result=None) -> None:
        self.tools = list(REMOTE_TOOLS)
        self.call_result = call_result or {
            "content": [{"type": "text", "text": "render finished"}]
        }
        self.calls: list[tuple[str, dict]] = []
        self.fail_with: Exception | None = None

    def post(self, url, body, headers, timeout):
        import json

        payload = json.loads(body.decode("utf-8"))
        method = payload.get("method", "")
        if method == "initialize":
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-higgsfield", "version": "1.0"},
                    },
                }
            )
            return MCPResponse(
                status=200,
                headers={"Content-Type": "application/json", "Mcp-Session-Id": "s1"},
                body=body.encode(),
            )
        if method == "notifications/initialized":
            return MCPResponse(status=202, headers={}, body=b"")
        if method == "tools/list":
            body = json.dumps(
                {"jsonrpc": "2.0", "id": 2, "result": {"tools": self.tools}}
            )
            return MCPResponse(
                status=200, headers={"Content-Type": "application/json"}, body=body.encode()
            )
        if method == "tools/call":
            self.calls.append((payload["params"]["name"], payload["params"]["arguments"]))
            if self.fail_with is not None:
                raise self.fail_with
            body = json.dumps(
                {"jsonrpc": "2.0", "id": 3, "result": self.call_result}
            )
            return MCPResponse(
                status=200, headers={"Content-Type": "application/json"}, body=body.encode()
            )
        return MCPResponse(status=202, headers={}, body=b"")


def _client(transport=None):
    return MCPClient(
        "https://mcp.higgsfield.ai/mcp", transport=transport or ScriptedTransport()
    )


def _registry():
    return ToolRegistry()


class NameTests(unittest.TestCase):
    def test_prefixes_and_sanitizes(self) -> None:
        self.assertEqual(sanitize_tool_name("use-after-effects"), "hf_use-after-effects")

    def test_replaces_slashes_and_spaces(self) -> None:
        self.assertEqual(
            sanitize_tool_name("marketing/adapt video", "mcp_"),
            "mcp_marketing_adapt_video",
        )

    def test_keeps_collons_out(self) -> None:
        self.assertNotIn(":", sanitize_tool_name("skills:render"))

    def test_empty_name_falls_back(self) -> None:
        self.assertEqual(sanitize_tool_name("///", "x_"), "x_tool")

    def test_custom_prefix(self) -> None:
        self.assertEqual(sanitize_tool_name("render", "mcp_"), "mcp_render")


class SchemaTests(unittest.TestCase):
    def test_adds_object_type_and_keeps_properties(self) -> None:
        schema = normalize_schema(
            {"properties": {"a": {"type": "string"}}, "required": ["a", 7]}
        )

        self.assertEqual(schema["type"], "object")
        self.assertIn("a", schema["properties"])
        self.assertEqual(schema["required"], ["a", "7"])

    def test_missing_properties_becomes_empty_object(self) -> None:
        schema = normalize_schema(None)

        self.assertEqual(schema, {"type": "object", "properties": {}, "required": []})

    def test_strips_schema_noise(self) -> None:
        schema = normalize_schema(
            {"properties": {}, "$schema": "x", "additionalProperties": False}
        )

        self.assertNotIn("$schema", schema)
        self.assertNotIn("additionalProperties", schema)


class RegistrationTests(unittest.TestCase):
    def test_registers_every_remote_tool_plus_list_tool(self) -> None:
        registry = _registry()

        names = register_mcp_tools(registry, _client())

        self.assertEqual(
            names,
            ["hf_use-after-effects", "hf_marketing_adapt-video", "hf_ping", "mcp_list_tools"],
        )
        for name in names:
            self.assertTrue(registry.is_enabled(name), name)

    def test_joins_default_registry(self) -> None:
        registry = build_default_registry(mcp=_client())

        self.assertIn("tap", registry.names())
        self.assertIn("mcp_list_tools", registry.names())
        self.assertIn("hf_use-after-effects", registry.enabled_names())
        self.assertIn("hf_use-after-effects", registry.specs_for_prompt())

    def test_default_registry_without_mcp_is_unchanged(self) -> None:
        registry = build_default_registry()

        self.assertNotIn("mcp_list_tools", registry.names())
        self.assertFalse([n for n in registry.names() if n.startswith("hf_")])
        self.assertFalse(registry.is_enabled("shell"))

    def test_include_filter_uses_remote_names(self) -> None:
        registry = _registry()

        names = register_mcp_tools(
            registry, _client(), include=["marketing/*"], add_list_tool=False
        )

        self.assertEqual(names, ["hf_marketing_adapt-video"])

    def test_exclude_filter(self) -> None:
        registry = _registry()

        names = register_mcp_tools(
            registry, _client(), exclude=["ping", "use-*"], add_list_tool=False
        )

        self.assertEqual(names, ["hf_marketing_adapt-video"])

    def test_max_tools_caps_registration(self) -> None:
        registry = _registry()

        names = register_mcp_tools(registry, _client(), max_tools=1, add_list_tool=False)

        self.assertEqual(len(names), 1)

    def test_name_collision_gets_suffix(self) -> None:
        registry = _registry()
        register_mcp_tools(registry, _client(), prefix="", add_list_tool=False)

        second = register_mcp_tools(registry, _client(), prefix="", add_list_tool=False)

        self.assertIn("use-after-effects_2", second)

    def test_custom_prefix(self) -> None:
        registry = _registry()

        names = register_mcp_tools(registry, _client(), prefix="mcp_", add_list_tool=False)

        self.assertTrue(all(n.startswith("mcp_") for n in names))

    def test_disabled_registration_is_gated(self) -> None:
        registry = _registry()

        register_mcp_tools(registry, _client(), enabled=False, add_list_tool=False)

        self.assertFalse(registry.is_enabled("hf_ping"))
        result = registry.execute("hf_ping", ToolContext(client=None), {})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_GATED")

    def test_collision_with_builtin_name_is_suffixed(self) -> None:
        registry = build_default_registry()
        transport = ScriptedTransport()
        transport.tools = [{"name": "screenshot", "description": "remote screenshot"}]

        names = register_mcp_tools(registry, _client(transport), add_list_tool=False)

        self.assertEqual(names, ["hf_screenshot"])
        self.assertIn("screenshot", registry.names())


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = ScriptedTransport()
        self.client = _client(self.transport)
        self.registry = ToolRegistry()
        register_mcp_tools(self.registry, self.client)
        self.ctx = ToolContext(client=None)

    def test_successful_call_returns_text(self) -> None:
        result = self.registry.execute(
            "hf_use-after-effects", self.ctx, {"prompt": "logo reveal", "duration": 4}
        )

        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.data["text"], "render finished")
        self.assertEqual(result.data["remote_tool"], "use-after-effects")
        self.assertEqual(
            self.transport.calls, [("use-after-effects", {"prompt": "logo reveal", "duration": 4})]
        )

    def test_required_argument_enforced_locally(self) -> None:
        result = self.registry.execute("hf_use-after-effects", self.ctx, {})

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")
        self.assertEqual(self.transport.calls, [])

    def test_argument_type_enforced_locally(self) -> None:
        result = self.registry.execute(
            "hf_use-after-effects", self.ctx, {"prompt": "x", "duration": "long"}
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_ARGS")

    def test_remote_is_error_maps_to_tool_error(self) -> None:
        self.transport.call_result = {
            "content": [{"type": "text", "text": "quota exceeded"}],
            "isError": True,
        }

        result = self.registry.execute("hf_ping", self.ctx, {})

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "MCP_TOOL_ERROR")
        self.assertIsNotNone(result.error_message)
        self.assertIn("quota exceeded", result.error_message or "")
        self.assertIn("quota exceeded", result.data["text"])

    def test_transport_failure_maps_to_error_code(self) -> None:
        self.transport.fail_with = OSError("connection reset")

        result = self.registry.execute("hf_ping", self.ctx, {})

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "MCP_TRANSPORT_ERROR")
        self.assertIsNotNone(result.error_message)
        self.assertIn("connection reset", result.error_message or "")

    def test_image_blocks_are_summarized(self) -> None:
        self.transport.call_result = {
            "content": [
                {"type": "text", "text": "done"},
                {"type": "image", "data": "A" * 120, "mimeType": "image/png"},
            ]
        }

        result = self.registry.execute("hf_ping", self.ctx, {})

        self.assertTrue(result.success)
        self.assertEqual(result.data["images"][0]["mimeType"], "image/png")
        self.assertEqual(result.data["images"][0]["base64_chars"], 120)

    def test_structured_content_is_exposed(self) -> None:
        self.transport.call_result = {
            "content": [],
            "structuredContent": {"variants": 100},
        }

        result = self.registry.execute("hf_ping", self.ctx, {})

        self.assertEqual(result.data["structured"], {"variants": 100})

    def test_action_type_is_mcp_call(self) -> None:
        tool = self.registry.get("hf_use-after-effects")

        self.assertIsNotNone(tool)
        self.assertEqual(tool.to_action_type() if tool else None, ActionType.MCP_CALL)

    def test_list_tool_filters_and_counts(self) -> None:
        result = self.registry.execute("mcp_list_tools", self.ctx, {"filter": "MARKETING"})

        self.assertTrue(result.success)
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.data["tools"][0]["name"], "marketing/adapt-video")
        self.assertEqual(result.data["tools"][0]["arguments"], ["reference"])

    def test_list_tool_without_filter_lists_all(self) -> None:
        result = self.registry.execute("mcp_list_tools", self.ctx, {})

        self.assertEqual(result.data["count"], 3)

    def test_authorization_hook_can_deny_mcp_calls(self) -> None:
        self.registry.set_authorization_hook(lambda payload: False)

        result = self.registry.execute("hf_ping", self.ctx, {})

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TOOL_DENIED_BY_HOOK")
        self.assertEqual(self.transport.calls, [])

    def test_repeat_breaker_stops_identical_failures(self) -> None:
        self.transport.call_result = {
            "content": [{"type": "text", "text": "nope"}],
            "isError": True,
        }

        first = self.registry.execute("hf_ping", self.ctx, {})
        second = self.registry.execute("hf_ping", self.ctx, {})
        third = self.registry.execute("hf_ping", self.ctx, {})

        self.assertEqual(first.error_code, "MCP_TOOL_ERROR")
        self.assertEqual(second.error_code, "MCP_TOOL_ERROR")
        self.assertEqual(third.error_code, "REPEAT_BLOCKED")
        self.assertEqual(len(self.transport.calls), 2)


class EnvTests(unittest.TestCase):
    def test_defaults_to_higgsfield(self) -> None:
        transport = ScriptedTransport()

        client = mcp_client_from_env(transport=transport)

        self.assertEqual(client.url, "https://mcp.higgsfield.ai/mcp")
        self.assertTrue(client.initialized)

    def test_env_overrides_url_and_timeout(self) -> None:
        import os

        os.environ[ENV_MCP_URL] = "https://mcp.internal/mcp"
        os.environ[ENV_MCP_TIMEOUT] = "7.5"
        try:
            client = mcp_client_from_env(transport=ScriptedTransport())
            self.assertEqual(client.url, "https://mcp.internal/mcp")
            self.assertEqual(client.timeout, 7.5)
        finally:
            os.environ.pop(ENV_MCP_URL, None)
            os.environ.pop(ENV_MCP_TIMEOUT, None)

    def test_invalid_timeout_falls_back(self) -> None:
        import os

        os.environ[ENV_MCP_TIMEOUT] = "not-a-number"
        try:
            client = mcp_client_from_env(transport=ScriptedTransport())
            self.assertEqual(client.timeout, 30.0)
        finally:
            os.environ.pop(ENV_MCP_TIMEOUT, None)

    def test_skip_initialization(self) -> None:
        client = mcp_client_from_env(transport=ScriptedTransport(), initialize=False)

        self.assertFalse(client.initialized)


class PackageExportTests(unittest.TestCase):
    def test_exports_available(self) -> None:
        import agentpro

        for name in (
            "MCPClient",
            "MCPError",
            "MCPToolInfo",
            "MCPToolResult",
            "MCPTransport",
            "HTTPMCPTransport",
            "MCPResponse",
            "DEFAULT_MCP_URL",
            "DEFAULT_MCP_PREFIX",
            "register_mcp_tools",
            "mcp_client_from_env",
            "sanitize_tool_name",
            "normalize_schema",
        ):
            self.assertTrue(hasattr(agentpro, name), name)
            self.assertIn(name, agentpro.__all__)

    def test_action_type_exposed(self) -> None:
        self.assertEqual(ActionType.MCP_CALL.value, "mcp_call")

    def test_result_constructor_is_usable_standalone(self) -> None:
        result = MCPToolResult(tool="x", content=({"type": "text", "text": "hi"},))

        self.assertEqual(result.text, "hi")


if __name__ == "__main__":
    unittest.main()
