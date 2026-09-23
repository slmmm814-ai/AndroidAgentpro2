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

CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 10.0
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
    """Raised when the Bridge rejects a valid request."""


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
                    f"{response.error_code}: {response.error_message}"
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
