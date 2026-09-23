from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from html import unescape
from typing import Protocol


class WebResearchError(Exception):
    """Raised for all controlled web-research boundary failures."""


class WebTrust(str, Enum):
    UNTRUSTED = "UNTRUSTED"


@dataclass(frozen=True)
class WebResearchPolicy:
    connect_timeout: float = 5.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_content_chars: int = 1_000_000
    max_url_length: int = 2048
    user_agent: str = "AndroidAgentPro-WebResearch/1.0"

    def __post_init__(self) -> None:
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be greater than zero")
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be greater than zero")
        if self.max_content_chars <= 0:
            raise ValueError("max_content_chars must be greater than zero")
        if self.max_url_length <= 0:
            raise ValueError("max_url_length must be greater than zero")
        if not isinstance(self.user_agent, str) or not self.user_agent.strip():
            raise ValueError("user_agent must be a non-empty string")


@dataclass(frozen=True)
class WebContent:
    normalized_text: str
    document_id: str
    source_url: str
    trust: WebTrust = WebTrust.UNTRUSTED

    def __post_init__(self) -> None:
        if not isinstance(self.normalized_text, str):
            raise TypeError("normalized_text must be a string")
        if not isinstance(self.document_id, str) or not self.document_id:
            raise ValueError("document_id must be a non-empty string")
        if not isinstance(self.source_url, str) or not self.source_url:
            raise ValueError("source_url must be a non-empty string")
        if self.trust is not WebTrust.UNTRUSTED:
            raise ValueError("web content must be untrusted")


@dataclass(frozen=True)
class ResearchItem:
    extracted_text: str
    source_url: str
    source_document_id: str
    trust: WebTrust = WebTrust.UNTRUSTED

    def __post_init__(self) -> None:
        if not isinstance(self.extracted_text, str) or not self.extracted_text:
            raise ValueError("extracted_text must be a non-empty string")
        if not isinstance(self.source_url, str) or not self.source_url:
            raise ValueError("source_url must be a non-empty string")
        if not isinstance(self.source_document_id, str) or not self.source_document_id:
            raise ValueError("source_document_id must be a non-empty string")
        if self.trust is not WebTrust.UNTRUSTED:
            raise ValueError("research items must remain untrusted")


@dataclass(frozen=True)
class WebDocument:
    url: str
    retrieved_at: float
    content: WebContent
    document_id: str
    trust: WebTrust = WebTrust.UNTRUSTED
    owner_confirmation: bool = False
    action: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("url must be a non-empty string")
        if not isinstance(self.retrieved_at, (int, float)):
            raise TypeError("retrieved_at must be numeric")
        if not isinstance(self.document_id, str) or not self.document_id:
            raise ValueError("document_id must be a non-empty string")
        if self.trust is not WebTrust.UNTRUSTED:
            raise ValueError("web document must be untrusted")
        if self.owner_confirmation:
            raise ValueError("web documents cannot grant owner confirmation")
        if self.action is not None:
            raise ValueError("web documents cannot create executable actions")
        if self.content.document_id != self.document_id:
            raise ValueError("content document_id must match document document_id")
        if self.content.source_url != self.url:
            raise ValueError("content source_url must match document url")
        if self.content.trust is not WebTrust.UNTRUSTED:
            raise ValueError("content must remain untrusted")


class WebTransport(Protocol):
    def fetch(
        self,
        url: str,
        timeout: float,
        max_bytes: int,
    ) -> tuple[int, str, bytes]:
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject HTTP redirects so the validated origin cannot change implicitly."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp,
        code: int,
        msg: str,
        headers,
        newurl: str,
    ):
        raise WebResearchError(
            f"HTTP redirect rejected: {code}"
        )


