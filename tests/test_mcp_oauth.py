"""Tests for the stdlib-only OAuth 2.1 client used by MCP endpoints."""

import json
import os
import tempfile
import time
import unittest

from agentpro.mcp_oauth import (
    DEVICE_CODE_GRANT,
    AuthorizationServerMetadata,
    ClientRegistration,
    DeviceAuthorization,
    HTTPOAuthTransport,
    MCPOAuthClient,
    OAuthError,
    OAuthTransport,
    ProtectedResourceMetadata,
    TokenSet,
    bearer_header_factory,
    clear_token,
    code_challenge_s256,
    load_token,
    new_code_verifier,
    random_state,
    save_token,
)
from agentpro.mcp_client import MCPClient

DEVICE_AUTH_METADATA = {
    "issuer": "https://clerk.example",
    "authorization_endpoint": "https://clerk.example/oauth/authorize",
    "token_endpoint": "https://clerk.example/oauth/token",
    "registration_endpoint": "https://clerk.example/oauth/register",
    "device_authorization_endpoint": "https://clerk.example/oauth/device_authorization",
    "grant_types_supported": ["authorization_code", "refresh_token", DEVICE_CODE_GRANT],
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["openid", "email", "offline_access"],
}

CODE_ONLY_METADATA = {
    "issuer": "https://code.example",
    "authorization_endpoint": "https://code.example/authorize",
    "token_endpoint": "https://code.example/token",
    "registration_endpoint": "https://code.example/register",
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["openid", "email"],
}

RESOURCE_METADATA = {
    "resource": "https://mcp.example/mcp",
    "authorization_servers": ["https://code.example", "https://clerk.example"],
    "scopes_supported": ["openid", "email", "offline_access"],
    "bearer_methods_supported": ["header"],
}


class FakeTransport(OAuthTransport):
    """Serves canned JSON per URL, recording every call."""

    def __init__(self, routes):
        self.routes = {url: list(items) for url, items in routes.items()}
        self.calls = []

    def _next(self, url, method, payload):
        self.calls.append((method, url, payload))
        queue = self.routes.get(url)
        if not queue:
            raise OAuthError(f"no canned response for {url}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get_json(self, url, timeout):
        return self._next(url, "GET", None)

    def post_form(self, url, data, timeout):
        return self._next(url, "POST_FORM", dict(data))

    def post_json(self, url, payload, timeout):
        return self._next(url, "POST_JSON", dict(payload))


def routes(*pairs):
    return FakeTransport(dict(pairs))


WELL_KNOWN_RESOURCE = "https://mcp.example/.well-known/oauth-protected-resource/mcp"
CLERK_METADATA_URL = "https://clerk.example/.well-known/oauth-authorization-server"
CODE_METADATA_URL = "https://code.example/.well-known/oauth-authorization-server"


class PKCEHelperTests(unittest.TestCase):
    def test_verifier_length_is_valid(self):
        for _ in range(5):
            self.assertTrue(43 <= len(new_code_verifier()) <= 128)

    def test_verifier_rejects_out_of_range_length(self):
        with self.assertRaises(ValueError):
            new_code_verifier(10)

    def test_challenge_is_s256_of_verifier(self):
        import base64
        import hashlib

        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            )
            .decode("ascii")
            .rstrip("=")
        )
        self.assertEqual(code_challenge_s256(verifier), expected)

    def test_state_is_random_and_nonempty(self):
        self.assertNotEqual(random_state(), random_state())
        self.assertTrue(random_state())


class MetadataTests(unittest.TestCase):
    def test_authorization_server_parses_flows(self):
        meta = AuthorizationServerMetadata.from_payload(
            "https://clerk.example", DEVICE_AUTH_METADATA
        )
        self.assertTrue(meta.supports_device_flow)
        self.assertTrue(meta.supports_pkce)
        self.assertEqual(meta.scopes_supported, ("openid", "email", "offline_access"))

    def test_code_only_server_has_no_device_flow(self):
        meta = AuthorizationServerMetadata.from_payload("x", CODE_ONLY_METADATA)
        self.assertFalse(meta.supports_device_flow)
        self.assertTrue(meta.registration_endpoint)

    def test_missing_fields_do_not_raise(self):
        meta = AuthorizationServerMetadata.from_payload("https://x", {})
        self.assertEqual(meta.issuer, "https://x")
        self.assertFalse(meta.supports_device_flow)
        self.assertFalse(meta.supports_pkce)

    def test_protected_resource_bearer_header(self):
        meta = ProtectedResourceMetadata.from_payload(RESOURCE_METADATA)
        self.assertTrue(meta.supports_bearer_header)
        self.assertEqual(len(meta.authorization_servers), 2)

    def test_protected_resource_defaults_to_header_support(self):
        meta = ProtectedResourceMetadata.from_payload({"resource": "https://r"})
        self.assertTrue(meta.supports_bearer_header)


