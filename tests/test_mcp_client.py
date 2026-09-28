from __future__ import annotations

import json
import unittest

from agentpro.mcp_client import (
    HTTPMCPTransport,
    MCPClient,
    MCPError,
    MCPResponse,
    MCPTransport,
    MCPToolInfo,
    MCPToolResult,
)


class FakeTransport(MCPTransport):
    """Scripted transport: records requests and replays canned responses."""

    def __init__(self, responses: list[MCPResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict] = []
        self.headers_seen: list[dict] = []

    def post(self, url, body, headers, timeout):  # type: ignore[override]
        payload = json.loads(body.decode("utf-8"))
        self.requests.append(payload)
        self.headers_seen.append(dict(headers))
        if not self.responses:
            return MCPResponse(status=202, headers={}, body=b"")
        return self.responses.pop(0)

    def methods(self) -> list[str]:
        return [r.get("method", "") for r in self.requests]


def rpc_result(result, request_id=1, session=None):
    headers = {"Content-Type": "application/json"}
    if session:
        headers["Mcp-Session-Id"] = session
    body = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
    return MCPResponse(status=200, headers=headers, body=body.encode())


def sse_result(result, event="message"):
    body = f"event: {event}\ndata: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result})}\n\n"
    return MCPResponse(
        status=200, headers={"Content-Type": "text/event-stream"}, body=body.encode()
    )


INIT_RESULT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "fake-higgsfield", "version": "9.9"},
}


def handshake_responses(session="sess-1"):
    return [rpc_result(INIT_RESULT, request_id=1, session=session), MCPResponse(status=202, headers={}, body=b"")]


def call_transport(call_result):
    return FakeTransport(handshake_responses() + [call_result])


def initialized_transport(tools=None, session="sess-1"):
    tools = tools if tools is not None else [
        {
            "name": "use-after-effects",
            "description": "Create motion design in After Effects",
            "inputSchema": {
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
        }
    ]
    return FakeTransport(
        [
            rpc_result(INIT_RESULT, request_id=1, session=session),
            MCPResponse(status=202, headers={}, body=b""),
            rpc_result({"tools": tools}, request_id=2),
        ]
    )


class InitializationTests(unittest.TestCase):
    def test_handshake_sends_initialize_then_notification(self) -> None:
        transport = initialized_transport()
        client = MCPClient("https://mcp.example/mcp", transport=transport)

        info = client.initialize()

        self.assertTrue(client.initialized)
        self.assertEqual(info["name"], "fake-higgsfield")
        self.assertEqual(
            transport.methods(), ["initialize", "notifications/initialized"]
        )
        params = transport.requests[0]["params"]
        self.assertEqual(params["clientInfo"]["name"], "AndroidAgentPro")
        self.assertIn("protocolVersion", params)

    def test_initialize_is_idempotent(self) -> None:
        transport = initialized_transport()
        client = MCPClient("https://mcp.example/mcp", transport=transport)

        client.initialize()
        client.initialize()

        self.assertEqual(transport.methods().count("initialize"), 1)

    def test_session_id_is_remembered_and_replayed(self) -> None:
        transport = initialized_transport(session="abc123")
        client = MCPClient("https://mcp.example/mcp", transport=transport)

        client.initialize()
        client.list_tools()

        self.assertEqual(client.session_id, "abc123")
        self.assertEqual(transport.headers_seen[-1]["Mcp-Session-Id"], "abc123")

    def test_negotiated_protocol_version_wins(self) -> None:
        transport = initialized_transport()
        client = MCPClient("https://mcp.example/mcp", transport=transport)

        client.initialize()

        self.assertEqual(client.protocol_version, "2025-06-18")

    def test_close_clears_session(self) -> None:
        transport = initialized_transport()
        client = MCPClient("https://mcp.example/mcp", transport=transport)
        client.initialize()

        client.close()

        self.assertIsNone(client.session_id)
        self.assertFalse(client.initialized)

    def test_context_manager_initializes(self) -> None:
        transport = initialized_transport()
        with MCPClient("https://mcp.example/mcp", transport=transport) as client:
            self.assertTrue(client.initialized)

    def test_rejects_empty_url(self) -> None:
        with self.assertRaises(ValueError):
            MCPClient("  ")

    def test_rejects_non_positive_timeout(self) -> None:
        with self.assertRaises(ValueError):
            MCPClient("https://mcp.example/mcp", timeout=0)


class ListToolsTests(unittest.TestCase):
    def test_parses_tools(self) -> None:
        transport = initialized_transport()
        client = MCPClient("https://mcp.example/mcp", transport=transport)

        tools = client.list_tools()

        self.assertEqual([t.name for t in tools], ["use-after-effects"])
        self.assertEqual(tools[0].argument_names(), ["prompt"])

    def test_follows_pagination_cursor(self) -> None:
        transport = FakeTransport(
            [
                rpc_result(INIT_RESULT, request_id=1),
                MCPResponse(status=202, headers={}, body=b""),
                rpc_result(
                    {"tools": [{"name": "a"}], "nextCursor": "page2"}, request_id=2
                ),
                rpc_result({"tools": [{"name": "b"}]}, request_id=3),
            ]
        )
        client = MCPClient("https://mcp.example/mcp", transport=transport)

        tools = client.list_tools()

        self.assertEqual([t.name for t in tools], ["a", "b"])
        self.assertEqual(transport.requests[-1]["params"], {"cursor": "page2"})

    def test_results_are_cached_until_refresh(self) -> None:
        transport = initialized_transport()
        client = MCPClient("https://mcp.example/mcp", transport=transport)

        client.list_tools()
        client.list_tools()

        self.assertEqual(transport.methods().count("tools/list"), 1)
        self.assertEqual(transport.responses, [])

        transport.responses.append(
            rpc_result({"tools": [{"name": "fresh"}]}, request_id=3)
        )
        client.list_tools(refresh=True)
        self.assertEqual(transport.methods().count("tools/list"), 2)

    def test_tool_entry_without_name_is_rejected(self) -> None:
        with self.assertRaises(MCPError):
            MCPToolInfo.from_payload({"description": "nameless"})


class EventStreamTests(unittest.TestCase):
    def test_parses_sse_body(self) -> None:
        transport = FakeTransport(
            [
                rpc_result(INIT_RESULT, request_id=1),
                MCPResponse(status=202, headers={}, body=b""),
                sse_result({"tools": [{"name": "color-grade", "description": "Grade"}]}),
            ]
        )
        client = MCPClient("https://mcp.example/mcp", transport=transport)

        tools = client.list_tools()

        self.assertEqual([t.name for t in tools], ["color-grade"])

    def test_ignores_keepalive_comments(self) -> None:
        body = (
            ": keepalive\n\n"
            f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'tools': []}})}\n\n"
        )
        response = MCPResponse(
            status=200, headers={"Content-Type": "text/event-stream"}, body=body.encode()
        )

        message = MCPClient._extract(response)

        self.assertEqual(message["result"], {"tools": []})

    def test_sse_without_result_raises(self) -> None:
        response = MCPResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            body=b"event: progress\ndata: {\"progress\": 1}\n\n",
        )

        with self.assertRaises(MCPError):
            MCPClient._extract(response)


