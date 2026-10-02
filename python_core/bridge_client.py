#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping


PROTOCOL = "ultimate"
VERSION = "1.0"
HOST = "127.0.0.1"
PORT = 8070
PATH = "/v1/command"

CONNECT_TIMEOUT_SECONDS = 5.0

# The Bridge guarantees a response within ~6s for gesture/input commands
# (GESTURE_TIMEOUT_MS=5s + routing overhead). Keep the client read timeout
# comfortably above that so slow devices surface a real remote error code
# instead of a misleading local socket timeout.
READ_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024


class BridgeClientError(Exception):
    """Base exception for Bridge client failures."""


class BridgeConfigurationError(BridgeClientError):
    """Raised when the local client configuration is invalid."""


class BridgeConnectionError(BridgeClientError):
    """Raised when the local Bridge cannot be reached."""


class BridgeTimeoutError(BridgeClientError):
    """Raised when the Bridge does not respond in time."""


class BridgeProtocolError(BridgeClientError):
    """Raised when the Bridge sends an invalid protocol response."""


class BridgeRemoteError(BridgeClientError):
    """Raised when the Bridge rejects a valid request.

    ``error_code`` carries the device-side code (for example
    ``PACKAGE_NOT_FOUND``) so callers can react to a specific failure instead
    of parsing the human-readable message.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.error_message = error_message


@dataclass(frozen=True)
class BridgeResponse:
    protocol: str
    version: str
    ok: bool
    request_id: str
    data: dict[str, Any] | None
    error_code: str | None
    error_message: str | None

    @classmethod
    def from_json(cls, payload: Any) -> "BridgeResponse":
        if not isinstance(payload, dict):
            raise BridgeProtocolError(
                "Bridge response root must be a JSON object"
            )

        protocol = payload.get("protocol")
        version = payload.get("version")
        ok = payload.get("ok")
        request_id = payload.get("request_id")

        if protocol != PROTOCOL:
            raise BridgeProtocolError(
                f"Unsupported response protocol: {protocol!r}"
            )

        if version != VERSION:
            raise BridgeProtocolError(
                f"Unsupported response version: {version!r}"
            )

        if not isinstance(ok, bool):
            raise BridgeProtocolError(
                "Bridge response field 'ok' must be boolean"
            )

        if not isinstance(request_id, str):
            raise BridgeProtocolError(
                "Bridge response field 'request_id' must be a string"
            )

        if len(request_id) > 128:
            raise BridgeProtocolError(
                "Bridge response request_id is too long"
            )

        if ok:
            data = payload.get("data", {})

            if not isinstance(data, dict):
                raise BridgeProtocolError(
                    "Successful Bridge response field 'data' must be an object"
                )

            return cls(
                protocol=protocol,
                version=version,
                ok=True,
                request_id=request_id,
                data=data,
                error_code=None,
                error_message=None,
            )

        error = payload.get("error")

        if not isinstance(error, dict):
            raise BridgeProtocolError(
                "Failed Bridge response must contain an 'error' object"
            )

        error_code = error.get("code")
        error_message = error.get("message")

        if not isinstance(error_code, str) or not error_code:
            raise BridgeProtocolError(
                "Bridge error code must be a non-empty string"
            )

        if not isinstance(error_message, str):
            raise BridgeProtocolError(
                "Bridge error message must be a string"
            )

        return cls(
            protocol=protocol,
            version=version,
            ok=False,
            request_id=request_id,
            data=None,
            error_code=error_code,
            error_message=error_message,
        )


class BridgeClient:
    """
    Synchronous HTTP client for AndroidAgentPro BridgeServer.

    Authentication is read exclusively from the
    ANDROID_AGENT_PRO_TOKEN environment variable.
    """

    # Mirrors ScreenshotEngine.MIN_CAPTURE_INTERVAL_MS on the device.
    SCREENSHOT_MIN_INTERVAL_S = 0.3

    def __init__(
        self,
        token: str | None = None,
        host: str = HOST,
        port: int = PORT,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token or os.environ.get("ANDROID_AGENT_PRO_TOKEN", "")

        if not self._token:
            raise BridgeConfigurationError(
                "ANDROID_AGENT_PRO_TOKEN is not configured"
            )

        if not host:
            raise BridgeConfigurationError(
                "Bridge host must not be empty"
            )

        if not 1 <= port <= 65535:
            raise BridgeConfigurationError(
                f"Invalid Bridge port: {port}"
            )

        if connect_timeout <= 0:
            raise BridgeConfigurationError(
                "connect_timeout must be greater than zero"
            )

        if read_timeout <= 0:
            raise BridgeConfigurationError(
                "read_timeout must be greater than zero"
            )

        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    def command(
        self,
        command: str,
        args: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> BridgeResponse:
        if not isinstance(command, str) or not command.strip():
            raise BridgeConfigurationError(
                "command must be a non-empty string"
            )

        command = command.strip()

        if len(command) > 64:
            raise BridgeConfigurationError(
                "command must not exceed 64 characters"
            )

        if args is None:
            request_args: dict[str, Any] = {}
        elif isinstance(args, Mapping):
            request_args = dict(args)
        else:
            raise BridgeConfigurationError(
                "args must be a mapping/object"
            )

        if request_id is None:
            request_id = str(uuid.uuid4())

        if not isinstance(request_id, str) or not request_id:
            raise BridgeConfigurationError(
                "request_id must be a non-empty string"
            )

        if len(request_id) > 128:
            raise BridgeConfigurationError(
                "request_id must not exceed 128 characters"
            )

        payload = {
            "protocol": PROTOCOL,
            "version": VERSION,
            "request_id": request_id,
            "command": command,
            "args": request_args,
        }

        body = self._encode_json(payload)

        if len(body) > MAX_REQUEST_BYTES:
            raise BridgeConfigurationError(
                "Request body exceeds the maximum allowed size"
            )

        response_bytes = self._post(body)

        try:
            response_payload = json.loads(
                response_bytes.decode("utf-8")
            )
        except UnicodeDecodeError as exc:
            raise BridgeProtocolError(
                "Bridge response is not valid UTF-8"
            ) from exc
        except json.JSONDecodeError as exc:
            raise BridgeProtocolError(
                "Bridge response is not valid JSON"
            ) from exc

        response = BridgeResponse.from_json(response_payload)

        if response.request_id != request_id:
            raise BridgeProtocolError(
                "Bridge response request_id does not match request"
            )

        if not response.ok:
            raise BridgeRemoteError(
                f"{response.error_code}: {response.error_message}"
            )

        return response

    def health(self) -> BridgeResponse:
        return self.command("health")

    def ui_dump(self) -> BridgeResponse:
        return self.command("ui_dump")

    def take_photo(self, facing: str = "front") -> BridgeResponse:
        """Capture a photo using the device camera ('front' or 'back')."""
        facing_clean = facing.strip().lower() if isinstance(facing, str) else "front"
        if facing_clean not in ("front", "back"):
            raise BridgeConfigurationError(
                "take_photo facing must be 'front' or 'back'"
            )
        return self.command("take_photo", {"facing": facing_clean})

    def shizuku_status(self) -> BridgeResponse:
        """Check if Shizuku service is available and has permission."""
        return self.command("shizuku_status")

    def shizuku_shell(self, command: str) -> BridgeResponse:
        """Execute a shell command via Shizuku (ADB-level access)."""
        if not isinstance(command, str) or not command.strip():
            raise BridgeConfigurationError("shizuku_shell command must be a non-empty string")
        return self.command("shizuku_shell", {"command": command.strip()})

    def ghost_start(self) -> BridgeResponse:
        """Start Ghost Mode (create virtual display)."""
        return self.command("ghost_start")

    def ghost_stop(self) -> BridgeResponse:
        """Stop Ghost Mode (release virtual display)."""
        return self.command("ghost_stop")

    def screenshot(self, *, retries: int = 2) -> BridgeResponse:
        """Capture the screen; the response carries a base64 PNG.

        The device refuses captures closer together than 250 ms
        (``ScreenshotEngine.MIN_CAPTURE_INTERVAL_MS``). Rather than surface
        that as an error, wait out the interval and retry, because a caller
        asking for two screenshots in a row wants both frames.
        """
        import time

        attempts = max(0, int(retries)) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.command("screenshot")
            except BridgeRemoteError as exc:
                last_error = exc
                if "too frequently" not in str(exc) or attempt == attempts - 1:
                    raise
                time.sleep(self.SCREENSHOT_MIN_INTERVAL_S)
                continue
            if response.ok:
                return response
            if (
                "too frequently" not in (response.error_message or "")
                or attempt == attempts - 1
            ):
                return response
            time.sleep(self.SCREENSHOT_MIN_INTERVAL_S)
        raise BridgeRemoteError(f"screenshot failed: {last_error}")

    def screen_size(self) -> tuple[int, int]:
        """Return the real display size, falling back to a sane phone default.

        Deriving the size from UI-tree bounds is wrong: the tree only covers
        the content area, so the bottom of the last element sits well above the
        real bottom edge and every scroll distance comes out short.
        """
        response = self.get_window()
        data = response.data or {}
        width = data.get("width") or data.get("w")
        height = data.get("height") or data.get("h")
        try:
            if width and height and int(width) > 0 and int(height) > 0:
                return (int(width), int(height))
        except (TypeError, ValueError):
            pass
        return (1080, 2400)

    def tap(self, x: float, y: float) -> BridgeResponse:
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            raise BridgeConfigurationError(
                "tap x coordinate must be numeric"
            )

        if not isinstance(y, (int, float)) or isinstance(y, bool):
            raise BridgeConfigurationError(
                "tap y coordinate must be numeric"
            )

        return self.command(
            "tap",
            {
                "x": x,
                "y": y,
            },
        )

    def back(self) -> BridgeResponse:
        return self.command("back")

    def input_text(self, text: str) -> BridgeResponse:
        if not isinstance(text, str) or not text:
            raise BridgeConfigurationError(
                "input_text text must be a non-empty string"
            )

        if len(text) > 10_000:
            raise BridgeConfigurationError(
                "input_text text must not exceed 10000 characters"
            )

        return self.command("input_text", {"text": text})

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration_ms: int = 300,
    ) -> BridgeResponse:
        for name, value in (("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise BridgeConfigurationError(
                    f"swipe {name} must be numeric"
                )

        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
            raise BridgeConfigurationError(
                "swipe duration_ms must be an integer"
            )

        if duration_ms <= 0 or duration_ms > 60_000:
            raise BridgeConfigurationError(
                "swipe duration_ms must be between 1 and 60000"
            )

        return self.command(
            "swipe",
            {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "duration_ms": duration_ms,
            },
        )

    def long_press(
        self,
        x: float,
        y: float,
        duration_ms: int = 600,
    ) -> BridgeResponse:
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            raise BridgeConfigurationError(
                "long_press x coordinate must be numeric"
            )

        if not isinstance(y, (int, float)) or isinstance(y, bool):
            raise BridgeConfigurationError(
                "long_press y coordinate must be numeric"
            )

        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
            raise BridgeConfigurationError(
                "long_press duration_ms must be an integer"
            )

        if duration_ms < 100 or duration_ms > 10_000:
            raise BridgeConfigurationError(
                "long_press duration_ms must be between 100 and 10000"
            )

        return self.command(
            "long_press",
            {
                "x": x,
                "y": y,
                "duration_ms": duration_ms,
            },
        )

    def clear_text(self) -> BridgeResponse:
        return self.command("clear_text")

    def erase_text(self) -> BridgeResponse:
        return self.command("erase_text")

    def get_window(self) -> BridgeResponse:
        return self.command("get_window")

    def open_url(self, url: str) -> BridgeResponse:
        if not isinstance(url, str) or not url.strip():
            raise BridgeConfigurationError(
                "open_url url must be a non-empty string"
            )

        url = url.strip()

        if not (
            url.startswith("http://")
            or url.startswith("https://")
            or url.startswith("data:")
        ):
            url = "https://" + url

        if len(url) > 20_000:
            raise BridgeConfigurationError(
                "open_url url must not exceed 20000 characters"
            )

        return self.command("open_url", {"url": url})

    def key_event(self, keycode: str) -> BridgeResponse:
        if not isinstance(keycode, str) or not keycode.strip():
            raise BridgeConfigurationError(
                "key_event keycode must be a non-empty string"
            )

        keycode = keycode.strip()

        if len(keycode) > 64:
            raise BridgeConfigurationError(
                "key_event keycode must not exceed 64 characters"
            )

        return self.command("key_event", {"keycode": keycode})

    def launch_app(self, package: str) -> BridgeResponse:
        if not isinstance(package, str) or not package.strip():
            raise BridgeConfigurationError(
                "launch_app package must be a non-empty string"
            )

        package = package.strip()

        if len(package) > 256:
            raise BridgeConfigurationError(
                "launch_app package must not exceed 256 characters"
            )

        return self.command("launch_app", {"package": package})

    # -- multi-window / node-action API -------------------------------- #
    #
    # These commands address a *specific* window by package and drive it
    # through accessibility node actions (ACTION_CLICK,
    # ACTION_SCROLL_FORWARD, ...) rather than screen-coordinate gestures.
    # A dispatchGesture only reaches the focused window, but a node action
    # reaches any window the service can see, which is what lets an app be
    # driven while another one stays in the foreground.

    def list_windows(self) -> BridgeResponse:
        """List every interactive window currently visible to the service."""
        return self.command("list_windows")

    def window_packages(self) -> list[str]:
        """Return the packages of all visible windows (deduplicated)."""
        response = self.list_windows()
        data = response.data or {}
        seen: list[str] = []
        for window in data.get("windows", []):
            package = window.get("package_name")
            if isinstance(package, str) and package and package not in seen:
                seen.append(package)
        return seen

    def ui_dump_for_package(self, package: str) -> BridgeResponse:
        """Dump the UI tree of a specific package's visible window.

        Unlike ``ui_dump`` (which reads the focused window), this reads the
        window whose root package matches, so a background app's tree stays
        reachable while another app is in the foreground.
        """
        if not isinstance(package, str) or not package.strip():
            raise BridgeConfigurationError(
                "ui_dump_for_package requires a non-empty package name"
            )

        return self.command("ui_dump", {"package": package.strip()})

    def node_action(
        self,
        action: str,
        *,
        package: str | None = None,
        resource_id: str | None = None,
        text: str | None = None,
        content_description: str | None = None,
        class_name: str | None = None,
        match_index: int = 0,
        text_argument: str | None = None,
        node_path: list[int] | None = None,
    ) -> BridgeResponse:
        """Run an accessibility action on a node inside a specific window.

        The node is located either by ``node_path`` (the exact sequence of
        child indices from the window root, as exposed by
        ``node_paths_of``) or by a selector (any combination of
        ``resource_id``, ``text``, ``content_description`` and
        ``class_name``); all supplied selector fields must match, and
        ``match_index`` picks among several matches.

        Actions: ``click``, ``long_click``, ``scroll_forward``,
        ``scroll_backward``, ``set_text`` (needs ``text_argument``),
        ``focus``, ``clear_focus``, ``select``.
        """
        if not isinstance(action, str) or not action.strip():
            raise BridgeConfigurationError(
                "node_action requires a non-empty action"
            )

        action = action.strip()

        if len(action) > 64:
            raise BridgeConfigurationError(
                "node_action action must not exceed 64 characters"
            )

        if node_path is not None:
            if not isinstance(node_path, list) or not node_path:
                raise BridgeConfigurationError(
                    "node_action node_path must be a non-empty list of ints"
                )

            if not all(isinstance(i, int) and i >= 0 for i in node_path):
                raise BridgeConfigurationError(
                    "node_action node_path must be non-negative ints"
                )

        if not any(
            isinstance(v, str) and v.strip()
            for v in (
                resource_id,
                text,
                content_description,
                class_name,
            )
        ) and node_path is None:
            raise BridgeConfigurationError(
                "node_action needs a node_path or a selector field"
            )

        if match_index < 0 or not isinstance(match_index, int):
            raise BridgeConfigurationError(
                "node_action match_index must be a non-negative integer"
            )

        args: dict[str, Any] = {
            "action": action,
            "match_index": match_index,
        }

        if package:
            args["package"] = package.strip()
        if resource_id:
            args["resource_id"] = resource_id.strip()
        if text:
            args["text"] = text.strip()
        if content_description:
            args["content_description"] = content_description.strip()
        if class_name:
            args["class_name"] = class_name.strip()
        if text_argument:
            args["text_argument"] = text_argument
        if node_path is not None:
            args["node_path"] = [int(i) for i in node_path]

        return self.command("node_action", args)

    def node_click(
        self,
        package: str,
        *,
        resource_id: str | None = None,
        text: str | None = None,
        content_description: str | None = None,
        class_name: str | None = None,
        match_index: int = 0,
    ) -> BridgeResponse:
        return self.node_action(
            "click",
            package=package,
            resource_id=resource_id,
            text=text,
            content_description=content_description,
            class_name=class_name,
            match_index=match_index,
        )

    def node_scroll(
        self,
        package: str,
        direction: str = "forward",
        *,
        resource_id: str | None = None,
        text: str | None = None,
        content_description: str | None = None,
        class_name: str | None = None,
        match_index: int = 0,
        node_path: list[int] | None = None,
    ) -> BridgeResponse:
        if direction not in ("forward", "backward"):
            raise BridgeConfigurationError(
                "node_scroll direction must be forward or backward"
            )

        return self.node_action(
            f"scroll_{direction}",
            package=package,
            resource_id=resource_id,
            text=text,
            content_description=content_description,
            class_name=class_name,
            match_index=match_index,
            node_path=node_path,
        )

    def scrollable_node_paths(
        self,
        package: str,
        *,
        limit: int = 8,
    ) -> list[list[int]]:
        """Return node paths of scrollable containers in a package window.

        Each entry is the child-index path from the window root, suitable
        for ``node_scroll(node_path=...)``. Ordering is breadth-first, so
        the outermost (usually the main feed) container comes first.
        """
        response = self.ui_dump_for_package(package)
        data = response.data or {}
        root = data.get("root")

        if not isinstance(root, Mapping):
            return []

        paths: list[list[int]] = []

        def visit(node: Mapping[str, Any], path: list[int]) -> None:
            if len(paths) >= limit:
                return

            if node.get("scrollable") is True:
                paths.append(list(path))

            for index, child in enumerate(node.get("children") or ()):
                if isinstance(child, Mapping):
                    visit(child, path + [index])

        if isinstance(root, Mapping):
            visit(root, [])

        return paths

    def window_feed_paths(
        self,
        package: str,
        *,
        limit: int = 8,
    ) -> list[list[int]]:
        """Return node paths of the vertical scroll containers of a window.

        Instagram's surfaces are paged: the Reels tab is a vertical
        ``clips_viewer_view_pager`` and the home feed is a vertical
        ``RecyclerView``. Horizontal carousels (``android:id/list`` inside a
        tab strip) and the search grid are not the feed, so they are
        excluded. Preference order:

        1. ``clips_viewer_view_pager`` (Reels pager, scrolls one clip)
        2. the home-feed ``RecyclerView``
        3. any other vertical ``RecyclerView``
        """
        response = self.ui_dump_for_package(package)
        data = response.data or {}
        root = data.get("root")

        if not isinstance(root, Mapping):
            return []

        clips_pagers: list[list[int]] = []
        feed_lists: list[list[int]] = []
        other_lists: list[list[int]] = []

        def visit(node: Mapping[str, Any], path: list[int]) -> None:
            if not node.get("scrollable"):
                for index, child in enumerate(node.get("children") or ()):
                    if isinstance(child, Mapping):
                        visit(child, path + [index])
                return

            resource_id = node.get("view_id_resource_name") or ""
            class_name = node.get("class_name") or ""
            is_recycler = (
                isinstance(class_name, str) and "RecyclerView" in class_name
            )

            if isinstance(resource_id, str) and "clips_viewer_view_pager" in resource_id:
                clips_pagers.append(list(path))
            elif isinstance(resource_id, str) and resource_id.endswith(":id/recycler_view"):
                feed_lists.append(list(path))
            elif is_recycler and resource_id != "android:id/list":
                other_lists.append(list(path))

            if (
                len(clips_pagers) + len(feed_lists) + len(other_lists)
                >= limit
            ):
                return

            for index, child in enumerate(node.get("children") or ()):
                if isinstance(child, Mapping):
                    visit(child, path + [index])

        visit(root, [])

        return clips_pagers + feed_lists + other_lists

    @staticmethod
    def _encode_json(payload: Mapping[str, Any]) -> bytes:
        try:
            text = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise BridgeConfigurationError(
                f"Unable to encode request JSON: {exc}"
            ) from exc

        return text.encode("utf-8")

    def _post(self, body: bytes) -> bytes:
        request = self._build_http_request(body)

        try:
            with socket.create_connection(
                (self._host, self._port),
                timeout=self._connect_timeout,
            ) as sock:
                sock.settimeout(self._read_timeout)

                self._send_all(sock, request)

                return self._read_http_response(sock)

        except socket.timeout as exc:
            raise BridgeTimeoutError(
                "Timed out while communicating with AndroidAgentPro Bridge"
            ) from exc
        except ConnectionRefusedError as exc:
            raise BridgeConnectionError(
                "AndroidAgentPro Bridge refused the connection. "
                "Ensure the app is running and Accessibility Service is enabled."
            ) from exc
        except OSError as exc:
            raise BridgeConnectionError(
                f"Unable to connect to AndroidAgentPro Bridge: {exc}"
            ) from exc

    def _build_http_request(self, body: bytes) -> bytes:
        token_bytes = self._token.encode("ascii", errors="strict")

        headers = (
            f"POST {PATH} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            f"Authorization: Bearer {token_bytes.decode('ascii')}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")

        return headers + body

    @staticmethod
    def _send_all(sock: socket.socket, data: bytes) -> None:
        view = memoryview(data)
        sent = 0

        while sent < len(view):
            try:
                count = sock.send(view[sent:])
            except socket.timeout as exc:
                raise BridgeTimeoutError(
                    "Timed out while sending request to Bridge"
                ) from exc
            except OSError as exc:
                raise BridgeConnectionError(
                    f"Failed while sending request to Bridge: {exc}"
                ) from exc

            if count <= 0:
                raise BridgeConnectionError(
                    "Bridge connection closed while sending request"
                )

            sent += count

    @staticmethod
    def _read_http_response(sock: socket.socket) -> bytes:
        buffer = bytearray()
        header_end = -1

        while header_end < 0:
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise BridgeTimeoutError(
                    "Timed out while reading Bridge HTTP headers"
                ) from exc
            except OSError as exc:
                raise BridgeConnectionError(
                    f"Failed while reading Bridge response: {exc}"
                ) from exc

            if not chunk:
                raise BridgeProtocolError(
                    "Bridge closed the connection before sending HTTP headers"
                )

            buffer.extend(chunk)

            if len(buffer) > MAX_RESPONSE_BYTES:
                raise BridgeProtocolError(
                    "Bridge response exceeds maximum allowed size"
                )

            header_end = buffer.find(b"\r\n\r\n")

        header_bytes = bytes(buffer[:header_end])
        body_buffer = bytearray(buffer[header_end + 4:])

        try:
            header_text = header_bytes.decode("iso-8859-1")
        except UnicodeDecodeError as exc:
            raise BridgeProtocolError(
                "Bridge HTTP headers are invalid"
            ) from exc

        lines = header_text.split("\r\n")

        if not lines or not lines[0].startswith("HTTP/1.1 "):
            raise BridgeProtocolError(
                "Invalid HTTP status line from Bridge"
            )

        status_parts = lines[0].split(" ", 2)

        if len(status_parts) < 2:
            raise BridgeProtocolError(
                "Invalid HTTP status line from Bridge"
            )

        try:
            status_code = int(status_parts[1])
        except ValueError as exc:
            raise BridgeProtocolError(
                "Invalid HTTP status code from Bridge"
            ) from exc

        content_length: int | None = None

        for line in lines[1:]:
            if not line:
                continue

            if ":" not in line:
                raise BridgeProtocolError(
                    "Malformed HTTP header from Bridge"
                )

            name, value = line.split(":", 1)

            if name.strip().lower() == "content-length":
                try:
                    content_length = int(value.strip())
                except ValueError as exc:
                    raise BridgeProtocolError(
                        "Invalid Content-Length from Bridge"
                    ) from exc

                if content_length < 0:
                    raise BridgeProtocolError(
                        "Negative Content-Length from Bridge"
                    )

                if content_length > MAX_RESPONSE_BYTES:
                    raise BridgeProtocolError(
                        "Bridge response exceeds maximum allowed size"
                    )

        if content_length is None:
            raise BridgeProtocolError(
                "Bridge response does not contain Content-Length"
            )

        while len(body_buffer) < content_length:
            try:
                chunk = sock.recv(
                    min(4096, content_length - len(body_buffer))
                )
            except socket.timeout as exc:
                raise BridgeTimeoutError(
                    "Timed out while reading Bridge response body"
                ) from exc
            except OSError as exc:
                raise BridgeConnectionError(
                    f"Failed while reading Bridge response body: {exc}"
                ) from exc

            if not chunk:
                raise BridgeProtocolError(
                    "Bridge closed connection before full response body"
                )

            body_buffer.extend(chunk)

            if len(body_buffer) > MAX_RESPONSE_BYTES:
                raise BridgeProtocolError(
                    "Bridge response exceeds maximum allowed size"
                )

        response_body = bytes(body_buffer[:content_length])

        if status_code < 200 or status_code >= 300:
            try:
                payload = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise BridgeRemoteError(
                    f"Bridge returned HTTP {status_code}"
                )

            try:
                response = BridgeResponse.from_json(payload)
            except BridgeProtocolError:
                raise BridgeRemoteError(
                    f"Bridge returned HTTP {status_code}"
                )

            if response.error_code and response.error_message:
                raise BridgeRemoteError(
                    f"{response.error_code}: {response.error_message}",
                    error_code=response.error_code,
                    error_message=response.error_message,
                )

            raise BridgeRemoteError(
                f"Bridge returned HTTP {status_code}"
            )

        return response_body


def main() -> int:
    client = BridgeClient()

    started = time.monotonic()

    try:
        response = client.health()
    except BridgeClientError as exc:
        print(f"BRIDGE_ERROR: {exc}")
        return 1

    elapsed_ms = (time.monotonic() - started) * 1000.0

    print("BRIDGE_OK")
    print(f"REQUEST_ID={response.request_id}")
    print(f"LATENCY_MS={elapsed_ms:.1f}")
    print(
        "DATA="
        + json.dumps(
            response.data or {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