class DiscoveryTests(unittest.TestCase):
    def test_prefers_device_flow_server(self):
        transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CODE_METADATA_URL, [CODE_ONLY_METADATA]),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
        )
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        server = client.discover()
        self.assertEqual(server.issuer, "https://clerk.example")
        self.assertEqual(len(client.servers()), 2)

    def test_caches_discovered_server(self):
        transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
        )
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        client.discover()
        first_round = len(transport.calls)
        client.discover()
        self.assertEqual(len(transport.calls), first_round)

    def test_falls_back_when_suffix_wellknown_fails(self):
        transport = routes(
            (
                "https://mcp.example/.well-known/oauth-protected-resource",
                [RESOURCE_METADATA],
            ),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
        )
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        self.assertEqual(client.discover().issuer, "https://clerk.example")

    def test_raises_when_no_authorization_server_responds(self):
        transport = routes((WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]))
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        with self.assertRaises(OAuthError):
            client.discover()

    def test_resource_metadata_without_authorization_servers(self):
        transport = routes(
            (
                "https://mcp.example/.well-known/oauth-protected-resource/mcp",
                [{"resource": "https://mcp.example/mcp"}],
            ),
            (
                "https://mcp.example/.well-known/oauth-authorization-server",
                [DEVICE_AUTH_METADATA],
            ),
        )
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        self.assertEqual(client.discover().issuer, "https://clerk.example")

    def test_choose_server_prefers_device_capable_server(self):
        code = AuthorizationServerMetadata.from_payload("c", CODE_ONLY_METADATA)
        device = AuthorizationServerMetadata.from_payload(
            "d", DEVICE_AUTH_METADATA
        )
        self.assertTrue(MCPOAuthClient._choose_server([code, device]).supports_device_flow)
        self.assertFalse(MCPOAuthClient._choose_server([code]).supports_device_flow)

    def test_choose_server_falls_back_to_first_candidate(self):
        bare = AuthorizationServerMetadata.from_payload("https://x", {})
        self.assertIs(MCPOAuthClient._choose_server([bare]), bare)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
            (
                "https://clerk.example/oauth/register",
                [
                    {
                        "client_id": "cid_device",
                        "redirect_uris": [],
                    }
                ],
            ),
        )
        self.client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=self.transport
        )

    def test_registration_is_public_and_uses_device_grant(self):
        registration = self.client.register()
        self.assertIsInstance(registration, ClientRegistration)
        self.assertEqual(registration.client_id, "cid_device")
        payload = [
            call[2]
            for call in self.transport.calls
            if call[0] == "POST_JSON"
        ][0]
        self.assertEqual(payload["token_endpoint_auth_method"], "none")
        self.assertIn(DEVICE_CODE_GRANT, payload["grant_types"])
        self.assertNotIn("client_secret", payload)

    def test_registration_is_cached(self):
        self.assertEqual(self.client.register().client_id, "cid_device")
        self.assertEqual(self.client.register().client_id, "cid_device")
        posts = [call for call in self.transport.calls if call[0] == "POST_JSON"]
        self.assertEqual(len(posts), 1)

    def test_registration_without_endpoint_raises(self):
        metadata = dict(DEVICE_AUTH_METADATA)
        metadata.pop("registration_endpoint")
        transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CLERK_METADATA_URL, [metadata]),
        )
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        with self.assertRaises(OAuthError):
            client.register()

    def test_registration_without_client_id_raises(self):
        transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
            ("https://clerk.example/oauth/register", [{"error": "bad"}]),
        )
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        with self.assertRaises(OAuthError):
            client.register()