class CallToolTests(unittest.TestCase):
    def _client(self, call_result, tools=None):
        transport = call_transport(rpc_result(call_result, request_id=3))
        client = MCPClient(
            "https://mcp.example/mcp", transport=transport, initialize=False
        )
        return client, transport

    def test_returns_text_content(self) -> None:
        client, transport = self._client(
            {"content": [{"type": "text", "text": "project saved"}]}
        )

        result = client.call_tool("use-after-effects", {"prompt": "intro"})

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "project saved")
        call = transport.requests[-1]
        self.assertEqual(call["method"], "tools/call")
        self.assertEqual(
            call["params"], {"name": "use-after-effects", "arguments": {"prompt": "intro"}}
        )

    def test_joins_multiple_text_blocks(self) -> None:
        client, _ = self._client(
            {
                "content": [
                    {"type": "text", "text": "line one"},
                    {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                    {"type": "text", "text": "line two"},
                ]
            }
        )

        result = client.call_tool("render")

        self.assertEqual(result.text, "line one\nline two")
        self.assertEqual(len(result.images), 1)
        self.assertIn("image/png", result.summary())

    def test_structured_content_is_preserved(self) -> None:
        client, _ = self._client(
            {"content": [], "structuredContent": {"variants": 100}}
        )

        result = client.call_tool("ad-variants")

        self.assertEqual(result.structured, {"variants": 100})

    def test_is_error_flag_is_surfaced(self) -> None:
        client, _ = self._client(
            {"content": [{"type": "text", "text": "quota exceeded"}], "isError": True}
        )

        result = client.call_tool("render")

        self.assertFalse(result.ok)
        self.assertIn("quota exceeded", result.text)

    def test_jsonrpc_error_raises(self) -> None:
        transport = call_transport(
            MCPResponse(
                status=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                ).encode(),
            )
        )
        client = MCPClient(
            "https://mcp.example/mcp", transport=transport, initialize=False
        )

        with self.assertRaises(MCPError) as ctx:
            client.call_tool("nope")

        self.assertIn("Method not found", str(ctx.exception))

    def test_empty_name_is_rejected(self) -> None:
        client, _ = self._client({"content": []})
        with self.assertRaises(ValueError):
            client.call_tool("")

    def test_auto_initializes_before_call(self) -> None:
        transport = call_transport(rpc_result({"content": []}, request_id=3))
        client = MCPClient(
            "https://mcp.example/mcp", transport=transport, initialize=False
        )

        client.call_tool("use-after-effects")

        self.assertEqual(
            transport.methods(),
            ["initialize", "notifications/initialized", "tools/call"],
        )


