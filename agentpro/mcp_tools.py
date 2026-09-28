"""Expose remote MCP tools as first-class AgentPro tools.

The agent already speaks "registry + :class:`Tool` + :class:`ToolResult`". This
module bridges that world to an MCP server: every tool the server advertises
becomes a local :class:`Tool` with the same JSON schema, so the planner, the
authorization hook, the repeat-breaker, and the trace recorder all keep working
unchanged.

Nothing here is specific to image/video generation: point it at any MCP
endpoint (Higgsfield, a local filesystem server, a documentation server) and its
tools land in the same registry as ``tap`` and ``screenshot``.

    client = mcp_client_from_env()
    registry = build_default_registry(mcp=client)
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .bridge_tools import (
    PermissionLevel,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from .mcp_client import (
    DEFAULT_MCP_PREFIX,
    DEFAULT_MCP_URL,
    DEFAULT_PROTOCOL_VERSION,
    MCPClient,
    MCPError,
    MCPToolInfo,
    MCPToolResult,
)
from .models import ActionType

ENV_MCP_URL = "AGENTPRO_MCP_URL"
ENV_MCP_TIMEOUT = "AGENTPRO_MCP_TIMEOUT"
ENV_MCP_PREFIX = "AGENTPRO_MCP_PREFIX"
ENV_MCP = "AGENTPRO_MCP"
ENV_MCP_INCLUDE = "AGENTPRO_MCP_INCLUDE"
ENV_MCP_EXCLUDE = "AGENTPRO_MCP_EXCLUDE"

_TRUTHY = {"1", "on", "true", "yes", "enable", "enabled"}

DEFAULT_TIMEOUT = 30.0
MAX_REMOTE_TOOLS = 80
MAX_DESCRIPTION = 400

_T_STRING = {"type": "string"}
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_tool_name(remote_name: str, prefix: str = DEFAULT_MCP_PREFIX) -> str:
    """Turn a remote tool name into a registry-safe local name.

    MCP servers namespace tools with slashes or colons (``marketing/ads``);
    the registry and the planner prompts are happier with ``[A-Za-z0-9_.-]``.
    """
    cleaned = _INVALID_NAME_CHARS.sub("_", remote_name).strip("_")
    if not cleaned:
        cleaned = "tool"
    return f"{prefix}{cleaned}"


def normalize_schema(input_schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """Coerce a remote JSON schema into the shape :class:`ToolSpec` expects."""
    schema: dict[str, Any] = dict(input_schema or {})
    props = schema.get("properties")
    schema["type"] = "object"
    schema["properties"] = dict(props) if isinstance(props, Mapping) else {}
    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        schema["required"] = [str(key) for key in required]
    else:
        schema["required"] = []
    for noise in ("$schema", "$id", "additionalProperties", "definitions", "$defs"):
        schema.pop(noise, None)
    return schema


def _describe(info: MCPToolInfo) -> str:
    description = " ".join((info.description or "").split())
    if not description:
        description = f"Remote MCP tool '{info.name}'"
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION] + "..."
    return description


class _RemoteMCPTool(Tool):
    """One MCP tool, callable like any built-in device tool."""

    def __init__(
        self,
        info: MCPToolInfo,
        client: MCPClient,
        *,
        local_name: str,
        source: str,
    ) -> None:
        super().__init__(
            ToolSpec(
                name=local_name,
                description=_describe(info),
                schema=normalize_schema(info.input_schema),
                permission=PermissionLevel.NORMAL,
            )
        )
        self.client = client
        self.remote_name = info.name
        self.source = source

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        try:
            result = self.client.call_tool(self.remote_name, dict(args))
        except MCPError as exc:
            return ToolResult.error("MCP_TRANSPORT_ERROR", str(exc))
        except ValueError as exc:
            return ToolResult.error("MCP_INVALID_ARGS", str(exc))

        data: dict[str, Any] = {
            "remote_tool": self.remote_name,
            "source": self.source,
            "text": result.summary(),
        }
        if result.structured is not None:
            data["structured"] = dict(result.structured)
        if result.images:
            data["images"] = [
                {
                    "mimeType": block.get("mimeType"),
                    "base64_chars": len(str(block.get("data") or "")),
                }
                for block in result.images
            ]
        if not result.ok:
            return ToolResult(
                success=False,
                data=data,
                error_code="MCP_TOOL_ERROR",
                error_message=result.summary(limit=300) or "remote tool reported failure",
            )
        return ToolResult.ok(**data)

    def to_action_type(self) -> ActionType:
        return ActionType.MCP_CALL


class _MCPListToolsTool(Tool):
    """Discovery tool: what can this MCP server do right now?"""

    def __init__(self, client: MCPClient, *, source: str) -> None:
        super().__init__(
            ToolSpec(
                name="mcp_list_tools",
                description=(
                    "List the tools offered by the connected MCP server, "
                    "optionally filtered by a substring of the tool name."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "filter": dict(
                            _T_STRING,
                            description="case-insensitive substring filter",
                        )
                    },
                },
                permission=PermissionLevel.NORMAL,
            )
        )
        self.client = client
        self.source = source

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        needle = str(args.get("filter") or "").strip().lower()
        try:
            infos = self.client.list_tools()
        except MCPError as exc:
            return ToolResult.error("MCP_TRANSPORT_ERROR", str(exc))
        rows: list[dict[str, Any]] = []
        for info in infos:
            if needle and needle not in info.name.lower():
                continue
            rows.append(
                {
                    "name": info.name,
                    "description": " ".join((info.description or "").split())[:200],
                    "arguments": info.argument_names(),
                }
            )
        return ToolResult.ok(source=self.source, count=len(rows), tools=rows)

    def to_action_type(self) -> ActionType:
        return ActionType.MCP_CALL


def register_mcp_tools(
    registry: ToolRegistry,
    client: MCPClient,
    *,
    prefix: str = DEFAULT_MCP_PREFIX,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    enabled: bool = True,
    refresh: bool = True,
    max_tools: int = MAX_REMOTE_TOOLS,
    source: str | None = None,
    add_list_tool: bool = True,
) -> list[str]:
    """Register an MCP server's tools into ``registry``.

    ``include``/``exclude`` accept shell-style patterns matched against the
    *remote* names. Returns the local tool names that were registered.
    """
    origin = source or client.url
    infos = client.list_tools(refresh=refresh)

    if include:
        patterns = tuple(include)
        infos = [
            info
            for info in infos
            if any(fnmatch.fnmatch(info.name, pattern) for pattern in patterns)
        ]
    if exclude:
        patterns = tuple(exclude)
        infos = [
            info
            for info in infos
            if not any(fnmatch.fnmatch(info.name, pattern) for pattern in patterns)
        ]
    if max_tools > 0:
        infos = infos[:max_tools]

    registered: list[str] = []
    taken = set(registry.names())
    for info in infos:
        local_name = sanitize_tool_name(info.name, prefix)
        candidate = local_name
        suffix = 2
        while candidate in taken:
            candidate = f"{local_name}_{suffix}"
            suffix += 1
        registry.register(
            _RemoteMCPTool(info, client, local_name=candidate, source=origin),
            enabled=enabled,
        )
        taken.add(candidate)
        registered.append(candidate)

    if add_list_tool and "mcp_list_tools" not in taken:
        registry.register(
            _MCPListToolsTool(client, source=origin),
            enabled=enabled,
        )
        registered.append("mcp_list_tools")

    return registered


def mcp_client_from_env(
    url: str | None = None,
    *,
    transport: Any = None,
    timeout: float | None = None,
    initialize: bool = True,
    use_token: bool = True,
    **kwargs: Any,
) -> MCPClient:
    """Build a client from ``AGENTPRO_MCP_*`` env vars, defaulting to Higgsfield.

    When an OAuth token is present (``AGENTPRO_MCP_TOKEN`` or
    ``~/.agentpro/mcp_token.json``, written by ``python3 -m agentpro.mcp_oauth
    login``) it is attached as a bearer header and refreshed automatically.
    """
    target = url or os.environ.get(ENV_MCP_URL) or DEFAULT_MCP_URL
    if timeout is None:
        raw = os.environ.get(ENV_MCP_TIMEOUT, "").strip()
        try:
            timeout = float(raw) if raw else DEFAULT_TIMEOUT
        except ValueError:
            timeout = DEFAULT_TIMEOUT
    if "extra_headers" not in kwargs and use_token:
        from .mcp_oauth import bearer_header_factory

        factory = bearer_header_factory(target, timeout=timeout)
        if factory is not None:
            kwargs["extra_headers"] = factory
    client = MCPClient(
        target,
        transport=transport,
        timeout=timeout,
        **kwargs,
    )
    if initialize:
        client.initialize()
    return client


def default_prefix_from_env(fallback: str = DEFAULT_MCP_PREFIX) -> str:
    raw = os.environ.get(ENV_MCP_PREFIX, "").strip()
    return raw or fallback


def mcp_enabled_from_env() -> bool:
    """MCP stays off unless asked for, so startup never touches the network."""
    if (os.environ.get(ENV_MCP, "") or "").strip().lower() in _TRUTHY:
        return True
    return bool((os.environ.get(ENV_MCP_URL, "") or "").strip())


def _patterns_from_env(name: str) -> list[str] | None:
    raw = os.environ.get(name, "") or ""
    items = [part.strip() for part in raw.split(",")]
    items = [part for part in items if part]
    return items or None


@dataclass(frozen=True)
class MCPAttachment:
    """Outcome of :func:`attach_mcp_from_env`."""

    enabled: bool
    names: tuple[str, ...] = ()
    url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.enabled and not self.error


def attach_mcp_from_env(
    registry: ToolRegistry,
    *,
    url: str | None = None,
    force: bool = False,
    client: MCPClient | None = None,
    **kwargs: Any,
) -> MCPAttachment:
    """Register MCP tools from the environment, never raising.

    Enable with ``AGENTPRO_MCP=1`` (or by setting ``AGENTPRO_MCP_URL``), narrow
    the catalogue with ``AGENTPRO_MCP_INCLUDE``/``AGENTPRO_MCP_EXCLUDE`` shell
    patterns, and authenticate with ``python3 -m agentpro.mcp_oauth login``.
    A failing endpoint is reported through ``error`` instead of breaking the
    agent, which keeps the Android side usable offline.
    """
    target = url or os.environ.get(ENV_MCP_URL) or DEFAULT_MCP_URL
    attached = getattr(registry, "_mcp_sources", None)
    if attached is None:
        attached = set()
        registry._mcp_sources = attached  # noqa: SLF001 - registry-local bookkeeping
    if client is not None:
        origin = client.url
    elif target in attached:
        return MCPAttachment(enabled=True, url=target)
    else:
        origin = target
    if not (force or client is not None or mcp_enabled_from_env()):
        return MCPAttachment(enabled=False, url=target)

    register_kwargs: dict[str, Any] = {
        key: kwargs.pop(key)
        for key in ("prefix", "max_tools", "enabled", "refresh", "source", "add_list_tool")
        if key in kwargs
    }
    register_kwargs.setdefault("prefix", default_prefix_from_env())
    include = kwargs.pop("include", None) or _patterns_from_env(ENV_MCP_INCLUDE)
    exclude = kwargs.pop("exclude", None) or _patterns_from_env(ENV_MCP_EXCLUDE)

    try:
        mcp = client if client is not None else mcp_client_from_env(target, **kwargs)
        names = register_mcp_tools(
            registry,
            mcp,
            include=include,
            exclude=exclude,
            **register_kwargs,
        )
    except (MCPError, OSError, ValueError, TypeError) as exc:
        return MCPAttachment(enabled=True, url=target, error=str(exc))
    attached.add(origin)
    return MCPAttachment(enabled=True, names=tuple(names), url=target)