class DeviceFlowTests(unittest.TestCase):
    def setUp(self):
        self.transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
            (
                "https://clerk.example/oauth/register",
                [{"client_id": "cid"}],
            ),
            (
                "https://clerk.example/oauth/device_authorization",
                [
                    {
                        "device_code": "dev",
                        "user_code": "WXYZ-1234",
                        "verification_uri": "https://clerk.example/activate",
                        "verification_uri_complete": "https://clerk.example/activate?code=X",
                        "expires_in": 600,
                        "interval": 1,
                    }
                ],
            ),
            (
                "https://clerk.example/oauth/token",
                [
                    {"error": "authorization_pending"},
                    {
                        "access_token": "at_device",
                        "expires_in": 900,
                        "refresh_token": "rt_device",
                    },
                ],
            ),
        )
        self.client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=self.transport
        )

    def test_begin_returns_user_facing_instructions(self):
        device = self.client.begin_device_authorization()
        self.assertIsInstance(device, DeviceAuthorization)
        self.assertEqual(device.user_code, "WXYZ-1234")
        self.assertEqual(device.interval, 1)
        self.assertIn("code=X", device.verification_uri_complete)

    def test_poll_returns_token_after_pending_responses(self):
        device = self.client.begin_device_authorization()
        token = self.client.poll_device_token(
            device, sleep=lambda _s: None, clock=lambda: 0
        )
        self.assertEqual(token.access_token, "at_device")

    def test_poll_handles_slow_down(self):
        self.transport.routes["https://clerk.example/oauth/token"] = [
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"access_token": "at", "expires_in": 60},
        ]
        device = self.client.begin_device_authorization()
        sleeps = []
        token = self.client.poll_device_token(
            device, sleep=sleeps.append, clock=lambda: 0
        )
        self.assertEqual(token.access_token, "at")
        self.assertEqual(sleeps, [1, 1, 6])

    def test_poll_raises_on_denied(self):
        self.transport.routes["https://clerk.example/oauth/token"] = [
            {"error": "access_denied", "error_description": "user said no"}
        ]
        device = self.client.begin_device_authorization()
        with self.assertRaises(OAuthError) as ctx:
            self.client.poll_device_token(
                device, sleep=lambda _s: None, clock=lambda: 0
            )
        self.assertIn("access_denied", str(ctx.exception))

    def test_poll_handles_transport_error_pending(self):
        self.transport.routes["https://clerk.example/oauth/token"] = [
            OAuthError("HTTP 400: {\"error\":\"authorization_pending\"}"),
            {"access_token": "at", "expires_in": 60},
        ]
        device = self.client.begin_device_authorization()
        token = self.client.poll_device_token(
            device, sleep=lambda _s: None, clock=lambda: 0
        )
        self.assertEqual(token.access_token, "at")

    def test_poll_gives_up_after_expiry(self):
        self.transport.routes["https://clerk.example/oauth/token"] = [
            {"error": "authorization_pending"}
        ]
        device = self.client.begin_device_authorization()
        ticks = iter([0, 10, 10_000])
        with self.assertRaises(OAuthError) as ctx:
            self.client.poll_device_token(
                device, sleep=lambda _s: None, clock=lambda: next(ticks)
            )
        self.assertIn("expired", str(ctx.exception))

    def test_begin_raises_when_device_unsupported(self):
        transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CODE_METADATA_URL, [CODE_ONLY_METADATA]),
            ("https://code.example/register", [{"client_id": "cid"}]),
        )
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        with self.assertRaises(OAuthError):
            client.begin_device_authorization()

    def test_begin_raises_on_incomplete_response(self):
        self.transport.routes[
            "https://clerk.example/oauth/device_authorization"
        ] = [{"device_code": "dev"}]
        with self.assertRaises(OAuthError):
            self.client.begin_device_authorization()