class ErrorHandlingTests(unittest.TestCase):
    def test_retries_server_error_then_succeeds(self) -> None:
        transport = FakeTransport(
            [
                rpc_result(INIT_RESULT, request_id=1),
                MCPResponse(status=202, headers={}, body=b""),
                MCPResponse(status=503, headers={}, body=b"overloaded"),
                rpc_result({"tools": [{"name": "ok"}]}, request_id=2),
            ]
        )
        client = MCPClient(
            "https://mcp.example/mcp", transport=transport, backoff=0.0
        )

        tools = client.list_tools()

        self.assertEqual([t.name for t in tools], ["ok"])
        self.assertEqual(transport.methods().count("tools/list"), 2)

    def test_gives_up_after_max_retries(self) -> None:
        transport = FakeTransport(
            [MCPResponse(status=500, headers={}, body=b"boom")] * 4
        )
        client = MCPClient(
            "https://mcp.example/mcp",
            transport=transport,
            backoff=0.0,
            max_retries=1,
        )

        with self.assertRaises(MCPError) as ctx:
            client.initialize()

        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertEqual(len(transport.requests), 2)

    def test_client_error_is_not_retried(self) -> None:
        transport = FakeTransport(
            [MCPResponse(status=401, headers={}, body=b"unauthorized")]
        )
        client = MCPClient("https://mcp.example/mcp", transport=transport, backoff=0.0)

        with self.assertRaises(MCPError) as ctx:
            client.initialize()

        self.assertIn("unauthorized", str(ctx.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_transport_exception_is_retried_then_raised(self) -> None:
        class BrokenTransport(MCPTransport):
            def __init__(self) -> None:
                self.calls = 0

            def post(self, url, body, headers, timeout):
                self.calls += 1
                raise OSError("network unreachable")

        transport = BrokenTransport()
        client = MCPClient(
            "https://mcp.example/mcp",
            transport=transport,
            backoff=0.0,
            max_retries=2,
        )

        with self.assertRaises(MCPError) as ctx:
            client.initialize()

        self.assertIn("network unreachable", str(ctx.exception))
        self.assertEqual(transport.calls, 3)

    def test_non_json_body_raises(self) -> None:
        transport = FakeTransport(
            [MCPResponse(status=200, headers={"Content-Type": "text/html"}, body=b"<html>")]
        )
        client = MCPClient("https://mcp.example/mcp", transport=transport, backoff=0.0)

        with self.assertRaises(MCPError):
            client.initialize()

    def test_empty_body_raises(self) -> None:
        transport = FakeTransport([MCPResponse(status=200, headers={}, body=b"")])
        client = MCPClient("https://mcp.example/mcp", transport=transport, backoff=0.0)

        with self.assertRaises(MCPError):
            client.initialize()


class ResultDecodingTests(unittest.TestCase):
    def test_summary_truncates(self) -> None:
        result = MCPToolResult(
            tool="t", content=({"type": "text", "text": "x" * 5000},)
        )

        self.assertLessEqual(len(result.summary(limit=100)), 103)

    def test_save_images_writes_files(self) -> None:
        import base64
        import os
        import tempfile

        payload = base64.b64encode(b"fake-png-bytes").decode()
        result = MCPToolResult(
            tool="render",
            content=({"type": "image", "data": payload, "mimeType": "image/png"},),
        )

        with tempfile.TemporaryDirectory() as tmp:
            paths = result.save_images(tmp, prefix="shot")
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].endswith(".png"))
            with open(paths[0], "rb") as handle:
                self.assertEqual(handle.read(), b"fake-png-bytes")

    def test_empty_result_summary(self) -> None:
        self.assertEqual(MCPToolResult(tool="t").summary(), "(empty result)")


class UrllibTransportTests(unittest.TestCase):
    def test_post_builds_request(self) -> None:
        captured = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = request.data
            captured["timeout"] = timeout
            return FakeResponse()

        transport = HTTPMCPTransport(opener=opener)
        response = transport.post(
            "https://mcp.example/mcp",
            b'{"jsonrpc":"2.0"}',
            {"Content-Type": "application/json"},
            12.5,
        )

        self.assertEqual(captured["url"], "https://mcp.example/mcp")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 12.5)
        self.assertEqual(response.status, 200)
        self.assertIn("ok", response.text())

    def test_header_lookup_is_case_insensitive(self) -> None:
        response = MCPResponse(status=200, headers={"mcp-session-id": "xyz"})

        self.assertEqual(response.header("Mcp-Session-Id"), "xyz")
        self.assertIsNone(response.header("Missing"))


if __name__ == "__main__":
    unittest.main()