class UrllibTransport:
    """HTTP(S) transport with bounded reads and deterministic response handling."""

    def __init__(self, user_agent: str) -> None:
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("user_agent must be a non-empty string")
        self._user_agent = user_agent
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def fetch(
        self,
        url: str,
        timeout: float,
        max_bytes: int,
    ) -> tuple[int, str, bytes]:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self._user_agent},
            method="GET",
        )

        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                content_type = response.headers.get("Content-Type", "")
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise WebResearchError(
                f"HTTP request failed with status {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise WebResearchError(f"network failure: {exc.reason}") from exc
        except TimeoutError as exc:
            raise WebResearchError("web request timed out") from exc
        except OSError as exc:
            raise WebResearchError(f"network failure: {exc}") from exc

        if len(body) > max_bytes:
            raise WebResearchError("response exceeds configured size limit")

        return status, content_type, body


class WebResearchService:
    """Fetches web documents while preserving a strict untrusted-data boundary."""

    _SCRIPT_STYLE_RE = re.compile(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _TAG_RE = re.compile(r"<[^>]+>", flags=re.DOTALL)
    _WHITESPACE_RE = re.compile(r"\s+")

    def __init__(
        self,
        transport: WebTransport | None = None,
        policy: WebResearchPolicy | None = None,
    ) -> None:
        actual_policy = policy or WebResearchPolicy()
        self._policy = actual_policy
        self._transport = transport or UrllibTransport(actual_policy.user_agent)

    def fetch(self, url: str) -> WebDocument:
        normalized_url = self._validate_url(url)

        try:
            status, content_type, body = self._transport.fetch(
                normalized_url,
                self._policy.connect_timeout,
                self._policy.max_response_bytes,
            )
        except WebResearchError:
            raise
        except (TimeoutError, OSError) as exc:
            raise WebResearchError(f"network failure: {exc}") from exc
        except Exception as exc:
            raise WebResearchError(f"web transport failure: {exc}") from exc

        if not isinstance(status, int) or isinstance(status, bool):
            raise WebResearchError("invalid transport status")
        if not isinstance(content_type, str):
            raise WebResearchError("invalid transport content type")
        if not isinstance(body, bytes):
            raise WebResearchError("invalid transport response body")

        if not 200 <= status < 300:
            raise WebResearchError(f"HTTP request failed with status {status}")

        if len(body) > self._policy.max_response_bytes:
            raise WebResearchError("response exceeds configured size limit")

        text = self._decode_body(body)
        normalized_text = self._normalize_content(text)

        if len(normalized_text) > self._policy.max_content_chars:
            raise WebResearchError("normalized content exceeds configured size limit")

        document_id = hashlib.sha256(
            normalized_url.encode("utf-8")
            + b"\0"
            + body
        ).hexdigest()

        content = WebContent(
            normalized_text=normalized_text,
            document_id=document_id,
            source_url=normalized_url,
            trust=WebTrust.UNTRUSTED,
        )

        return WebDocument(
            url=normalized_url,
            retrieved_at=time.time(),
            content=content,
            document_id=document_id,
            trust=WebTrust.UNTRUSTED,
            owner_confirmation=False,
            action=None,
        )

    def _validate_url(self, url: str) -> str:
        if not isinstance(url, str):
            raise WebResearchError("URL must be a string")

        candidate = url.strip()
        if not candidate:
            raise WebResearchError("URL must not be empty")

        if len(candidate) > self._policy.max_url_length:
            raise WebResearchError("URL exceeds configured length limit")

        try:
            parsed = urllib.parse.urlsplit(candidate)
        except ValueError as exc:
            raise WebResearchError("invalid URL") from exc

        if parsed.scheme.lower() not in {"http", "https"}:
            raise WebResearchError("only HTTP(S) URLs are supported")

        if not parsed.hostname:
            raise WebResearchError("URL must contain a hostname")

        if parsed.username is not None or parsed.password is not None:
            raise WebResearchError("URL credentials are not permitted")

        hostname = parsed.hostname.rstrip(".").lower()

        if not hostname:
            raise WebResearchError("URL hostname must not be empty")

        self._reject_private_or_loopback_host(hostname)

        try:
            port = parsed.port
        except ValueError as exc:
            raise WebResearchError("invalid URL port") from exc

        if port is not None and not 1 <= port <= 65535:
            raise WebResearchError("invalid URL port")

        normalized = urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )

        return normalized

    @staticmethod
    def _reject_private_or_loopback_host(hostname: str) -> None:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None

        if address is not None:
            if (
                address.is_loopback
                or address.is_private
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise WebResearchError(
                    "loopback, private, link-local, reserved, multicast, "
                    "and unspecified addresses are not permitted"
                )
            return

        lowered = hostname.lower().rstrip(".")
        if lowered in {"localhost", "localhost.localdomain"}:
            raise WebResearchError("loopback hostname is not permitted")

        try:
            resolved = socket.getaddrinfo(
                lowered,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            return

        for result in resolved:
            sockaddr = result[4]
            if not sockaddr:
                continue
            resolved_host = sockaddr[0]
            try:
                address = ipaddress.ip_address(resolved_host)
            except ValueError:
                continue

            if (
                address.is_loopback
                or address.is_private
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise WebResearchError(
                    "hostname resolves to a non-public network address"
                )

    def extract_research_item(self, document: WebDocument) -> ResearchItem:
        if not isinstance(document, WebDocument):
            raise WebResearchError("invalid web document")
        if document.trust is not WebTrust.UNTRUSTED:
            raise WebResearchError("web document trust must remain untrusted")
        if document.content.trust is not WebTrust.UNTRUSTED:
            raise WebResearchError("web content trust must remain untrusted")
        if document.content.document_id != document.document_id:
            raise WebResearchError("invalid document provenance")
        if document.content.source_url != document.url:
            raise WebResearchError("invalid content provenance")
        if not document.content.normalized_text:
            raise WebResearchError("web document contains no research text")
        return ResearchItem(
            extracted_text=document.content.normalized_text,
            source_url=document.url,
            source_document_id=document.document_id,
            trust=WebTrust.UNTRUSTED,
        )

    @staticmethod
    def _decode_body(body: bytes) -> str:
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return body.decode("utf-8", errors="replace")
            except Exception as exc:
                raise WebResearchError("unable to decode response") from exc

    def _normalize_content(self, text: str) -> str:
        if not isinstance(text, str):
            raise WebResearchError("response content must be text")

        without_scripts = self._SCRIPT_STYLE_RE.sub(" ", text)
        without_tags = self._TAG_RE.sub(" ", without_scripts)
        unescaped = unescape(without_tags)
        normalized = self._WHITESPACE_RE.sub(" ", unescaped).strip()

        if len(normalized) > self._policy.max_content_chars:
            raise WebResearchError(
                "normalized content exceeds configured size limit"
            )

        return normalized
