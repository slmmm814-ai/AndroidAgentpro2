"""Tests for the opt-in environment wiring of MCP tools into the agent."""

import json
import os
import unittest

from agentpro.bridge_tools import ToolRegistry
from agentpro.mcp_client import MCPResponse
from agentpro.mcp_oauth import (
    AuthorizationServerMetadata,
    ClientRegistration,
    MCPOAuthClient,
    OAuthTransport,
    TokenSet,
)
from agentpro.mcp_tools import (
    ENV_MCP,
    ENV_MCP_EXCLUDE,
    ENV_MCP_INCLUDE,
    ENV_MCP_PREFIX,
    ENV_MCP_URL,
    MCPAttachment,
    attach_mcp_from_env,
    mcp_enabled_from_env,
)

TOOLS = {
    "tools": [
        {
            "name": "marketing/ads",
            "description": "make an ad",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
        {"name": "hf_image", "description": "image", "inputSchema": {"type": "object"}},
    ]
}


class FakeTransport(OAuthTransport):
    def get_json(self, url, timeout):
        return {}

    def post_form(self, url, data, timeout):
        return {}

    def post_json(self, url, payload, timeout):
        return {}


class StubClient:
    """A minimal stand-in for :class:`agentpro.mcp_client.MCPClient`."""

    def __init__(self, url="https://mcp.example/mcp", tools=None, error=None):
        self.url = url
        self.initialized = True
        self._tools = tools if tools is not None else TOOLS
        self._error = error

    def list_tools(self, refresh=True):
        if self._error:
            raise self._error
        from agentpro.mcp_client import MCPToolInfo

        return tuple(
            MCPToolInfo(
                name=item["name"],
                description=item.get("description", ""),
                input_schema=item.get("inputSchema", {}),
            )
            for item in self._tools["tools"]
        )

    def call_tool(self, name, arguments=None):
        from agentpro.mcp_client import MCPToolResult

        return MCPToolResult(
            tool=name, content=({"type": "text", "text": "ok"},)
        )


class EnvIsolationTestCase(unittest.TestCase):
    KEYS = (
        ENV_MCP,
        ENV_MCP_URL,
        ENV_MCP_PREFIX,
        ENV_MCP_INCLUDE,
        ENV_MCP_EXCLUDE,
        "AGENTPRO_MCP_TOKEN",
        "AGENTPRO_MCP_TOKEN_PATH",
    )

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class EnabledFlagTests(EnvIsolationTestCase):
    def test_disabled_by_default(self):
        self.assertFalse(mcp_enabled_from_env())

    def test_truthy_values_enable(self):
        for value in ("1", "on", "true", "YES", "enabled"):
            os.environ[ENV_MCP] = value
            self.assertTrue(mcp_enabled_from_env(), value)

    def test_falsy_values_stay_off(self):
        for value in ("0", "off", "false", "", "maybe"):
            os.environ[ENV_MCP] = value
            self.assertFalse(mcp_enabled_from_env(), value)

    def test_explicit_url_enables(self):
        os.environ[ENV_MCP_URL] = "https://mcp.example/mcp"
        self.assertTrue(mcp_enabled_from_env())


class AttachTests(EnvIsolationTestCase):
    def test_no_network_when_disabled(self):
        registry = ToolRegistry()
        result = attach_mcp_from_env(registry)
        self.assertFalse(result.enabled)
        self.assertEqual(result.names, ())
        self.assertEqual(registry.names(), [])

    def test_force_registers_with_injected_client(self):
        registry = ToolRegistry()
        result = attach_mcp_from_env(registry, client=StubClient())
        self.assertTrue(result.ok)
        self.assertIn("hf_marketing_ads", result.names)
        self.assertIn("mcp_list_tools", result.names)
        self.assertTrue(registry.is_enabled("hf_marketing_ads"))

    def test_include_pattern_narrows_catalogue(self):
        registry = ToolRegistry()
        result = attach_mcp_from_env(
            registry, client=StubClient(), include=["hf_*"]
        )
        self.assertEqual(set(result.names), {"hf_hf_image", "mcp_list_tools"})

    def test_exclude_pattern_drops_tools(self):
        registry = ToolRegistry()
        result = attach_mcp_from_env(
            registry, client=StubClient(), exclude=["marketing/*"]
        )
        self.assertEqual(set(result.names), {"hf_hf_image", "mcp_list_tools"})

    def test_prefix_from_env(self):
        os.environ[ENV_MCP_PREFIX] = "gen_"
        registry = ToolRegistry()
        result = attach_mcp_from_env(registry, client=StubClient())
        self.assertIn("gen_marketing_ads", result.names)

    def test_include_from_env_comma_list(self):
        os.environ[ENV_MCP_INCLUDE] = "hf_image, marketing/ads"
        registry = ToolRegistry()
        result = attach_mcp_from_env(registry, client=StubClient())
        self.assertEqual(set(result.names), {"hf_hf_image", "hf_marketing_ads", "mcp_list_tools"})

    def test_blank_patterns_are_ignored(self):
        os.environ[ENV_MCP_INCLUDE] = " , ,"
        registry = ToolRegistry()
        result = attach_mcp_from_env(registry, client=StubClient())
        self.assertIn("hf_marketing_ads", result.names)

    def test_endpoint_error_is_reported_not_raised(self):
        from agentpro.mcp_client import MCPError

        registry = ToolRegistry()
        result = attach_mcp_from_env(
            registry, client=StubClient(error=MCPError("HTTP 401"))
        )
        self.assertTrue(result.enabled)
        self.assertFalse(result.ok)
        self.assertIn("401", result.error)
        self.assertEqual(registry.names(), [])

    def test_unauthorized_flag_marks_enabled(self):
        registry = ToolRegistry()
        result = attach_mcp_from_env(registry, force=True, url="https://x/mcp")
        self.assertTrue(result.error)  # real network attempt fails offline-safe
        self.assertEqual(result.url, "https://x/mcp")

    def test_max_tools_is_respected(self):
        registry = ToolRegistry()
        result = attach_mcp_from_env(
            registry, client=StubClient(), max_tools=1
        )
        self.assertEqual(len(result.names), 2)  # one remote + the list tool

    def test_registry_keeps_builtin_tools(self):
        registry = ToolRegistry()
        attach_mcp_from_env(registry, client=StubClient())
        self.assertIn("mcp_list_tools", registry.names())

    def test_reattaching_same_url_is_idempotent(self):
        registry = ToolRegistry()
        first = attach_mcp_from_env(
            registry, client=StubClient(), force=True
        )
        self.assertTrue(first.ok)
        names_before = registry.names()
        second = attach_mcp_from_env(registry, url="https://mcp.example/mcp", force=True)
        self.assertEqual(second.names, ())
        self.assertEqual(registry.names(), names_before)


class AgentRuntimeWiringTests(EnvIsolationTestCase):
    class FakeBridge:
        def ui_dump(self):
            return "<hierarchy/>"

        def tap(self, x, y):
            return None

        def swipe(self, x1, y1, x2, y2, duration=300):
            return None

        def type_text(self, text):
            return None

        def press_back(self):
            return None

        def open_app(self, name):
            return None

    class FakeLLM:
        def complete(self, messages, *, json_mode=False, timeout=30):
            return '{"subgoals": []}'

    def test_agent_stays_offline_by_default(self):
        from agentpro.agent_v2 import AutonomousAgent

        agent = AutonomousAgent(
            self.FakeBridge(), self.FakeLLM(), trace_path="/tmp/opencode/trace.jsonl"
        )
        self.assertFalse(agent.mcp.enabled)
        self.assertNotIn("mcp_list_tools", agent.registry.names())

    def test_agent_uses_injected_registry_untouched(self):
        from agentpro.agent_v2 import AutonomousAgent

        registry = ToolRegistry()
        agent = AutonomousAgent(
            self.FakeBridge(),
            self.FakeLLM(),
            registry=registry,
            trace_path="/tmp/opencode/trace.jsonl",
        )
        self.assertIs(agent.registry, registry)
        self.assertIsNone(agent.mcp)

    def test_agent_can_opt_out_explicitly(self):
        from agentpro.agent_v2 import AutonomousAgent

        agent = AutonomousAgent(
            self.FakeBridge(),
            self.FakeLLM(),
            use_mcp=False,
            trace_path="/tmp/opencode/trace.jsonl",
        )
        self.assertIsNone(agent.mcp)


class UserAgentTests(unittest.TestCase):
    def test_default_user_agent_is_a_product_token(self):
        from agentpro.mcp_client import DEFAULT_USER_AGENT, default_user_agent

        saved = os.environ.pop("AGENTPRO_MCP_USER_AGENT", None)
        try:
            self.assertEqual(default_user_agent(), DEFAULT_USER_AGENT)
            self.assertNotIn("Python-urllib", DEFAULT_USER_AGENT)
            os.environ["AGENTPRO_MCP_USER_AGENT"] = "Custom/9"
            self.assertEqual(default_user_agent(), "Custom/9")
        finally:
            os.environ.pop("AGENTPRO_MCP_USER_AGENT", None)
            if saved is not None:
                os.environ["AGENTPRO_MCP_USER_AGENT"] = saved

    def test_oauth_transport_sends_user_agent(self):
        from agentpro.mcp_oauth import HTTPOAuthTransport

        class Opener:
            def __init__(self):
                self.requests = []

            def __call__(self, request, timeout=None):
                self.requests.append(request)
                return type(
                    "R",
                    (),
                    {
                        "read": lambda self: b"{}",
                        "__enter__": lambda self: self,
                        "__exit__": lambda self, *a: False,
                    },
                )()

        opener = Opener()
        transport = HTTPOAuthTransport(opener=opener)
        transport.get_json("https://x", 5)
        transport.post_form("https://x", {"a": "b"}, 5)
        transport.post_json("https://x", {"a": "b"}, 5)
        for request in opener.requests:
            sent = {k.lower(): v for k, v in request.headers.items()}
            self.assertIn("user-agent", sent)
            self.assertNotIn("Python-urllib", sent["user-agent"])


class RegistrationContractTests(unittest.TestCase):
    class RecordingTransport(OAuthTransport):
        def __init__(self, grant_response):
            self.grant_response = grant_response
            self.registrations = []

        def get_json(self, url, timeout):
            return {
                "issuer": "https://as.example",
                "token_endpoint": "https://as.example/token",
                "registration_endpoint": "https://as.example/register",
                "device_authorization_endpoint": "https://as.example/device",
                "grant_types_supported": [
                    "authorization_code",
                    "refresh_token",
                    "urn:ietf:params:oauth:grant-type:device_code",
                ],
                "code_challenge_methods_supported": ["S256"],
            }

        def post_form(self, url, data, timeout):
            return {}

        def post_json(self, url, payload, timeout):
            self.registrations.append(payload)
            return self.grant_response

    def _client(self, transport):
        return MCPOAuthClient("https://mcp.example/mcp", transport=transport)

    def test_registration_always_sends_redirect_uris(self):
        transport = self.RecordingTransport(
            {"client_id": "cid", "grant_types": ["authorization_code"]}
        )
        self._client(transport).register()
        payload = transport.registrations[0]
        self.assertEqual(payload["redirect_uris"], ["http://127.0.0.1:8765/callback"])

    def test_explicit_redirect_uris_win(self):
        transport = self.RecordingTransport({"client_id": "cid"})
        self._client(transport).register(redirect_uris=["agentpro://cb"])
        self.assertEqual(transport.registrations[0]["redirect_uris"], ["agentpro://cb"])

    def test_granted_device_flow_is_recorded(self):
        transport = self.RecordingTransport(
            {
                "client_id": "cid",
                "grant_types": [
                    "urn:ietf:params:oauth:grant-type:device_code",
                    "refresh_token",
                ],
            }
        )
        registration = self._client(transport).register()
        self.assertTrue(registration.supports_device_flow)
        self.assertIn("urn:ietf:params:oauth:grant-type:device_code", registration.grant_types)

    def test_device_flow_denied_by_server_is_recorded(self):
        transport = self.RecordingTransport(
            {"client_id": "cid", "grant_types": ["authorization_code"]}
        )
        registration = self._client(transport).register()
        self.assertFalse(registration.supports_device_flow)

    def test_absent_grant_list_is_permissive(self):
        transport = self.RecordingTransport({"client_id": "cid"})
        self.assertTrue(self._client(transport).register().supports_device_flow)

    def test_registration_type_is_public(self):
        transport = self.RecordingTransport({"client_id": "cid"})
        self._client(transport).register()
        self.assertEqual(
            transport.registrations[0]["token_endpoint_auth_method"], "none"
        )
        self.assertNotIn("client_secret", transport.registrations[0])


class DiscoveryURLTests(unittest.TestCase):
    def test_wellknown_paths_are_origin_scoped(self):
        seen = []

        class Transport(OAuthTransport):
            def get_json(self, url, timeout):
                seen.append(url)
                if "oauth-protected-resource" in url:
                    return {
                        "resource": "https://mcp.example/mcp",
                        "authorization_servers": ["https://as.example"],
                    }
                return {
                    "issuer": "https://as.example",
                    "token_endpoint": "https://as.example/token",
                    "registration_endpoint": "https://as.example/register",
                    "code_challenge_methods_supported": ["S256"],
                }

            def post_form(self, url, data, timeout):
                return {}

            def post_json(self, url, payload, timeout):
                return {}

        client = MCPOAuthClient("https://mcp.example/mcp", transport=Transport())
        client.discover()
        self.assertEqual(seen[0], "https://mcp.example/.well-known/oauth-protected-resource/mcp")
        self.assertTrue(
            all("/mcp/.well-known" not in url for url in seen), seen
        )

    def test_discovery_failure_lists_known_urls(self):
        class Transport(OAuthTransport):
            def get_json(self, url, timeout):
                from agentpro.mcp_oauth import OAuthError

                raise OAuthError("404")

            def post_form(self, url, data, timeout):
                return {}

            def post_json(self, url, payload, timeout):
                return {}

        client = MCPOAuthClient("https://mcp.example/mcp", transport=Transport())
        with self.assertRaises(Exception) as ctx:
            client.discover()
        self.assertIn("protected-resource", str(ctx.exception))

    def test_metadata_prefers_device_capable_server(self):
        device = AuthorizationServerMetadata.from_payload(
            "https://a", DEVICE_AUTH_METADATA
        )
        code = AuthorizationServerMetadata.from_payload(
            "https://b", {"issuer": "https://b", "token_endpoint": "t"}
        )
        self.assertIs(MCPOAuthClient._choose_server([code, device]), device)
        self.assertIs(MCPOAuthClient._choose_server([code]), code)

    def test_registration_dataclass_defaults(self):
        registration = ClientRegistration(client_id="cid")
        self.assertEqual(registration.redirect_uris, ())
        self.assertTrue(registration.supports_device_flow)


DEVICE_AUTH_METADATA = {
    "issuer": "https://a",
    "device_authorization_endpoint": "https://a/device",
    "token_endpoint": "https://a/token",
    "grant_types_supported": ["urn:ietf:params:oauth:grant-type:device_code"],
    "code_challenge_methods_supported": ["S256"],
}


class TokenHeaderTests(unittest.TestCase):
    def test_token_serialises_for_reuse(self):
        import time

        token = TokenSet(access_token="at", expires_in=60, obtained_at=time.time())
        restored = TokenSet.from_dict(token.to_dict())
        self.assertEqual(restored.access_token, "at")
        self.assertEqual(restored.bearer_headers(), {"Authorization": "Bearer at"})

    def test_mcp_response_helper_used_by_clients(self):
        response = MCPResponse(
            status=200, headers={"Mcp-Session-Id": "s"}, body=json.dumps({"ok": 1}).encode()
        )
        self.assertEqual(response.header("mcp-session-id"), "s")
        self.assertIsNone(response.header("missing"))


if __name__ == "__main__":
    unittest.main()
