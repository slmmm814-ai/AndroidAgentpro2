from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .models import AgentAction


LOGGER = logging.getLogger(__name__)


class AuthorizationError(ValueError):
    """Raised when an authorization object is structurally invalid."""


class AuthorizationDecision(str, Enum):
    """Fail-closed authorization decisions."""

    GRANTED = "GRANTED"
    DENIED = "DENIED"
    NEED_CONFIRMATION = "NEED_CONFIRMATION"


class DangerousActionPolicy:
    """Deterministic classification of actions requiring owner authorization."""

    _DANGEROUS_ACTION_VALUES = frozenset({"install_apk"})

    @classmethod
    def is_dangerous(cls, action: AgentAction) -> bool:
        if not isinstance(action, AgentAction):
            raise TypeError("action must be an AgentAction")
        return action.action_type.value in cls._DANGEROUS_ACTION_VALUES

    @classmethod
    def requires_owner_confirmation(cls, action: AgentAction) -> bool:
        if not isinstance(action, AgentAction):
            raise TypeError("action must be an AgentAction")
        return action.requires_confirmation or cls.is_dangerous(action)


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, bool):
            return value
        if isinstance(value, float):
            if value != value or value in {float("inf"), float("-inf")}:
                raise AuthorizationError("non-finite numeric values are not allowed")
        return value

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuthorizationError("authorization mappings require string keys")
            normalized[key] = _canonicalize(item)
        return {
            key: normalized[key]
            for key in sorted(normalized)
        }

    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]

    raise AuthorizationError(
        f"unsupported value type in authorization payload: {type(value).__name__}"
    )


def action_fingerprint(action: AgentAction) -> str:
    """Return a stable SHA-256 fingerprint for the exact typed action."""

    if not isinstance(action, AgentAction):
        raise TypeError("action must be an AgentAction")

    canonical = {
        "action_type": action.action_type.value,
        "arguments": _canonicalize(action.arguments),
        "requires_confirmation": action.requires_confirmation,
    }

    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ConfirmationRequest:
    """Owner-confirmation request bound to one exact action."""

    request_id: str
    action_fingerprint: str
    action_type: str
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise AuthorizationError("request_id must be a non-empty string")

        if not isinstance(self.action_fingerprint, str) or len(self.action_fingerprint) != 64:
            raise AuthorizationError("action_fingerprint must be a SHA-256 hex digest")

        try:
            int(self.action_fingerprint, 16)
        except ValueError as exc:
            raise AuthorizationError(
                "action_fingerprint must contain hexadecimal characters"
            ) from exc

        if not isinstance(self.action_type, str) or not self.action_type.strip():
            raise AuthorizationError("action_type must be a non-empty string")

        if isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float)):
            raise AuthorizationError("created_at must be numeric")

        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float)):
            raise AuthorizationError("expires_at must be numeric")

        if self.expires_at <= self.created_at:
            raise AuthorizationError("expires_at must be greater than created_at")


@dataclass(frozen=True)
class OwnerConfirmation:
    """Explicit owner authorization bound to one confirmation request."""

    request_id: str
    action_fingerprint: str
    granted: bool
    decided_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise AuthorizationError("request_id must be a non-empty string")

        if not isinstance(self.action_fingerprint, str) or len(self.action_fingerprint) != 64:
            raise AuthorizationError("action_fingerprint must be a SHA-256 hex digest")

        try:
            int(self.action_fingerprint, 16)
        except ValueError as exc:
            raise AuthorizationError(
                "action_fingerprint must contain hexadecimal characters"
            ) from exc

        if not isinstance(self.granted, bool):
            raise AuthorizationError("granted must be bool")

        if isinstance(self.decided_at, bool) or not isinstance(self.decided_at, (int, float)):
            raise AuthorizationError("decided_at must be numeric")


@dataclass(frozen=True)
class AuthorizationResult:
    """Result of validating owner authorization for an exact action."""

    decision: AuthorizationDecision
    reason: str


