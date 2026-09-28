"""OAuth 2.1 support for MCP servers, standard library only.

Many MCP endpoints (Higgsfield among them) are protected resources: they answer
``401`` with a ``WWW-Authenticate`` header pointing at RFC 9728 metadata, and an
OAuth 2.1 authorization server sits behind them. This module implements the
parts a phone-side agent actually needs:

* RFC 9728 protected-resource discovery and RFC 8414 authorization-server
  discovery, with a preference for the server that supports the flows we want
* RFC 7591 dynamic client registration (public client, no secret)
* the **device authorization grant** — ideal here, because the user approves in
  the browser and types a short code, with no redirect URI and no local web
  server to keep alive
* the authorization-code grant with PKCE (S256) plus a loopback listener, for
  servers without a device endpoint
* refresh-token renewal and a 0600 token file so the agent logs in once

    python3 -m agentpro.mcp_oauth login          # device flow, prints a URL + code
    python3 -m agentpro.mcp_oauth whoami         # shows the stored token's state

Tokens are read from ``AGENTPRO_MCP_TOKEN`` or the token file, so
:func:`agentpro.mcp_tools.mcp_client_from_env` can authenticate automatically.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Mapping, Sequence

DEFAULT_SCOPES = ("openid", "email", "offline_access")
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_TOKEN_PATH = os.path.join(
    os.path.expanduser("~"), ".agentpro", "mcp_token.json"
)
ENV_TOKEN = "AGENTPRO_MCP_TOKEN"
ENV_TOKEN_PATH = "AGENTPRO_MCP_TOKEN_PATH"

_SKIP = 60


class OAuthError(RuntimeError):
    """Raised for discovery, registration, or token exchange failures."""


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------


class OAuthTransport:
    """GET JSON, POST form, and POST JSON over HTTP."""

    def get_json(self, url: str, timeout: float) -> dict[str, Any]:
        raise NotImplementedError

    def post_form(
        self, url: str, data: Mapping[str, str], timeout: float
    ) -> dict[str, Any]:
        raise NotImplementedError

    def post_json(
        self, url: str, payload: Mapping[str, Any], timeout: float
    ) -> dict[str, Any]:
        raise NotImplementedError


class HTTPOAuthTransport(OAuthTransport):
    """``urllib``-backed transport, mirroring :mod:`agentpro.mcp_client`.

    A real ``User-Agent`` is required: the default ``Python-urllib`` signature is
    rejected by Cloudflare with error 1010 on the registration endpoint.
    """

    def __init__(self, opener: Any = None, user_agent: str | None = None) -> None:
        self._opener = opener or urllib.request.urlopen
        from .mcp_client import default_user_agent

        self.user_agent = user_agent or default_user_agent()

    def _call(
        self,
        request: urllib.request.Request,
        timeout: float,
    ) -> dict[str, Any]:
        try:
            with self._opener(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = b""
            try:
                detail = exc.read()
            except Exception:  # pragma: no cover - defensive
                pass
            raise OAuthError(
                f"HTTP {exc.code} from {request.full_url}: "
                f"{detail.decode('utf-8', errors='replace')[:300]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise OAuthError(f"request to {request.full_url} failed: {exc}") from exc
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise OAuthError(
                f"response from {request.full_url} was not JSON"
            ) from exc

    def get_json(self, url: str, timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        return self._call(request, timeout)

    def post_form(
        self, url: str, data: Mapping[str, str], timeout: float
    ) -> dict[str, Any]:
        body = urllib.parse.urlencode(dict(data)).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        return self._call(request, timeout)

    def post_json(
        self, url: str, payload: Mapping[str, Any], timeout: float
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        return self._call(request, timeout)


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationServerMetadata:
    issuer: str
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    registration_endpoint: str = ""
    device_authorization_endpoint: str = ""
    grant_types_supported: tuple[str, ...] = ()
    code_challenge_methods_supported: tuple[str, ...] = ()
    scopes_supported: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, issuer: str, payload: Mapping[str, Any]) -> "AuthorizationServerMetadata":
        def as_tuple(key: str) -> tuple[str, ...]:
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                return tuple(str(item) for item in value)
            return ()

        return cls(
            issuer=str(payload.get("issuer") or issuer),
            authorization_endpoint=str(payload.get("authorization_endpoint") or ""),
            token_endpoint=str(payload.get("token_endpoint") or ""),
            registration_endpoint=str(payload.get("registration_endpoint") or ""),
            device_authorization_endpoint=str(
                payload.get("device_authorization_endpoint") or ""
            ),
            grant_types_supported=as_tuple("grant_types_supported"),
            code_challenge_methods_supported=as_tuple(
                "code_challenge_methods_supported"
            ),
            scopes_supported=as_tuple("scopes_supported"),
            raw=dict(payload),
        )

    @property
    def supports_device_flow(self) -> bool:
        return bool(
            self.device_authorization_endpoint
            and DEVICE_CODE_GRANT in self.grant_types_supported
        )

    @property
    def supports_pkce(self) -> bool:
        return "S256" in self.code_challenge_methods_supported


@dataclass(frozen=True)
class ProtectedResourceMetadata:
    resource: str
    authorization_servers: tuple[str, ...] = ()
    scopes_supported: tuple[str, ...] = ()
    bearer_methods_supported: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProtectedResourceMetadata":
        def as_tuple(key: str) -> tuple[str, ...]:
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                return tuple(str(item) for item in value)
            return ()

        return cls(
            resource=str(payload.get("resource") or ""),
            authorization_servers=as_tuple("authorization_servers"),
            scopes_supported=as_tuple("scopes_supported"),
            bearer_methods_supported=as_tuple("bearer_methods_supported"),
            raw=dict(payload),
        )

    @property
    def supports_bearer_header(self) -> bool:
        return not self.bearer_methods_supported or "header" in self.bearer_methods_supported


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    token_type: str = "Bearer"
    refresh_token: str | None = None
    expires_in: int | None = None
    scope: str = ""
    id_token: str | None = None
    obtained_at: float = 0.0
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TokenSet":
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise OAuthError("token response has no access_token")
        expires = payload.get("expires_in")
        return cls(
            access_token=access,
            token_type=str(payload.get("token_type") or "Bearer"),
            refresh_token=(
                str(payload["refresh_token"])
                if payload.get("refresh_token")
                else None
            ),
            expires_in=int(expires) if isinstance(expires, (int, float)) else None,
            scope=str(payload.get("scope") or ""),
            id_token=str(payload["id_token"]) if payload.get("id_token") else None,
            obtained_at=time.time(),
            raw=dict(payload),
        )

    def expired(self, skew: int = _SKIP) -> bool:
        if self.expires_in is None:
            return False
        return (time.time() + skew) > (self.obtained_at + self.expires_in)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "refresh_token": self.refresh_token,
            "expires_in": self.expires_in,
            "scope": self.scope,
            "id_token": self.id_token,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TokenSet":
        return cls(
            access_token=str(payload["access_token"]),
            token_type=str(payload.get("token_type") or "Bearer"),
            refresh_token=(
                str(payload["refresh_token"])
                if payload.get("refresh_token")
                else None
            ),
            expires_in=(
                int(payload["expires_in"])
                if isinstance(payload.get("expires_in"), (int, float))
                else None
            ),
            scope=str(payload.get("scope") or ""),
            id_token=(
                str(payload["id_token"]) if payload.get("id_token") else None
            ),
            obtained_at=float(payload.get("obtained_at") or 0.0),
        )

    def bearer_headers(self) -> dict[str, str]:
        return {"Authorization": f"{self.token_type} {self.access_token}"}


def token_file_path() -> str:
    return os.environ.get(ENV_TOKEN_PATH) or DEFAULT_TOKEN_PATH


def save_token(token: TokenSet, path: str | None = None) -> str:
    target = path or token_file_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(token.to_dict(), handle, indent=2)
    os.chmod(target, 0o600)
    return target


def load_token(path: str | None = None) -> TokenSet | None:
    raw = (os.environ.get(ENV_TOKEN) or "").strip()
    if raw:
        return TokenSet(access_token=raw)
    target = path or token_file_path()
    if not os.path.exists(target):
        return None
    try:
        with open(target, encoding="utf-8") as handle:
            return TokenSet.from_dict(json.load(handle))
    except (OSError, ValueError, KeyError):
        return None


def clear_token(path: str | None = None) -> bool:
    target = path or token_file_path()
    if os.path.exists(target):
        os.remove(target)
        return True
    return False


def bearer_header_factory(
    resource_url: str,
    *,
    path: str | None = None,
    transport: OAuthTransport | None = None,
    timeout: float = 30.0,
    on_refresh: Any = None,
) -> Any:
    """Return a zero-argument callable producing ``Authorization`` headers.

    The token is read once from the environment or the token file, refreshed in
    place when it has expired, and re-saved so the next process starts valid.
    Returns ``None`` when no token is stored, which keeps unauthenticated
    endpoints working exactly as before.
    """
    token = load_token(path)
    if token is None:
        return None

    state: dict[str, TokenSet] = {"token": token}
    client_box: dict[str, MCPOAuthClient | None] = {"client": None}

    def headers() -> dict[str, str]:
        current = state["token"]
        if current.expired() and current.refresh_token:
            if client_box["client"] is None:
                client_box["client"] = MCPOAuthClient(
                    resource_url, transport=transport, timeout=timeout
                )
            try:
                current = client_box["client"].ensure_valid(current)
            except OAuthError:
                current = state["token"]
            else:
                state["token"] = current
                save_token(current, path)
                if callable(on_refresh):
                    on_refresh(current)
        return current.bearer_headers()

    return headers


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def new_code_verifier(length: int = 64) -> str:
    if not 43 <= length <= 128:
        raise ValueError("code verifier length must be between 43 and 128")
    return secrets.token_urlsafe(length)[:length]


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def random_state() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# loopback callback (only used by the authorization-code flow)
# ---------------------------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).captured = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;text-align:center'>"
            b"<h2>AgentPro authorized</h2>"
            b"<p>You can close this tab and return to Termux.</p>"
            b"</body></html>"
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def capture_loopback_code(
    expected_state: str,
    *,
    port: int = 0,
    timeout: float = 300.0,
) -> str:
    """Serve one loopback request and return the authorization code."""
    _CallbackHandler.captured = {}
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 5.0
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            server.handle_request()
            if _CallbackHandler.captured:
                break
    finally:
        server.server_close()
    captured = dict(_CallbackHandler.captured)
    if not captured:
        raise OAuthError("no authorization code arrived on the loopback listener")
    if captured.get("error"):
        raise OAuthError(f"authorization failed: {captured['error']}")
    if captured.get("state") != expected_state:
        raise OAuthError("authorization state mismatch")
    code = captured.get("code")
    if not code:
        raise OAuthError("authorization response had no code")
    return code


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientRegistration:
    client_id: str
    client_secret: str | None = None
    redirect_uris: tuple[str, ...] = ()
    grant_types: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def supports_device_flow(self) -> bool:
        return not self.grant_types or DEVICE_CODE_GRANT in self.grant_types


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str = ""
    expires_in: int = 900
    interval: int = 5


class MCPOAuthClient:
    """Discovery + registration + device/code flows for one MCP resource."""

    def __init__(
        self,
        resource_url: str,
        *,
        transport: OAuthTransport | None = None,
        timeout: float = 30.0,
        client_name: str = "AndroidAgentPro",
        scopes: Sequence[str] | None = None,
    ) -> None:
        self.resource_url = resource_url
        self.timeout = float(timeout)
        self.client_name = client_name
        self.scopes = tuple(scopes) if scopes else DEFAULT_SCOPES
        self.transport = transport or HTTPOAuthTransport()
        self._resource: ProtectedResourceMetadata | None = None
        self._servers: tuple[AuthorizationServerMetadata, ...] = ()
        self._server: AuthorizationServerMetadata | None = None
        self._registration: ClientRegistration | None = None

    # -- discovery --------------------------------------------------------

    def discover(self) -> AuthorizationServerMetadata:
        """Resolve the authorization server to use, preferring device flow."""
        if self._server is not None:
            return self._server

        resource = self._resource
        if resource is None:
            parsed = urllib.parse.urlparse(self.resource_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            suffix = parsed.path or "/mcp"
            by_path = f"{origin}/.well-known/oauth-protected-resource{suffix}"
            by_origin = f"{origin}/.well-known/oauth-protected-resource"
            payload: dict[str, Any] | None = None
            for candidate_url in (by_path, by_origin):
                try:
                    payload = self.transport.get_json(candidate_url, self.timeout)
                    break
                except OAuthError:
                    continue
            if payload is None:
                raise OAuthError(
                    f"no OAuth protected-resource metadata for {self.resource_url}"
                )
            resource = ProtectedResourceMetadata.from_payload(payload)
            if not resource.authorization_servers:
                try:
                    resource = ProtectedResourceMetadata.from_payload(
                        self.transport.get_json(by_origin, self.timeout)
                    )
                except OAuthError:
                    pass
            self._resource = resource

        candidates: list[AuthorizationServerMetadata] = []
        parsed = urllib.parse.urlparse(self.resource_url)
        default_issuer = f"{parsed.scheme}://{parsed.netloc}"
        issuers = list(resource.authorization_servers) or [default_issuer]
        for issuer in issuers:
            metadata_url = (
                f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server"
            )
            try:
                payload = self.transport.get_json(metadata_url, self.timeout)
            except OAuthError:
                continue
            candidates.append(
                AuthorizationServerMetadata.from_payload(issuer, payload)
            )
        if not candidates:
            raise OAuthError(
                f"no usable authorization server for {self.resource_url}"
            )
        self._servers = tuple(candidates)
        self._server = self._choose_server(candidates)
        return self._server

    @staticmethod
    def _choose_server(
        candidates: Sequence[AuthorizationServerMetadata],
    ) -> AuthorizationServerMetadata:
        for candidate in candidates:
            if candidate.supports_device_flow:
                return candidate
        for candidate in candidates:
            if candidate.token_endpoint and candidate.registration_endpoint:
                return candidate
        return candidates[0]

    def servers(self) -> tuple[AuthorizationServerMetadata, ...]:
        if not self._servers:
            self.discover()
        return self._servers

    # -- registration -----------------------------------------------------

    def register(
        self, *, redirect_uris: Sequence[str] | None = None
    ) -> ClientRegistration:
        """Dynamic client registration; public client, no secret.

        ``redirect_uris`` always ends up non-empty: Clerk rejects a device-only
        registration, and the loopback URI is what makes the browser flow work
        on the phone itself.
        """
        if self._registration is not None:
            return self._registration
        server = self.discover()
        if not server.registration_endpoint:
            raise OAuthError(
                f"{server.issuer} has no dynamic registration endpoint"
            )
        grants = ["refresh_token"]
        uris: list[str] = list(redirect_uris) if redirect_uris else [DEFAULT_REDIRECT_URI]
        if server.supports_device_flow:
            grants.insert(0, DEVICE_CODE_GRANT)
        else:
            grants.insert(0, "authorization_code")
        payload: dict[str, Any] = {
            "client_name": self.client_name,
            "grant_types": grants,
            "response_types": ["code"] if "authorization_code" in grants else [],
            "token_endpoint_auth_method": "none",
        }
        if uris:
            payload["redirect_uris"] = uris
        scope = server.scopes_supported or self.scopes
        if scope:
            payload["scope"] = " ".join(scope)
        response = self.transport.post_json(
            server.registration_endpoint, payload, self.timeout
        )
        client_id = response.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise OAuthError("registration response has no client_id")
        self._registration = ClientRegistration(
            client_id=client_id,
            client_secret=(
                str(response["client_secret"])
                if response.get("client_secret")
                else None
            ),
            redirect_uris=tuple(
                str(u) for u in (response.get("redirect_uris") or uris)
            ),
            grant_types=tuple(
                str(g) for g in (response.get("grant_types") or ())
            ),
            raw=dict(response),
        )
        return self._registration

    def _client_id(self) -> str:
        return self.register().client_id

    # -- device flow ------------------------------------------------------

    def begin_device_authorization(self) -> DeviceAuthorization:
        server = self.discover()
        if not server.supports_device_flow:
            raise OAuthError(
                f"{server.issuer} does not advertise the device authorization grant"
            )
        scope = server.scopes_supported or self.scopes
        response = self.transport.post_form(
            server.device_authorization_endpoint,
            {
                "client_id": self._client_id(),
                "scope": " ".join(scope),
            },
            self.timeout,
        )
        device_code = response.get("device_code")
        user_code = response.get("user_code")
        verification_uri = response.get("verification_uri") or response.get(
            "verification_url"
        )
        if not device_code or not user_code or not verification_uri:
            raise OAuthError(
                "device authorization response is incomplete: "
                f"{sorted(response)}"
            )
        return DeviceAuthorization(
            device_code=str(device_code),
            user_code=str(user_code),
            verification_uri=str(verification_uri),
            verification_uri_complete=str(
                response.get("verification_uri_complete") or ""
            ),
            expires_in=int(response.get("expires_in") or 900),
            interval=int(response.get("interval") or 5),
        )

    def poll_device_token(
        self,
        device: DeviceAuthorization,
        *,
        sleep=time.sleep,
        clock=time.time,
    ) -> TokenSet:
        server = self.discover()
        interval = max(int(device.interval), 1)
        deadline = clock() + max(int(device.expires_in), 60)
        while clock() < deadline:
            sleep(interval)
            try:
                response = self.transport.post_form(
                    server.token_endpoint,
                    {
                        "grant_type": DEVICE_CODE_GRANT,
                        "device_code": device.device_code,
                        "client_id": self._client_id(),
                    },
                    self.timeout,
                )
            except OAuthError as exc:
                message = str(exc)
                if "authorization_pending" in message:
                    continue
                if "slow_down" in message:
                    interval += 5
                    continue
                raise
            if "error" in response:
                error = str(response.get("error"))
                if error in ("authorization_pending", "slow_down"):
                    if error == "slow_down":
                        interval += 5
                    continue
                raise OAuthError(
                    f"device flow failed: {error} "
                    f"{response.get('error_description', '')}".strip()
                )
            return TokenSet.from_payload(response)
        raise OAuthError("device authorization expired before approval")

    # -- authorization-code + PKCE ---------------------------------------

    def begin_authorization(self, redirect_uri: str) -> tuple[str, str, str]:
        """Return ``(authorization_url, state, code_verifier)``."""
        server = self.discover()
        if not server.authorization_endpoint:
            raise OAuthError(f"{server.issuer} has no authorization endpoint")
        state = random_state()
        verifier = new_code_verifier()
        query = {
            "response_type": "code",
            "client_id": self._client_id(),
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge_s256(verifier),
            "code_challenge_method": "S256",
        }
        scope = server.scopes_supported or self.scopes
        if scope:
            query["scope"] = " ".join(scope)
        separator = "&" if "?" in server.authorization_endpoint else "?"
        return f"{server.authorization_endpoint}{separator}{urllib.parse.urlencode(query)}", state, verifier

    def exchange_code(
        self,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> TokenSet:
        server = self.discover()
        response = self.transport.post_form(
            server.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._client_id(),
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            self.timeout,
        )
        if "error" in response:
            raise OAuthError(
                f"token exchange failed: {response.get('error')} "
                f"{response.get('error_description', '')}".strip()
            )
        return TokenSet.from_payload(response)

    # -- refresh ----------------------------------------------------------

    def refresh(self, token: TokenSet) -> TokenSet:
        if not token.refresh_token:
            raise OAuthError("token has no refresh_token")
        server = self.discover()
        response = self.transport.post_form(
            server.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                "client_id": self._client_id(),
            },
            self.timeout,
        )
        if "error" in response:
            raise OAuthError(f"refresh failed: {response.get('error')}")
        fresh = TokenSet.from_payload(response)
        if not fresh.refresh_token:
            fresh = TokenSet(
                access_token=fresh.access_token,
                token_type=fresh.token_type,
                refresh_token=token.refresh_token,
                expires_in=fresh.expires_in,
                scope=fresh.scope,
                obtained_at=fresh.obtained_at,
            )
        return fresh

    def ensure_valid(self, token: TokenSet) -> TokenSet:
        return self.refresh(token) if token.expired() else token


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_resource() -> str:
    from .mcp_tools import DEFAULT_MCP_URL

    return os.environ.get("AGENTPRO_MCP_URL") or DEFAULT_MCP_URL


def _login(resource_url: str) -> int:
    import sys

    client = MCPOAuthClient(resource_url)
    try:
        server = client.discover()
    except OAuthError as exc:
        print(f"OAuth discovery failed: {exc}", file=sys.stderr)
        return 2
    print(f"resource        : {resource_url}")
    print(f"authorization   : {server.issuer}")
    if not server.token_endpoint:
        print("this server has no token endpoint", file=sys.stderr)
        return 2
    if server.supports_device_flow and client.register().supports_device_flow:
        try:
            device = client.begin_device_authorization()
        except OAuthError as exc:
            print(f"device flow unavailable: {exc}", file=sys.stderr)
        else:
            print()
            print("  1) open this URL on the phone:")
            print(
                f"     {device.verification_uri_complete or device.verification_uri}"
            )
            print(f"  2) enter this code: {device.user_code}")
            print()
            print("waiting for approval...")
            token = client.poll_device_token(device)
            path = save_token(token)
            print(f"saved token to {path} (expires in {token.expires_in}s)")
            return 0
    return _login_browser(client)


def _login_browser(client: MCPOAuthClient) -> int:
    import sys

    redirect_uri = DEFAULT_REDIRECT_URI
    try:
        url, state, verifier = client.begin_authorization(redirect_uri)
    except OAuthError as exc:
        print(f"could not start the browser flow: {exc}", file=sys.stderr)
        return 2
    print()
    print("  1) open this URL on the phone:")
    print(f"     {url}")
    print(f"  2) log in and approve; the reply lands on {redirect_uri}")
    print()
    print("waiting for the browser...")
    try:
        code = capture_loopback_code(state, port=_loopback_port(redirect_uri))
    except OAuthError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    try:
        token = client.exchange_code(code, verifier, redirect_uri)
    except OAuthError as exc:
        print(f"token exchange failed: {exc}", file=sys.stderr)
        return 1
    path = save_token(token)
    print(f"saved token to {path} (expires in {token.expires_in}s)")
    return 0


def _loopback_port(redirect_uri: str) -> int:
    parsed = urllib.parse.urlparse(redirect_uri)
    try:
        return int(parsed.port or 8765)
    except ValueError:
        return 8765


def _whoami(resource_url: str) -> int:
    token = load_token()
    if token is None:
        print(f"no token stored at {token_file_path()}")
        return 1
    state = "expired" if token.expired() else "valid"
    print(f"token file : {token_file_path()}")
    print(f"state      : {state}")
    print(f"type       : {token.token_type}")
    print(f"scope      : {token.scope or '(unknown)'}")
    print(f"expires_in : {token.expires_in}")
    print(f"refresh    : {'yes' if token.refresh_token else 'no'}")
    if token.expired() and token.refresh_token:
        client = MCPOAuthClient(resource_url)
        try:
            fresh = client.ensure_valid(token)
        except OAuthError as exc:
            print(f"refresh failed: {exc}")
            return 1
        save_token(fresh)
        print("refreshed successfully")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "whoami"
    resource = args[1] if len(args) > 1 else _default_resource()
    if command == "login":
        return _login(resource)
    if command == "whoami":
        return _whoami(resource)
    if command == "logout":
        removed = clear_token()
        print("token removed" if removed else "no token stored")
        return 0
    if command == "discover":
        client = MCPOAuthClient(resource)
        server = client.discover()
        print(f"issuer       : {server.issuer}")
        print(f"token        : {server.token_endpoint or '-'}")
        print(f"register     : {server.registration_endpoint or '-'}")
        print(f"device flow  : {server.supports_device_flow}")
        print(f"pkce S256    : {server.supports_pkce}")
        for candidate in client.servers():
            print(f"  - {candidate.issuer} device={candidate.supports_device_flow}")
        return 0
    print("usage: python3 -m agentpro.mcp_oauth [login|whoami|logout|discover] [resource_url]")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
