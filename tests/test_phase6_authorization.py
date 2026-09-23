from __future__ import annotations

import unittest

from agentpro.authorization import (
    AuthorizationDecision,
    AuthorizationError,
    AuthorizationService,
    DangerousActionPolicy,
    OwnerConfirmation,
    action_fingerprint,
)
from agentpro.models import ActionType, AgentAction


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_action(
    action_type: ActionType = ActionType.TAP,
    *,
    arguments: dict[str, object] | None = None,
    requires_confirmation: bool = False,
) -> AgentAction:
    return AgentAction(
        action_type=action_type,
        arguments=arguments or {"x": 10, "y": 20},
        requires_confirmation=requires_confirmation,
    )


class AuthorizationClassificationTests(unittest.TestCase):
    def test_install_apk_is_classified_as_dangerous_by_typed_value(self) -> None:
        action = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/test.apk"},
        )

        self.assertTrue(DangerousActionPolicy.is_dangerous(action))
        self.assertTrue(DangerousActionPolicy.requires_owner_confirmation(action))

    def test_normal_action_without_confirmation_is_not_dangerous(self) -> None:
        action = make_action()

        self.assertFalse(DangerousActionPolicy.is_dangerous(action))
        self.assertFalse(DangerousActionPolicy.requires_owner_confirmation(action))

    def test_explicit_confirmation_requirement_is_preserved(self) -> None:
        action = make_action(requires_confirmation=True)

        self.assertFalse(DangerousActionPolicy.is_dangerous(action))
        self.assertTrue(DangerousActionPolicy.requires_owner_confirmation(action))


class AuthorizationFingerprintTests(unittest.TestCase):
    def test_same_action_has_same_fingerprint(self) -> None:
        first = make_action(
            action_type=ActionType.TAP,
            arguments={"x": 10, "y": 20},
        )
        second = make_action(
            action_type=ActionType.TAP,
            arguments={"y": 20, "x": 10},
        )

        self.assertEqual(action_fingerprint(first), action_fingerprint(second))

    def test_different_arguments_have_different_fingerprints(self) -> None:
        first = make_action(arguments={"x": 10, "y": 20})
        second = make_action(arguments={"x": 11, "y": 20})

        self.assertNotEqual(action_fingerprint(first), action_fingerprint(second))

    def test_confirmation_requirement_changes_fingerprint(self) -> None:
        first = make_action(requires_confirmation=False)
        second = make_action(requires_confirmation=True)

        self.assertNotEqual(action_fingerprint(first), action_fingerprint(second))


class AuthorizationRequestTests(unittest.TestCase):
    def test_request_created_only_for_confirmation_required_action(self) -> None:
        clock = FakeClock()
        service = AuthorizationService(clock=clock)

        action = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/test.apk"},
        )

        request = service.create_confirmation_request(action)

        self.assertEqual(request.action_type, "install_apk")
        self.assertEqual(request.action_fingerprint, action_fingerprint(action))
        self.assertEqual(request.created_at, 1000.0)
        self.assertEqual(request.expires_at, 1300.0)
        self.assertTrue(request.request_id)

    def test_request_rejected_for_safe_action(self) -> None:
        service = AuthorizationService(clock=FakeClock())
        action = make_action()

        with self.assertRaises(AuthorizationError):
            service.create_confirmation_request(action)


class AuthorizationDecisionTests(unittest.TestCase):
    def test_missing_confirmation_requires_confirmation(self) -> None:
        service = AuthorizationService(clock=FakeClock())
        action = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/test.apk"},
        )

        result = service.authorize(action, None)

        self.assertEqual(
            result.decision,
            AuthorizationDecision.NEED_CONFIRMATION,
        )

    def test_valid_owner_confirmation_grants_exact_action(self) -> None:
        clock = FakeClock()
        service = AuthorizationService(clock=clock)
        action = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/test.apk"},
        )

        request = service.create_confirmation_request(action)
        confirmation = service.grant(
            request,
            action,
            granted=True,
        )

        result = service.authorize(action, confirmation)

        self.assertEqual(result.decision, AuthorizationDecision.GRANTED)

    def test_owner_denial_is_denied(self) -> None:
        clock = FakeClock()
        service = AuthorizationService(clock=clock)
        action = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/test.apk"},
        )

        request = service.create_confirmation_request(action)
        confirmation = service.grant(
            request,
            action,
            granted=False,
        )

        result = service.authorize(action, confirmation)

        self.assertEqual(result.decision, AuthorizationDecision.DENIED)

    def test_different_action_cannot_use_confirmation(self) -> None:
        clock = FakeClock()
        service = AuthorizationService(clock=clock)

        first = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/first.apk"},
        )
        second = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/second.apk"},
        )

        request = service.create_confirmation_request(first)
        confirmation = service.grant(
            request,
            first,
            granted=True,
        )

        result = service.authorize(second, confirmation)

        self.assertEqual(result.decision, AuthorizationDecision.DENIED)

    def test_different_action_cannot_receive_grant_for_existing_request(self) -> None:
        clock = FakeClock()
        service = AuthorizationService(clock=clock)

        first = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/first.apk"},
        )
        second = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/second.apk"},
        )

        request = service.create_confirmation_request(first)

        with self.assertRaises(AuthorizationError):
            service.grant(
                request,
                second,
                granted=True,
            )

    def test_expired_request_cannot_be_granted(self) -> None:
        clock = FakeClock()
        service = AuthorizationService(
            clock=clock,
            confirmation_ttl_seconds=10,
        )

        action = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/test.apk"},
        )

        request = service.create_confirmation_request(action)
        clock.value = 1011.0

        with self.assertRaises(AuthorizationError):
            service.grant(
                request,
                action,
                granted=True,
            )

    def test_safe_action_is_granted_without_confirmation(self) -> None:
        service = AuthorizationService(clock=FakeClock())
        action = make_action()

        result = service.authorize(action, None)

        self.assertEqual(result.decision, AuthorizationDecision.GRANTED)


class AuthorizationValidationTests(unittest.TestCase):
    def test_malformed_confirmation_is_denied_fail_closed(self) -> None:
        service = AuthorizationService(clock=FakeClock())
        action = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/test.apk"},
        )

        result = service.authorize(
            action,
            object(),  # type: ignore[arg-type]
        )

        self.assertEqual(result.decision, AuthorizationDecision.DENIED)

    def test_forged_confirmation_for_different_fingerprint_is_denied(self) -> None:
        service = AuthorizationService(clock=FakeClock())
        action = make_action(
            action_type=ActionType("install_apk"),
            arguments={"path": "/sdcard/Download/test.apk"},
        )

        forged = OwnerConfirmation(
            request_id="forged-request",
            action_fingerprint="0" * 64,
            granted=True,
            decided_at=1000.0,
        )

        result = service.authorize(action, forged)

        self.assertEqual(result.decision, AuthorizationDecision.DENIED)


if __name__ == "__main__":
    unittest.main()
