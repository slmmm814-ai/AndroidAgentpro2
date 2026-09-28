"""A dependency-free MCP (Model Context Protocol) client.

Speaks JSON-RPC 2.0 over the Streamable HTTP transport, which means a single
POST endpoint that answers either with one JSON object or with an
``text/event-stream`` body. The client handles the handshake
(``initialize`` then the ``notifications/initialized`` notification), session
id propagation, ``tools/list`` pagination, and ``tools/call`` result decoding
(text and image content blocks).

Only the standard library is used, matching the rest of the project: the
transport is injectable so tests never touch the network.

Typical use::

    client = MCPClient("https://mcp.higgsfield.ai/mcp")
    for info in client.list_tools():
        print(info.name, info.description)
    result = client.call_tool("use-after-effects", {"prompt": "..."})
    print(result.text)
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

#: Protocol revision this client advertises. Servers answer with the revision
#: they support; :attr:`MCPClient.protocol_version` reflects the negotiated one.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_USER_AGENT = "AndroidAgentPro/1.0"

#: Higgsfield's public endpoint. It needs no API key.
DEFAULT_MCP_URL = "https://mcp.higgsfield.ai/mcp"

#: Prefix applied to local tool names so remote tools are easy to spot.
DEFAULT_MCP_PREFIX = "hf_"

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class MCPError(RuntimeError):
    """Raised for transport, protocol, or remote JSON-RPC failures."""


@dataclass(frozen=True)
class MCPResponse:
    """A raw transport response, kept transport-agnostic for testing."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class MCPTransport:
    """Minimal POST-only transport contract."""

    def post(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> MCPResponse:
        raise NotImplementedError


def default_user_agent() -> str:
    """A product User-Agent; some gateways reject the stdlib default."""
    return os.environ.get("AGENTPRO_MCP_USER_AGENT") or DEFAULT_USER_AGENT


class HTTPMCPTransport(MCPTransport):
    """``urllib``-backed transport: standard library, no third-party deps."""

    def __init__(self, opener: Any = None) -> None:
        self._opener = opener or urllib.request.urlopen

    def post(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> MCPResponse:
        request = urllib.request.Request(
            url, data=body, method="POST", headers=dict(headers)
        )
        if "User-Agent" not in request.headers:
            request.add_header("User-Agent", default_user_agent())
        try:
            with self._opener(request, timeout=timeout) as response:
                return MCPResponse(
                    status=int(getattr(response, "status", 200) or 200),
                    headers=dict(getattr(response, "headers", {}) or {}),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            detail = b""
            try:
                detail = exc.read()
            except Exception:  # pragma: no cover - defensive
                pass
            return MCPResponse(
                status=int(exc.code),
                headers=dict(getattr(exc, "headers", {}) or {}),
                body=detail,
            )


@dataclass(frozen=True)
class MCPToolInfo:
    """One tool advertised by an MCP server."""

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> "MCPToolInfo":
        if not isinstance(payload, Mapping):
            raise MCPError("tool entry must be a JSON object")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MCPError("tool entry is missing a name")
        schema = payload.get("inputSchema")
        if not isinstance(schema, Mapping):
            schema = payload.get("input_schema")
        return cls(
            name=name,
            description=str(payload.get("description") or ""),
            input_schema=dict(schema) if isinstance(schema, Mapping) else {},
        )

    def argument_names(self) -> list[str]:
        props = self.input_schema.get("properties")
        if not isinstance(props, Mapping):
            return []
        return [str(key) for key in props]


@dataclass(frozen=True)
class MCPToolResult:
    """Decoded ``tools/call`` result."""

    tool: str
    is_error: bool = False
    content: tuple[Mapping[str, Any], ...] = ()
    structured: Mapping[str, Any] | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.is_error

    @property
    def text(self) -> str:
        parts = [
            str(block.get("text", ""))
            for block in self.content
            if isinstance(block, Mapping) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)

    @property
    def images(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            block
            for block in self.content
            if isinstance(block, Mapping) and block.get("type") == "image"
        )

    def summary(self, limit: int = 600) -> str:
        """Compact, prompt-friendly rendering of the result."""
        parts: list[str] = []
        if self.text:
            parts.append(self.text)
        if self.structured:
            parts.append(json.dumps(self.structured, ensure_ascii=False)[:limit])
        for block in self.images:
            mime = block.get("mimeType") or "image"
            size = len(str(block.get("data") or ""))
            parts.append(f"[{mime} image, {size} base64 chars]")
        joined = "\n".join(parts) if parts else "(empty result)"
        return joined if len(joined) <= limit else joined[:limit] + "..."

    def save_images(self, directory: str, prefix: str = "mcp_image") -> list[str]:
        """Decode image blocks to files and return the written paths."""
        written: list[str] = []
        os.makedirs(directory, exist_ok=True)
        for index, block in enumerate(self.images, start=1):
            data = block.get("data")
            if not isinstance(data, str) or not data:
                continue
            mime = str(block.get("mimeType") or "image/png")
            extension = mime.split("/")[-1].split(";")[0] or "png"
            path = os.path.join(directory, f"{prefix}_{index}.{extension}")
            try:
                raw = base64.b64decode(data, validate=False)
            except Exception:
                continue
            with open(path, "wb") as handle:
                handle.write(raw)
            written.append(path)
        return written


class MCPClient:
    """JSON-RPC 2.0 client for a single Streamable HTTP MCP endpoint."""

    def __init__(
        self,
        url: str,
        *,
        transport: MCPTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        client_name: str = "AndroidAgentPro",
        client_version: str = "1.0",
        extra_headers: "Mapping[str, str] | Callable[[], Mapping[str, str]] | None" = None,
        initialize: bool = False,
    ) -> None:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("MCP endpoint url must be a non-empty string")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")

        self.url = url.strip()
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff = float(backoff)
        self.protocol_version = protocol_version
        self.client_name = client_name
        self.client_version = client_version
        self.extra_headers = dict(extra_headers) if isinstance(extra_headers, Mapping) else {}
        self._extra_headers_factory = extra_headers if callable(extra_headers) else None
        self.transport = transport or HTTPMCPTransport()

        self.session_id: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.initialized = False
        self._request_id = 0
        self._tool_cache: tuple[MCPToolInfo, ...] | None = None
        if initialize:
            self.initialize()

    def __enter__(self) -> "MCPClient":
        if not self.initialized:
            self.initialize()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Forget session state; the endpoint is stateless per request."""
        self.session_id = None
        self.initialized = False
        self.server_info = {}
        self.capabilities = {}
        self._tool_cache = None

    # -- protocol plumbing ------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        headers.update(self.extra_headers)
        if self._extra_headers_factory is not None:
            headers.update(
                {
                    str(k): str(v)
                    for k, v in dict(self._extra_headers_factory()).items()
                }
            )
        return headers

    @staticmethod
    def _detail(response: MCPResponse, limit: int = 300) -> str:
        text = response.text().strip()
        if not text:
            return "(empty body)"
        return text[:limit]

    @staticmethod
    def _sse_messages(text: str) -> list[Any]:
        """Extract JSON payloads from an SSE body."""
        payloads: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                payloads.append(line[5:].strip())
        messages: list[Any] = []
        for payload in payloads:
            if not payload or payload == "[DONE]":
                continue
            try:
                messages.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        return messages

    @classmethod
    def _extract(cls, response: MCPResponse) -> Any:
        content_type = (response.header("Content-Type") or "").lower()
        text = response.text().strip()
        if not text:
            return None
        if "text/event-stream" in content_type or text.startswith(("data:", "event:")):
            for message in cls._sse_messages(text):
                if isinstance(message, Mapping) and (
                    "result" in message or "error" in message
                ):
                    return message
                if isinstance(message, list) and message:
                    return {"result": message[0]}
            raise MCPError("event stream carried no JSON-RPC response")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPError(f"response was not JSON: {cls._detail(response)}") from exc

    def _post(self, payload: Mapping[str, Any], *, expect_response: bool = True) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        attempts = self.max_retries + 1
        last_error = ""

        for attempt in range(attempts):
            try:
                response = self.transport.post(
                    self.url, body, self._headers(), self.timeout
                )
            except (urllib.error.URLError, OSError) as exc:
                last_error = f"transport error: {exc}"
                if attempt + 1 >= attempts:
                    break
                time.sleep(self.backoff * (2**attempt))
                continue

            if response.status in _RETRYABLE_STATUS and attempt + 1 < attempts:
                time.sleep(self.backoff * (2**attempt))
                continue

            if response.status >= 400:
                raise MCPError(
                    f"HTTP {response.status} from {self.url}: "
                    f"{self._detail(response)}"
                )

            session = response.header("Mcp-Session-Id")
            if session:
                self.session_id = session

            if not expect_response:
                return None

            message = self._extract(response)
            if message is None:
                raise MCPError(f"empty response from {self.url}")
            if isinstance(message, Mapping) and message.get("error"):
                error = message["error"]
                if isinstance(error, Mapping):
                    code = error.get("code", "unknown")
                    message_text = error.get("message", "")
                    raise MCPError(f"MCP error {code}: {message_text}")
                raise MCPError(f"MCP error: {error}")
            if isinstance(message, Mapping) and "result" in message:
                return message["result"]
            return message

        raise MCPError(f"MCP request failed after {attempts} attempt(s): {last_error}")

    # -- MCP methods ------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        """Run the handshake once; later calls are no-ops."""
        if self.initialized:
            return self.server_info
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self.client_name,
                        "version": self.client_version,
                    },
                },
            }
        )
        if isinstance(result, Mapping):
            info = result.get("serverInfo")
            self.server_info = dict(info) if isinstance(info, Mapping) else {}
            capabilities = result.get("capabilities")
            self.capabilities = (
                dict(capabilities) if isinstance(capabilities, Mapping) else {}
            )
            negotiated = result.get("protocolVersion")
            if isinstance(negotiated, str) and negotiated.strip():
                self.protocol_version = negotiated
        self._notify("notifications/initialized")
        self.initialized = True
        return self.server_info

    def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        try:
            self._post(payload, expect_response=False)
        except MCPError:
            pass

    def ping(self) -> bool:
        if not self.initialized:
            self.initialize()
        self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "ping"})
        return True

    def list_tools(self, *, refresh: bool = False) -> list[MCPToolInfo]:
        """Return every advertised tool, following pagination cursors."""
        if self._tool_cache is not None and not refresh:
            return list(self._tool_cache)
        if not self.initialized:
            self.initialize()

        tools: list[MCPToolInfo] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/list",
                    "params": params,
                }
            )
            entries = result.get("tools") if isinstance(result, Mapping) else None
            for entry in entries or []:
                tools.append(MCPToolInfo.from_payload(entry))
            cursor = result.get("nextCursor") if isinstance(result, Mapping) else None
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)

        self._tool_cache = tuple(tools)
        return list(tools)

    def find_tool(self, name: str) -> MCPToolInfo | None:
        for info in self.list_tools():
            if info.name == name:
                return info
        return None

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> MCPToolResult:
        """Invoke a remote tool and decode its content blocks."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool name must be a non-empty string")
        if not self.initialized:
            self.initialize()

        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments or {})},
            }
        )
        if not isinstance(result, Mapping):
            return MCPToolResult(tool=name, raw={"result": result})

        content_raw = result.get("content")
        blocks: list[Mapping[str, Any]] = [
            block for block in (content_raw or []) if isinstance(block, Mapping)
        ]
        structured = result.get("structuredContent")
        return MCPToolResult(
            tool=name,
            is_error=bool(result.get("isError")),
            content=tuple(blocks),
            structured=dict(structured) if isinstance(structured, Mapping) else None,
            raw=dict(result),
        )