class AuthorizationCodeTests(unittest.TestCase):
    def setUp(self):
        self.transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CODE_METADATA_URL, [CODE_ONLY_METADATA]),
            ("https://code.example/register", [{"client_id": "cid"}]),
        )
        self.client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=self.transport
        )

    def test_authorize_url_carries_pkce_and_state(self):
        url, state, verifier = self.client.begin_authorization(
            "http://127.0.0.1:8765/callback"
        )
        self.assertTrue(url.startswith("https://code.example/authorize?"))
        self.assertIn(f"client_id=cid", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn(f"state={state}", url)
        self.assertIn(f"code_challenge={code_challenge_s256(verifier)}", url)
        self.assertIn("redirect_uri=", url)
        self.assertNotIn("client_secret", url)

    def test_exchange_code_returns_token(self):
        self.transport.routes["https://code.example/token"] = [
            {"access_token": "at_code", "expires_in": 900, "refresh_token": "rt"}
        ]
        token = self.client.exchange_code("code1", "verifier", "http://127.0.0.1:8765/callback")
        self.assertEqual(token.access_token, "at_code")
        self.assertEqual(token.refresh_token, "rt")
        payload = [
            call[2] for call in self.transport.calls if call[0] == "POST_FORM"
        ][0]
        self.assertEqual(payload["grant_type"], "authorization_code")
        self.assertEqual(payload["code_verifier"], "verifier")

    def test_exchange_reports_error_payload(self):
        self.transport.routes["https://code.example/token"] = [
            {"error": "invalid_grant", "error_description": "expired"}
        ]
        with self.assertRaises(OAuthError) as ctx:
            self.client.exchange_code("c", "v", "http://127.0.0.1:8765/callback")
        self.assertIn("invalid_grant", str(ctx.exception))

    def test_begin_raises_without_authorization_endpoint(self):
        metadata = dict(CODE_ONLY_METADATA)
        metadata.pop("authorization_endpoint")
        transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CODE_METADATA_URL, [metadata]),
        )
        client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=transport
        )
        with self.assertRaises(OAuthError):
            client.begin_authorization("http://127.0.0.1:1/cb")


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
            ("https://clerk.example/oauth/register", [{"client_id": "cid"}]),
        )
        self.client = MCPOAuthClient(
            "https://mcp.example/mcp", transport=self.transport
        )

    def test_refresh_keeps_old_refresh_token_when_not_rotated(self):
        self.transport.routes["https://clerk.example/oauth/token"] = [
            {"access_token": "at_new", "expires_in": 60}
        ]
        old = TokenSet(
            access_token="at_old", refresh_token="rt_old", expires_in=60,
            obtained_at=time.time() - 3600,
        )
        fresh = self.client.refresh(old)
        self.assertEqual(fresh.access_token, "at_new")
        self.assertEqual(fresh.refresh_token, "rt_old")

    def test_refresh_requires_refresh_token(self):
        with self.assertRaises(OAuthError):
            self.client.refresh(TokenSet(access_token="a"))

    def test_refresh_reports_error(self):
        self.transport.routes["https://clerk.example/oauth/token"] = [
            {"error": "invalid_grant"}
        ]
        with self.assertRaises(OAuthError):
            self.client.refresh(
                TokenSet(access_token="a", refresh_token="r")
            )

    def test_ensure_valid_refreshes_only_when_expired(self):
        self.transport.routes["https://clerk.example/oauth/token"] = [
            {"access_token": "at_new", "expires_in": 900}
        ]
        fresh_token = TokenSet(
            access_token="at", refresh_token="r", expires_in=900,
            obtained_at=time.time(),
        )
        self.assertIs(self.client.ensure_valid(fresh_token), fresh_token)
        stale = TokenSet(
            access_token="a", refresh_token="r", expires_in=60,
            obtained_at=time.time() - 3600,
        )
        self.assertEqual(self.client.ensure_valid(stale).access_token, "at_new")


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "token.json")
        self._env = dict(os.environ)
        os.environ.pop("AGENTPRO_MCP_TOKEN", None)
        os.environ["AGENTPRO_MCP_TOKEN_PATH"] = self.path

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_roundtrip(self):
        token = TokenSet(
            access_token="at", refresh_token="rt", expires_in=60,
            scope="openid", obtained_at=time.time(),
        )
        save_token(token)
        loaded = load_token()
        self.assertEqual(loaded.access_token, "at")
        self.assertEqual(loaded.refresh_token, "rt")

    def test_file_is_owner_only(self):
        save_token(TokenSet(access_token="at"))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_env_token_wins(self):
        save_token(TokenSet(access_token="from_file"))
        os.environ["AGENTPRO_MCP_TOKEN"] = "  from_env  "
        self.assertEqual(load_token().access_token, "from_env")

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_token(os.path.join(self.dir, "nope.json")))

    def test_corrupt_file_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(load_token())

    def test_missing_access_token_in_dict_raises(self):
        with self.assertRaises(KeyError):
            TokenSet.from_dict({"token_type": "Bearer"})

    def test_clear_token(self):
        save_token(TokenSet(access_token="at"))
        self.assertTrue(clear_token())
        self.assertFalse(clear_token())

    def test_expired_uses_skew(self):
        token = TokenSet(
            access_token="a", expires_in=100, obtained_at=time.time()
        )
        self.assertFalse(token.expired())
        self.assertTrue(token.expired(skew=200))

    def test_token_without_expiry_never_expires(self):
        self.assertFalse(TokenSet(access_token="a").expired())

    def test_bearer_headers(self):
        headers = TokenSet(access_token="abc", token_type="Bearer").bearer_headers()
        self.assertEqual(headers, {"Authorization": "Bearer abc"})

    def test_from_payload_requires_access_token(self):
        with self.assertRaises(OAuthError):
            TokenSet.from_payload({"token_type": "Bearer"})