class AuthorizationService:
    """Fail-closed owner authorization boundary for executable actions."""

    def __init__(
        self,
        *,
        confirmation_ttl_seconds: float = 300.0,
        clock: Any = time.time,
    ) -> None:
        if isinstance(confirmation_ttl_seconds, bool) or not isinstance(
            confirmation_ttl_seconds,
            (int, float),
        ):
            raise TypeError("confirmation_ttl_seconds must be numeric")

        if confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation_ttl_seconds must be greater than zero")

        if not callable(clock):
            raise TypeError("clock must be callable")

        self._confirmation_ttl_seconds = float(confirmation_ttl_seconds)
        self._clock = clock
        self._confirmation_requests: dict[str, ConfirmationRequest] = {}
        self._issued_confirmations: dict[str, OwnerConfirmation] = {}

    def create_confirmation_request(
        self,
        action: AgentAction,
    ) -> ConfirmationRequest:
        if not isinstance(action, AgentAction):
            raise TypeError("action must be an AgentAction")

        if not DangerousActionPolicy.requires_owner_confirmation(action):
            raise AuthorizationError(
                "confirmation request is not required for this action"
            )

        now = float(self._clock())
        if not now == now or now in {float("inf"), float("-inf")}:
            raise AuthorizationError("clock returned a non-finite value")

        request = ConfirmationRequest(
            request_id=secrets.token_hex(16),
            action_fingerprint=action_fingerprint(action),
            action_type=action.action_type.value,
            created_at=now,
            expires_at=now + self._confirmation_ttl_seconds,
        )

        self._confirmation_requests[request.request_id] = request
        LOGGER.info(
            "created owner confirmation request id=%s action_type=%s",
            request.request_id,
            request.action_type,
        )
        return request

    def get_confirmation_request(
        self,
        request_id: str,
    ) -> ConfirmationRequest | None:
        if not isinstance(request_id, str):
            raise TypeError("request_id must be a string")
        if not request_id:
            raise ValueError("request_id must not be empty")
        return self._confirmation_requests.get(request_id)

    def grant(
        self,
        request: ConfirmationRequest,
        action: AgentAction,
        *,
        granted: bool,
    ) -> OwnerConfirmation:
        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request must be a ConfirmationRequest")

        if not isinstance(action, AgentAction):
            raise TypeError("action must be an AgentAction")

        if not isinstance(granted, bool):
            raise TypeError("granted must be bool")

        current_fingerprint = action_fingerprint(action)

        if current_fingerprint != request.action_fingerprint:
            raise AuthorizationError(
                "confirmation cannot be granted for a different action"
            )

        if action.action_type.value != request.action_type:
            raise AuthorizationError(
                "confirmation request action type does not match the action"
            )

        now = float(self._clock())
        if now > request.expires_at:
            raise AuthorizationError("confirmation request has expired")

        confirmation = OwnerConfirmation(
            request_id=request.request_id,
            action_fingerprint=current_fingerprint,
            granted=granted,
            decided_at=now,
        )

        self._issued_confirmations[confirmation.request_id] = confirmation
        LOGGER.info(
            "owner confirmation decision recorded id=%s granted=%s",
            request.request_id,
            granted,
        )
        return confirmation

    def authorize(
        self,
        action: AgentAction,
        confirmation: OwnerConfirmation | None,
    ) -> AuthorizationResult:
        if not isinstance(action, AgentAction):
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "invalid action",
            )

        if not DangerousActionPolicy.requires_owner_confirmation(action):
            return AuthorizationResult(
                AuthorizationDecision.GRANTED,
                "action does not require owner confirmation",
            )

        if confirmation is None:
            return AuthorizationResult(
                AuthorizationDecision.NEED_CONFIRMATION,
                "explicit owner confirmation is required",
            )

        if not isinstance(confirmation, OwnerConfirmation):
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation object is invalid",
            )

        expected_fingerprint = action_fingerprint(action)
        request = self._confirmation_requests.get(confirmation.request_id)
        if request is None:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation request is unknown",
            )

        if request.action_type != action.action_type.value:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation request does not match the action type",
            )

        if request.action_fingerprint != expected_fingerprint:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation request does not match the current action",
            )

        if confirmation.action_fingerprint != expected_fingerprint:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation does not match the current action",
            )

        issued_confirmation = self._issued_confirmations.get(confirmation.request_id)
        if issued_confirmation is None or issued_confirmation != confirmation:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation was not issued by this authorization service",
            )

        now = float(self._clock())
        if not now == now or now in {float("inf"), float("-inf")}:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "clock returned a non-finite value",
            )

        if now > request.expires_at:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation request has expired",
            )

        if confirmation.decided_at < request.created_at:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation decision predates the request",
            )

        if confirmation.decided_at > request.expires_at:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation decision occurred after request expiry",
            )

        if confirmation.decided_at > now:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "confirmation decision is from the future",
            )

        if not confirmation.granted:
            return AuthorizationResult(
                AuthorizationDecision.DENIED,
                "owner denied the action",
            )

        return AuthorizationResult(
            AuthorizationDecision.GRANTED,
            "owner confirmation matches the exact action",
        )
