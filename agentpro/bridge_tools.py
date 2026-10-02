"""Unified, extensible Tool System for the autonomous agent.

Every device capability is represented as a :class:`Tool` with:

* a name and human description
* an input JSON schema (lightweight, dependency-free)
* input validation
* a permission level (``NORMAL`` / ``OWNER``)
* an executor binding to the bridge client
* a structured :class:`ToolResult`
* an optional verification hook

Dangerous tools (shell, install_apk) are **closed by default**: they are
registered with ``permission=OWNER`` and ``enabled=False``. The autonomy loop
may enable them only after an explicit, traceable owner decision. This layer
mirrors the action-level authorization in :mod:`agentpro.authorization` at the
tool level without duplicating its internals.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from .models import ActionType
from .mcp_client import DEFAULT_MCP_PREFIX
from .screen import ScreenReader, summarize_screen

if TYPE_CHECKING:  # pragma: no cover
    from .mcp_client import MCPClient


class PermissionLevel(str, Enum):
    NORMAL = "NORMAL"
    OWNER = "OWNER"
    SYSTEM = "SYSTEM"


class ToolError(RuntimeError):
    """Raised for structural tool problems (registry, schema, dispatch)."""


class _RemoteToolError(RuntimeError):
    """Internal carrier for a device/tool failure code.

    Raised by a tool when the device (or a resolver) rejected the call with a
    meaningful code such as ``PACKAGE_NOT_FOUND``; the registry turns it into a
    :class:`ToolResult` that keeps the code instead of a generic error.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: Mapping[str, Any]
    permission: PermissionLevel = PermissionLevel.NORMAL

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ToolError("tool name must be a non-empty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ToolError("tool description must be a non-empty string")
        props = self.schema.get("properties")
        if not isinstance(props, Mapping):
            raise ToolError("tool schema must declare a 'properties' object")

    @property
    def arguments_string(self) -> str:
        lines: list[str] = []
        props = self.schema.get("properties", {})
        required = set(self.schema.get("required", []))
        for name, prop in props.items():
            if not isinstance(prop, Mapping):
                continue
            ptype = prop.get("type", "any")
            desc = prop.get("description", "")
            tag = " (required)" if name in required else ""
            lines.append(f"- {name}: {ptype} {desc}{tag}")
        return "\n".join(lines) if lines else "(none)"


@dataclass
class ToolContext:
    """Everything a tool needs at runtime."""

    client: Any
    screen_reader: ScreenReader | None = None
    snapshot: Any = None
    fast: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def last_screenshot_dims(self) -> tuple[int, int] | None:
        dims = self.extra.get("screenshot_dims")
        if isinstance(dims, tuple) and len(dims) == 2:
            return dims
        return None

    def screen_dims(self) -> tuple[int, int] | None:
        """Best-known screen dimensions: screenshot, then snapshot bounds."""
        dims = self.last_screenshot_dims()
        if dims:
            return dims
        snapshot = self.snapshot
        if snapshot is not None:
            elements = getattr(snapshot, "elements", ())
            if elements:
                max_x = max(
                    (getattr(el, "right", 0) for el in elements),
                    default=0,
                )
                max_y = max(
                    (getattr(el, "bottom", 0) for el in elements),
                    default=0,
                )
                if max_x > 0 and max_y > 0:
                    return (max_x, max_y)
        return None


@dataclass
class ToolResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    verification: str | None = None

    @classmethod
    def ok(cls, **data: Any) -> "ToolResult":
        return cls(success=True, data=data)

    @classmethod
    def error(cls, code: str, message: str) -> "ToolResult":
        return cls(success=False, error_code=code, error_message=message)


class Tool:
    spec: ToolSpec

    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        raise NotImplementedError

    def verify(
        self,
        args: Mapping[str, Any],
        before: Any,
        after: Any,
    ) -> str | None:
        """Return a verification keyword or ``None`` for 'not applicable'."""
        return None

    def to_action_type(self) -> ActionType:
        return ActionType.UI_DUMP


class ToolRegistry:
    """Register, validate inputs, and dispatch tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._enabled: dict[str, bool] = {}
        self._hook: Callable[[Mapping[str, Any]], bool] | None = None
        self._failures: dict[str, int] = {}

    def register(self, tool: Tool, *, enabled: bool = True) -> "ToolRegistry":
        if not isinstance(tool, Tool):
            raise ToolError("register() requires a Tool instance")
        name = tool.spec.name
        if name in self._tools:
            raise ToolError(f"tool already registered: {name}")
        self._tools[name] = tool
        self._enabled[name] = enabled and (
            enabled or tool.spec.permission is not PermissionLevel.OWNER
        )
        return self

    def get(self, name: str) -> Tool | None:
        if not isinstance(name, str):
            raise TypeError("tool name must be a string")
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def is_enabled(self, name: str) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            return False
        return bool(self._enabled.get(name, False))

    def enable(self, name: str, *, enabled: bool) -> None:
        if name not in self._tools:
            raise ToolError(f"unknown tool: {name}")
        self._enabled[name] = enabled

    def enabled_names(self) -> list[str]:
        return sorted(name for name, tool in self._tools.items() if self.is_enabled(name))

    def specs_for_prompt(self) -> str:
        sections: list[str] = []
        for name in self.enabled_names():
            tool = self._tools[name]
            sections.append(
                f"- {name}: {tool.spec.description}\n"
                f"  arguments:\n{tool.spec.arguments_string}"
            )
        return "\n".join(sections)

    def validate_args(self, name: str, args: Mapping[str, Any]) -> tuple[bool, list[str]]:
        tool = self._tools.get(name)
        if tool is None:
            return (False, [f"unknown tool: {name}"])
        return _validate_schema(tool.spec.schema, args)

    def execute(self, name: str, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.error("UNKNOWN_TOOL", f"unknown tool: {name}")

        if not isinstance(args, Mapping):
            return ToolResult.error("INVALID_ARGS", "args must be an object")

        valid, errors = _validate_schema(tool.spec.schema, args)
        if not valid:
            return ToolResult.error(
                "INVALID_ARGS",
                f"invalid arguments for {name}: {'; '.join(errors)}",
            )

        if not self.is_enabled(name):
            return ToolResult.error(
                "TOOL_GATED",
                f"tool '{name}' requires owner enablement and is disabled",
            )

        if self._hook is not None:
            try:
                if not self._hook({"tool": name, "args": dict(args)}):
                    return ToolResult.error(
                        "TOOL_DENIED_BY_HOOK",
                        f"tool '{name}' was denied by the authorization hook",
                    )
            except Exception as exc:
                return ToolResult.error(
                    "TOOL_HOOK_ERROR",
                    f"tool authorization hook failed: {exc}",
                )

        blocked = self.repeat_blocked(name, args)
        if blocked is not None:
            return ToolResult.error("REPEAT_BLOCKED", f"{name}: {blocked}")

        try:
            result = tool.execute(ctx, args)
        except _RemoteToolError as exc:
            result = ToolResult.error(exc.code, exc.message)
        except Exception as exc:
            code = getattr(exc, "error_code", None) or "TOOL_EXECUTION_ERROR"
            result = ToolResult.error(code, f"tool '{name}' failed: {exc}")

        self._note_outcome(name, args, result)
        return result

    # -- repetition control ------------------------------------------------

    #: An identical call is blocked once it has failed this many times. One
    #: retry stays allowed (transient device errors happen), a second identical
    #: failure is treated as a dead end and never sent to the device again.
    REPEAT_FAILURE_LIMIT = 2

    @staticmethod
    def _call_key(name: str, args: Mapping[str, Any]) -> str:
        try:
            rendered = repr(sorted((str(k), repr(v)) for k, v in args.items()))
        except TypeError:
            rendered = repr(sorted(str(k) for k in args))
        return f"{name}({rendered})"

    def _note_outcome(
        self,
        name: str,
        args: Mapping[str, Any],
        result: ToolResult,
    ) -> None:
        key = self._call_key(name, args)
        if result.success:
            self._failures.pop(key, None)
            return
        self._failures[key] = self._failures.get(key, 0) + 1

    def repeat_blocked(self, name: str, args: Mapping[str, Any]) -> str | None:
        """Return the recorded error when an identical call is a dead end."""
        key = self._call_key(name, args)
        count = self._failures.get(key, 0)
        if count < self.REPEAT_FAILURE_LIMIT:
            return None
        return (
            f"identical call already failed {count} times; "
            "pick a different tool, target, or screen instead of retrying it"
        )

    def set_authorization_hook(
        self,
        hook: Callable[[Mapping[str, Any]], bool] | None,
    ) -> None:
        self._hook = hook


_T_NUMBER = {"type": "number", "minimum": 0}
_T_INT = {"type": "integer", "minimum": 0}
_T_STRING = {"type": "string"}
_T_BOOL = {"type": "boolean"}


def _validate_schema(
    schema: Mapping[str, Any],
    args: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    props = schema.get("properties", {})
    required = schema.get("required", [])
    seen = set(args)

    for key in required:
        if key not in args:
            errors.append(f"missing required argument '{key}'")
            continue
        spec = props.get(key, {})
        if not _check_type(args[key], spec):
            errors.append(f"argument '{key}' has wrong type")

    for key, value in args.items():
        if key not in props:
            continue
        spec = props.get(key, {})
        if not _check_type(value, spec):
            errors.append(
                f"argument '{key}' must be {spec.get('type', 'any')}"
            )
        else:
            minimum = spec.get("minimum")
            maximum = spec.get("maximum")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if minimum is not None and value < minimum:
                    errors.append(
                        f"argument '{key}' must be >= {minimum}"
                    )
                if maximum is not None and value > maximum:
                    errors.append(
                        f"argument '{key}' must be <= {maximum}"
                    )
            if spec.get("minLength") is not None and isinstance(value, str):
                if len(value) < spec["minLength"]:
                    errors.append(
                        f"argument '{key}' must be at least "
                        f"{spec['minLength']} characters"
                    )

    groups = [g for g in (schema.get("anyOf") or []) if isinstance(g, Mapping)]
    if groups:
        satisfied = any(
            all(key in args for key in group.get("required", [])) for group in groups
        )
        if not satisfied:
            alternatives = " or ".join(
                "/".join(f"'{key}'" for key in group.get("required", []))
                for group in groups
            )
            errors.append(f"one of {alternatives} is required")

    return (not errors, errors)


def _check_type(value: Any, spec: Mapping[str, Any]) -> bool:
    ptype = spec.get("type")
    if ptype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ptype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if ptype == "string":
        return isinstance(value, str)
    if ptype == "boolean":
        return isinstance(value, bool)
    if ptype == "array":
        if not isinstance(value, list):
            return False
        item = spec.get("items", {})
        if not item:
            return True
        return all(_check_type(v, item) for v in value)
    if ptype == "object":
        return isinstance(value, Mapping)
    return True


# ---------------------------------------------------------------------------
# Device tools
# ---------------------------------------------------------------------------


def _resp_data(resp: Any) -> dict[str, Any]:
    data = getattr(resp, "data", None)
    return dict(data) if isinstance(data, Mapping) else {}


class _TapTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="tap",
                description="Tap (click) a point on the screen",
                schema={
                    "type": "object",
                    "required": ["x", "y"],
                    "properties": {
                        "x": dict(_T_NUMBER, description="x coordinate (pixels)"),
                        "y": dict(_T_NUMBER, description="y coordinate (pixels)"),
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        resp = ctx.client.tap(float(args["x"]), float(args["y"]))
        data = _resp_data(resp)
        return ToolResult(success=True, data=data)

    def to_action_type(self) -> ActionType:
        return ActionType.TAP


class _TapElementTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="tap_element",
                description=(
                    "Tap an element from the current UI snapshot by its [N] "
                    "index from the screen summary, or by matching its text / "
                    "content description. Prefer this over raw tap coordinates; "
                    "it targets the element center automatically."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "index": dict(
                            _T_INT,
                            description="element index [N] from the latest screen summary",
                            **{"minimum": 0},
                        ),
                        "text": dict(
                            _T_STRING,
                            description="exact or substring of the element text or content description",
                        ),
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        has_index = isinstance(args.get("index"), (int, float))
        text = args.get("text")
        has_text = isinstance(text, str) and bool(text.strip())

        if not has_index and not has_text:
            return ToolResult.error(
                "INVALID_ARGS",
                "tap_element requires either 'index' or 'text'",
            )

        target = self._resolve(ctx, args)
        if target is None:
            return ToolResult.error(
                "TOOL_TARGET_NOT_FOUND",
                "No element matched the given index/text in the current screen",
            )

        x, y = target.center()
        resp = ctx.client.tap(x, y)
        data = _resp_data(resp)
        data["x"] = x
        data["y"] = y
        data["element_index"] = target.node_index
        data["element_label"] = target.label
        return ToolResult(success=True, data=data)

    def _resolve(self, ctx: ToolContext, args: Mapping[str, Any]):
        if isinstance(args.get("index"), (int, float)):
            index = int(args["index"])
            for el in self._elements(ctx):
                if el.node_index == index:
                    return el
            return None

        needle = str(args["text"]).strip().lower()
        elements = self._elements(ctx)
        for el in elements:
            if not el.clickable or el.editable:
                continue
            label = ((el.text or "").strip()) or (
                (el.content_desc or "").strip()
            )
            if label and needle in label.lower():
                return el
        for el in elements:
            if el.resource_id and needle in el.resource_id.lower():
                return el
        for el in elements:
            if el.clickable or el.editable:
                label = ((el.text or "").strip()) or (
                    (el.content_desc or "").strip()
                )
                if label and needle in label.lower():
                    return el
        return None

    def _elements(self, ctx: ToolContext) -> list:
        snapshot = getattr(ctx, "snapshot", None)
        elements = getattr(snapshot, "elements", ())
        if elements:
            return list(elements)
        if ctx.screen_reader is not None:
            try:
                snap = ctx.screen_reader.observe()
                if getattr(snap, "success", False):
                    return list(getattr(snap, "elements", ()))
            except Exception:
                return []
        return []

    def to_action_type(self) -> ActionType:
        return ActionType.TAP


class _LongPressTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="long_press",
                description="Press and hold a point on the screen",
                schema={
                    "type": "object",
                    "required": ["x", "y"],
                    "properties": {
                        "x": dict(_T_NUMBER, description="x coordinate (pixels)"),
                        "y": dict(_T_NUMBER, description="y coordinate (pixels)"),
                        "duration_ms": dict(
                            _T_INT,
                            description="press duration in milliseconds",
                            **{"minimum": 100, "maximum": 5000},
                        ),
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        resp = ctx.client.long_press(
            float(args["x"]),
            float(args["y"]),
            int(args.get("duration_ms", 600)),
        )
        return ToolResult(success=True, data=_resp_data(resp))

    def to_action_type(self) -> ActionType:
        return ActionType.LONG_PRESS


class _TypeTextTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="type_text",
                description="Type text into the currently focused input field",
                schema={
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": dict(
                            _T_STRING,
                            description="the text to type",
                            **{"minLength": 1, "maxLength": 10000},
                        )
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        resp = ctx.client.input_text(str(args["text"]))
        return ToolResult(success=True, data=_resp_data(resp))

    def to_action_type(self) -> ActionType:
        return ActionType.INPUT_TEXT


class _ClearTextTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="clear_text",
                description="Clear the currently focused input field",
                schema={"type": "object", "properties": {}},
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        resp = ctx.client.clear_text()
        return ToolResult(success=True, data=_resp_data(resp))

    def to_action_type(self) -> ActionType:
        return ActionType.CLEAR_TEXT


class _EraseTextTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="erase_text",
                description=(
                    "Erase all text from the currently focused input field "
                    "(select-all + cut); unlike clear_text it keeps the field "
                    "focused for continued typing"
                ),
                schema={"type": "object", "properties": {}},
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        resp = ctx.client.erase_text()
        return ToolResult(success=True, data=_resp_data(resp))

    def to_action_type(self) -> ActionType:
        return ActionType.ERASE_TEXT


class _SwipeTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="swipe",
                description="Perform a swipe gesture from one point to another",
                schema={
                    "type": "object",
                    "required": ["x1", "y1", "x2", "y2"],
                    "properties": {
                        "x1": dict(_T_NUMBER, description="start x"),
                        "y1": dict(_T_NUMBER, description="start y"),
                        "x2": dict(_T_NUMBER, description="end x"),
                        "y2": dict(_T_NUMBER, description="end y"),
                        "duration_ms": dict(
                            _T_INT,
                            description="gesture duration",
                            **{"minimum": 10, "maximum": 60000},
                        ),
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        resp = ctx.client.swipe(
            float(args["x1"]),
            float(args["y1"]),
            float(args["x2"]),
            float(args["y2"]),
            int(args.get("duration_ms", 300)),
        )
        return ToolResult(success=True, data=_resp_data(resp))

    def to_action_type(self) -> ActionType:
        return ActionType.SWIPE


class _ScrollTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="scroll",
                description="Scroll the current screen in one direction",
                schema={
                    "type": "object",
                    "required": ["direction"],
                    "properties": {
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                            "description": "scroll direction (content moves toward it)",
                        },
                        "distance": dict(
                            _T_INT,
                            description="scroll distance in pixels",
                            **{"minimum": 50, "maximum": 5000},
                        ),
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        direction = str(args["direction"])
        distance = int(args.get("distance", 900))
        dims = ctx.screen_dims() or (1080, 2400)
        width, height = dims
        cx = width // 2
        cy = height // 2

        if direction == "down":
            y1, y2 = cy - distance // 2, cy + distance // 2
            x1 = x2 = cx
        elif direction == "up":
            y1, y2 = cy + distance // 2, cy - distance // 2
            x1 = x2 = cx
        elif direction == "left":
            x1, x2 = cx + distance // 2, cx - distance // 2
            y1 = y2 = cy
        else:  # right
            x1, x2 = cx - distance // 2, cx + distance // 2
            y1 = y2 = cy

        x1 = max(0, min(width - 1, x1))
        y1 = max(0, min(height - 1, y1))
        x2 = max(0, min(width - 1, x2))
        y2 = max(0, min(height - 1, y2))

        resp = ctx.client.swipe(x1, y1, x2, y2, 400)
        return ToolResult(success=True, data=_resp_data(resp))

    def to_action_type(self) -> ActionType:
        return ActionType.SWIPE


class _BackTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="back",
                description="Press the system back button",
                schema={"type": "object", "properties": {}},
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(success=True, data=_resp_data(ctx.client.back()))

    def to_action_type(self) -> ActionType:
        return ActionType.BACK


class _KeyEventTool(Tool):
    _SUPPORTED = (
        "back",
        "home",
        "recents",
        "notifications",
        "quick_settings",
        "power_dialog",
    )

    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="key_event",
                description=(
                    "Trigger a system-level action. For text editing use "
                    "erase_text/clear_text/type_text instead; key_event does "
                    "NOT support enter/delete/volume keys."
                ),
                schema={
                    "type": "object",
                    "required": ["keycode"],
                    "properties": {
                        "keycode": {
                            "type": "string",
                            "enum": list(self._SUPPORTED),
                            "description": "one of: back, home, recents, "
                            "notifications, quick_settings, power_dialog",
                        }
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(
            success=True, data=_resp_data(ctx.client.key_event(str(args["keycode"])))
        )

    def to_action_type(self) -> ActionType:
        return ActionType.KEY_EVENT


class _HomeTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="home",
                description="Go to the home screen",
                schema={"type": "object", "properties": {}},
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(
            success=True, data=_resp_data(ctx.client.key_event("home"))
        )

    def to_action_type(self) -> ActionType:
        return ActionType.KEY_EVENT


class _RecentsTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="recents",
                description="Open the recent apps overview",
                schema={"type": "object", "properties": {}},
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(
            success=True, data=_resp_data(ctx.client.key_event("recents"))
        )

    def to_action_type(self) -> ActionType:
        return ActionType.KEY_EVENT


class _OpenUrlTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="open_url",
                description=(
                    "Open a URL in a browser (http/https or data:text/html "
                    "to render an app without installing anything)"
                ),
                schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": dict(
                            _T_STRING,
                            description="the URL to open",
                            **{"minLength": 1, "maxLength": 20000},
                        )
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(
            success=True, data=_resp_data(ctx.client.open_url(str(args["url"])))
        )

    def to_action_type(self) -> ActionType:
        return ActionType.OPEN_URL


class _OpenAppTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="open_app",
                description=(
                    "Open an app. Pass 'package' for a known Android package "
                    "name, or 'name' for the label shown on the launcher (for "
                    "example 'Calculator'); with 'name' the launcher is read "
                    "and the matching icon is tapped, so no package guessing "
                    "is needed."
                ),
                schema={
                    "type": "object",
                    "anyOf": [{"required": ["package"]}, {"required": ["name"]}],
                    "properties": {
                        "package": dict(
                            _T_STRING,
                            description="Android package name",
                            **{"minLength": 1, "maxLength": 256},
                        ),
                        "name": dict(
                            _T_STRING,
                            description="app label as shown on the launcher",
                            **{"minLength": 1, "maxLength": 128},
                        ),
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        package = args.get("package")
        name = args.get("name")
        has_package = isinstance(package, str) and bool(package.strip())
        has_name = isinstance(name, str) and bool(name.strip())

        if not has_package and not has_name:
            return ToolResult.error(
                "INVALID_ARGS",
                "open_app requires 'package' or 'name'",
            )

        if has_package:
            try:
                data = _resp_data(ctx.client.launch_app(str(package).strip()))
            except Exception as exc:
                code = getattr(exc, "error_code", None)
                if not has_name or code != "PACKAGE_NOT_FOUND":
                    raise
                data = self._launch_by_label(ctx, str(name).strip())
                data["package_attempt"] = str(package).strip()
                return ToolResult(success=True, data=data)
            data["resolved_by"] = "package"
            return ToolResult(success=True, data=data)

        return ToolResult(success=True, data=self._launch_by_label(ctx, str(name).strip()))

    def _launch_by_label(self, ctx: ToolContext, name: str) -> dict[str, Any]:
        from .app_resolver import AppResolutionError, LauncherAppResolver

        resolver = LauncherAppResolver(
            ctx.client,
            settle_seconds=float(ctx.extra.get("launcher_settle_seconds", 1.2)),
        )
        try:
            return resolver.launch(name)
        except AppResolutionError as exc:
            raise _RemoteToolError(exc.code, exc.message) from exc

    def to_action_type(self) -> ActionType:
        return ActionType.OPEN_APP


class _GetWindowTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="get_window_info",
                description="Get the current window's package, activity, and title",
                schema={"type": "object", "properties": {}},
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        if ctx.screen_reader is not None:
            pkg, activity, title = ctx.screen_reader.get_window_info()
            return ToolResult.ok(
                package_name=pkg,
                activity_name=activity,
                window_title=title,
            )
        return ToolResult(success=True, data=_resp_data(ctx.client.get_window()))

    def to_action_type(self) -> ActionType:
        return ActionType.GET_WINDOW


class _DumpUiTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="dump_ui",
                description="Dump the current UI hierarchy",
                schema={"type": "object", "properties": {}},
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(success=True, data=_resp_data(ctx.client.ui_dump()))

    def to_action_type(self) -> ActionType:
        return ActionType.UI_DUMP


class _ScreenshotTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="screenshot",
                description="Capture a screenshot of the current screen",
                schema={"type": "object", "properties": {}},
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(success=True, data=_resp_data(ctx.client.screenshot()))

    def to_action_type(self) -> ActionType:
        return ActionType.SCREENSHOT


class _TakePhotoTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="take_photo",
                description="Capture a photo using the phone camera ('front' or 'back')",
                schema={
                    "type": "object",
                    "properties": {
                        "facing": {
                            "type": "string",
                            "enum": ["front", "back"],
                            "description": "Camera to use ('front' or 'back'). Default is 'front'.",
                        }
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        facing = str(args.get("facing", "front")) if isinstance(args, Mapping) else "front"
        return ToolResult(success=True, data=_resp_data(ctx.client.take_photo(facing=facing)))

    def to_action_type(self) -> ActionType:
        return ActionType.TAKE_PHOTO


class _WaitTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="wait",
                description="Wait for a number of seconds before the next observation",
                schema={
                    "type": "object",
                    "required": ["seconds"],
                    "properties": {
                        "seconds": dict(
                            _T_NUMBER,
                            description="duration in seconds",
                            **{"minimum": 0, "maximum": 5},
                        )
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        seconds = float(args["seconds"])
        time.sleep(seconds)
        return ToolResult.ok(wait_seconds=seconds)

    def to_action_type(self) -> ActionType:
        return ActionType.WAIT


class _WaitForTextTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="wait_for_text",
                description=(
                    "Smart wait: poll the screen until a given text appears "
                    "(or the timeout elapses). Returns whether it was found."
                ),
                schema={
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": dict(
                            _T_STRING,
                            description="text that must appear",
                            **{"minLength": 1},
                        ),
                        "timeout_s": dict(
                            _T_NUMBER,
                            description="max wait in seconds",
                            **{"minimum": 0.1, "maximum": 60},
                        ),
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        # Prefer the FastDriver path: it polls get_window/ui_dump directly
        # and avoids the fixed sleeps inside ScreenReader.wait_for_text.
        fast = getattr(ctx, "fast", None)
        if fast is not None:
            ok = fast.smart_wait(
                text=str(args["text"]),
                timeout=float(args.get("timeout_s", 15.0)),
            )
            return ToolResult.ok(found=ok)

        if ctx.screen_reader is None:
            return ToolResult.error(
                "NO_SCREEN_READER",
                "wait_for_text requires a screen reader",
            )
        found, _snapshot = ctx.screen_reader.wait_for_text(
            str(args["text"]),
            timeout_s=float(args.get("timeout_s", 15.0)),
        )
        return ToolResult.ok(found=found)

    def to_action_type(self) -> ActionType:
        return ActionType.WAIT


class _WaitForScreenStableTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="wait_for_screen_stable",
                description=(
                    "Smart wait: poll until the screen stops changing "
                    "(used after app launches to let UI settle)"
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "timeout_s": dict(
                            _T_NUMBER,
                            description="max wait in seconds",
                            **{"minimum": 0.5, "maximum": 60},
                        ),
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        if ctx.screen_reader is None:
            return ToolResult.error(
                "NO_SCREEN_READER",
                "wait_for_screen_stable requires a screen reader",
            )
        stable, _snapshot = ctx.screen_reader.wait_for_stable(
            timeout_s=float(args.get("timeout_s", 10.0))
        )
        return ToolResult.ok(stable=stable)

    def to_action_type(self) -> ActionType:
        return ActionType.WAIT


class _DangerTool(Tool):
    """Base for owner-gated dangerous tools (closed by default)."""

    def __init__(self, spec: ToolSpec) -> None:
        super().__init__(
            ToolSpec(
                name=spec.name,
                description=spec.description,
                schema=spec.schema,
                permission=PermissionLevel.OWNER,
            )
        )


class _InstallApkTool(_DangerTool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="install_apk",
                description=(
                    "Install an APK from an on-device path. DANGEROUS: requires "
                    "owner authorization and a separate install mechanism."
                ),
                schema={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": dict(
                            _T_STRING,
                            description="path to the APK on the device",
                            **{"minLength": 1, "maxLength": 1024},
                        )
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult.error(
            "NOT_IMPLEMENTED_BY_AGENT",
            "install_apk must be performed by the sidecar/owner flow; "
            "the agent never installs packages itself",
        )

    def to_action_type(self) -> ActionType:
        return ActionType.INSTALL_APK


class _ShellTool(_DangerTool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="shell",
                description=(
                    "Execute an arbitrary shell command on the device. "
                    "DANGEROUS and closed by default: requires owner enablement "
                    "and is not routed through the standard bridge."
                ),
                schema={
                    "type": "object",
                    "required": ["command"],
                    "properties": {
                        "command": dict(
                            _T_STRING,
                            description="shell command line",
                            **{"minLength": 1, "maxLength": 2048},
                        )
                    },
                },
            )
        )

    def execute(self, ctx: ToolContext, args: Mapping[str, Any]) -> ToolResult:
        return ToolResult.error(
            "NOT_IMPLEMENTED_BY_AGENT",
            "shell execution is closed by default and must be routed through "
            "a dedicated, owner-authorized transport",
        )

    def to_action_type(self) -> ActionType:
        return ActionType.KEY_EVENT


def build_default_registry(
    *,
    include_dangerous: bool = False,
    mcp: "MCPClient | None" = None,
    mcp_prefix: str = DEFAULT_MCP_PREFIX,
) -> ToolRegistry:
    """Construct the standard tool registry.

    Dangerous tools (``shell``, ``install_apk``) are registered but **disabled**
    unless ``include_dangerous`` is set; even then they stay gated behind the
    registry authorization hook / permission level until explicitly enabled.

    Passing ``mcp`` registers that server's tools as well (prefixed by
    ``mcp_prefix``), so remote capabilities join the same planning loop.
    """
    registry = ToolRegistry()
    registry.register(_TapTool())
    registry.register(_TapElementTool())
    registry.register(_LongPressTool())
    registry.register(_TypeTextTool())
    registry.register(_ClearTextTool())
    registry.register(_EraseTextTool())
    registry.register(_SwipeTool())
    registry.register(_ScrollTool())
    registry.register(_BackTool())
    registry.register(_HomeTool())
    registry.register(_RecentsTool())
    registry.register(_KeyEventTool())
    registry.register(_OpenUrlTool())
    registry.register(_OpenAppTool())
    registry.register(_GetWindowTool())
    registry.register(_DumpUiTool())
    registry.register(_ScreenshotTool())
    registry.register(_TakePhotoTool())
    registry.register(_WaitTool())
    registry.register(_WaitForTextTool())
    registry.register(_WaitForScreenStableTool())

    registry.register(_InstallApkTool(), enabled=False)
    registry.register(_ShellTool(), enabled=False)

    if not include_dangerous:
        registry.enable("install_apk", enabled=False)
        registry.enable("shell", enabled=False)

    if mcp is not None:
        from .mcp_tools import register_mcp_tools

        register_mcp_tools(registry, mcp, prefix=mcp_prefix)

    return registry


def tool_result_error_summary(result: ToolResult) -> str:
    if result.success:
        return "ok"
    return f"{result.error_code}: {result.error_message or 'failed'}"


def tool_result_to_string(result: ToolResult) -> str:
    if result.success:
        return summarize_tool_data(result.data)
    return f"ERROR {result.error_code}: {result.error_message or 'failed'}"


def summarize_tool_data(data: Mapping[str, Any], limit: int = 800) -> str:
    if not data:
        return "no data"
    compact = str(dict(data))
    if len(compact) > limit:
        return compact[:limit] + "..."
    return compact