class BearerHeaderFactoryTests(TokenStoreTests):
    def setUp(self):
        super().setUp()
        self.refreshed = []

    def test_factory_returns_none_without_token(self):
        self.assertIsNone(bearer_header_factory("https://mcp.example/mcp"))

    def test_factory_emits_bearer_header(self):
        save_token(TokenSet(access_token="at_static", expires_in=900, obtained_at=time.time()))
        factory = bearer_header_factory("https://mcp.example/mcp")
        self.assertEqual(factory(), {"Authorization": "Bearer at_static"})

    def test_factory_refreshes_expired_token_and_persists(self):
        save_token(
            TokenSet(
                access_token="at_old", refresh_token="rt", expires_in=60,
                obtained_at=time.time() - 3600,
            )
        )
        transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
            ("https://clerk.example/oauth/register", [{"client_id": "cid"}]),
            (
                "https://clerk.example/oauth/token",
                [{"access_token": "at_fresh", "expires_in": 900}],
            ),
        )
        factory = bearer_header_factory(
            "https://mcp.example/mcp", transport=transport, on_refresh=self.refreshed.append
        )
        self.assertEqual(factory()["Authorization"], "Bearer at_fresh")
        self.assertEqual(load_token().access_token, "at_fresh")
        self.assertEqual(len(self.refreshed), 1)

    def test_factory_survives_refresh_failure(self):
        save_token(
            TokenSet(
                access_token="at_old", refresh_token="rt", expires_in=60,
                obtained_at=time.time() - 3600,
            )
        )
        transport = routes(
            (WELL_KNOWN_RESOURCE, [RESOURCE_METADATA]),
            (CLERK_METADATA_URL, [DEVICE_AUTH_METADATA]),
            ("https://clerk.example/oauth/register", [{"client_id": "cid"}]),
            ("https://clerk.example/oauth/token", [{"error": "invalid_grant"}]),
        )
        factory = bearer_header_factory(
            "https://mcp.example/mcp", transport=transport
        )
        self.assertEqual(factory()["Authorization"], "Bearer at_old")


class MCPClientTokenIntegrationTests(TokenStoreTests):
    class RecordingTransport:
        """Minimal MCPTransport returning canned initialize responses."""

        def __init__(self):
            self.headers = []

        def post(self, url, body, headers, timeout):
            from agentpro.mcp_client import MCPResponse

            self.headers.append(dict(headers))
            if b"notifications/initialized" in body:
                return MCPResponse(status=202, headers={}, body=b"")
            return MCPResponse(
                status=200,
                headers={"Mcp-Session-Id": "sess"},
                body=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake", "version": "1"},
                        },
                    }
                ).encode(),
            )

    def test_client_sends_bearer_header_from_factory(self):
        save_token(
            TokenSet(access_token="at_mcp", expires_in=900, obtained_at=time.time())
        )
        transport = self.RecordingTransport()
        client = MCPClient(
            "https://mcp.example/mcp",
            transport=transport,
            extra_headers=bearer_header_factory("https://mcp.example/mcp"),
        )
        client.initialize()
        self.assertTrue(client.initialized)
        self.assertEqual(client.session_id, "sess")
        self.assertEqual(len(transport.headers), 2)
        self.assertTrue(
            all(h.get("Authorization") == "Bearer at_mcp" for h in transport.headers)
        )

    def test_constructor_initialize_flag_is_honored(self):
        transport = self.RecordingTransport()
        client = MCPClient(
            "https://mcp.example/mcp", transport=transport, initialize=True
        )
        self.assertTrue(client.initialized)
        self.assertEqual(len(transport.headers), 2)

    def test_constructor_defaults_to_lazy_initialization(self):
        transport = self.RecordingTransport()
        client = MCPClient("https://mcp.example/mcp", transport=transport)
        self.assertFalse(client.initialized)
        self.assertEqual(transport.headers, [])


class HTTPOAuthTransportTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, body, status=200):
            self._body = body
            self.status = status

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class FakeOpener:
        def __init__(self, response=None, error=None):
            self.response = response
            self.error = error
            self.requests = []

        def __call__(self, request, timeout=None):
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            return self.response

    def test_get_json_parses_body(self):
        opener = self.FakeOpener(
            self.FakeResponse(json.dumps({"issuer": "https://x"}).encode())
        )
        transport = HTTPOAuthTransport(opener=opener)
        self.assertEqual(transport.get_json("https://x", 5)["issuer"], "https://x")

    def test_get_json_on_empty_body_returns_empty_dict(self):
        transport = HTTPOAuthTransport(opener=self.FakeOpener(self.FakeResponse(b"")))
        self.assertEqual(transport.get_json("https://x", 5), {})

    def test_post_form_sends_urlencoded_body(self):
        opener = self.FakeOpener(self.FakeResponse(b'{"ok":true}'))
        transport = HTTPOAuthTransport(opener=opener)
        transport.post_form("https://x", {"a": "b c"}, 5)
        request = opener.requests[0]
        self.assertEqual(request.data, b"a=b+c")
        self.assertEqual(
            request.headers.get("Content-type"),
            "application/x-www-form-urlencoded",
        )

    def test_post_json_sends_json_body(self):
        opener = self.FakeOpener(self.FakeResponse(b'{"ok":true}'))
        transport = HTTPOAuthTransport(opener=opener)
        transport.post_json("https://x", {"a": 1}, 5)
        self.assertEqual(opener.requests[0].data, b'{"a": 1}')

    def test_http_error_is_wrapped(self):
        import urllib.error

        error = urllib.error.HTTPError(
            "https://x", 401, "Unauthorized", {}, None
        )
        error.read = lambda: b'{"error":"Unauthorized"}'
        transport = HTTPOAuthTransport(opener=self.FakeOpener(error=error))
        with self.assertRaises(OAuthError) as ctx:
            transport.get_json("https://x", 5)
        self.assertIn("401", str(ctx.exception))

    def test_url_error_is_wrapped(self):
        import urllib.error

        error = urllib.error.URLError("no route")
        transport = HTTPOAuthTransport(opener=self.FakeOpener(error=error))
        with self.assertRaises(OAuthError):
            transport.get_json("https://x", 5)

    def test_non_json_body_raises(self):
        transport = HTTPOAuthTransport(
            opener=self.FakeOpener(self.FakeResponse(b"<html>"))
        )
        with self.assertRaises(OAuthError):
            transport.get_json("https://x", 5)


class LoopbackCallbackTests(unittest.TestCase):
    def _serve(self, query, port=0, timeout=6.0):
        import threading
        import urllib.request

        from agentpro.mcp_oauth import _CallbackHandler, capture_loopback_code

        _CallbackHandler.captured = {}
        result = {}

        def run():
            try:
                result["code"] = capture_loopback_code(
                    "state_ok", port=port, timeout=timeout
                )
            except Exception as exc:  # pragma: no cover - surfaced below
                result["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.3)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port or _last_port(result)}{query}", timeout=3
            ).read()
        except Exception:
            pass
        thread.join(timeout=timeout)
        return result

    def test_captures_code(self):
        from agentpro.mcp_oauth import _CallbackHandler
        from http.server import HTTPServer

        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        port = server.server_address[1]
        server.server_close()
        result = self._serve("?code=abc&state=state_ok", port=port)
        self.assertEqual(result.get("code"), "abc")

    def test_state_mismatch_raises(self):
        from agentpro.mcp_oauth import _CallbackHandler
        from http.server import HTTPServer

        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        port = server.server_address[1]
        server.server_close()
        result = self._serve("?code=abc&state=wrong", port=port)
        self.assertIsInstance(result.get("error"), OAuthError)

    def test_error_query_raises(self):
        from agentpro.mcp_oauth import _CallbackHandler
        from http.server import HTTPServer

        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        port = server.server_address[1]
        server.server_close()
        result = self._serve("?error=access_denied&state=state_ok", port=port)
        self.assertIsInstance(result.get("error"), OAuthError)


def _last_port(_result):
    return 1


if __name__ == "__main__":
    unittest.main()
