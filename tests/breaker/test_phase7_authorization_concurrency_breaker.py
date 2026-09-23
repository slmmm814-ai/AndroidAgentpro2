import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from agentpro.authorization import (
    AuthorizationDecision,
    AuthorizationService,
)
from agentpro.models import ActionType, AgentAction


class TestPhase7AuthorizationConcurrencyBreaker(unittest.TestCase):
    def _action(self, index: int) -> AgentAction:
        return AgentAction(
            action_type=ActionType.TAP,
            arguments={"x": index, "y": index + 1},
            requires_confirmation=True,
        )

    def test_concurrent_confirmation_lifecycle_remains_consistent(self) -> None:
        service = AuthorizationService(confirmation_ttl_seconds=60.0)
        barrier = threading.Barrier(32)

        def worker(index: int) -> AuthorizationDecision:
            action = self._action(index)
            barrier.wait(timeout=5.0)

            request = service.create_confirmation_request(action)
            confirmation = service.grant(
                request,
                action,
                granted=True,
            )

            result = service.authorize(action, confirmation)
            return result.decision

        with ThreadPoolExecutor(max_workers=32) as executor:
            results = list(executor.map(worker, range(32)))

        self.assertEqual(
            results,
            [AuthorizationDecision.GRANTED] * 32,
        )

    def test_concurrent_requests_remain_distinct(self) -> None:
        service = AuthorizationService(confirmation_ttl_seconds=60.0)
        barrier = threading.Barrier(32)

        def worker(index: int) -> str:
            barrier.wait(timeout=5.0)
            request = service.create_confirmation_request(self._action(index))
            return request.request_id

        with ThreadPoolExecutor(max_workers=32) as executor:
            request_ids = list(executor.map(worker, range(32)))

        self.assertEqual(len(request_ids), 32)
        self.assertEqual(len(set(request_ids)), 32)

        for request_id in request_ids:
            self.assertIsNotNone(service.get_confirmation_request(request_id))


    def test_owner_confirmation_exception_fails_closed(self) -> None:
        service = AuthorizationService(confirmation_ttl_seconds=60.0)
        executed = []

        def owner_confirmation_handler(
            _action: AgentAction,
            _request_id: str,
        ):
            raise RuntimeError("simulated confirmation failure")

        from agentpro.executor import AgentExecutor
        from agentpro.fsm import AgentFSM
        from agentpro.models import AgentContext, Observation
        from agentpro.trace import TraceRecorder
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            context = AgentContext(goal="security test")
            executor = AgentExecutor(
                context=context,
                fsm=AgentFSM(context),
                trace=TraceRecorder(Path(directory) / "trace.jsonl"),
                observer=lambda: Observation(data={}),
                planner=lambda _context: [],
                action_executor=lambda action: executed.append(action),
                goal_verifier=lambda _context, _observation: True,
                recovery_handler=lambda _context: False,
                authorization_service=service,
                owner_confirmation_handler=owner_confirmation_handler,
            )

            result = executor._authorize_dangerous_action(
                AgentAction(
                    action_type=ActionType.INSTALL_APK,
                    arguments={"path": "/tmp/test.apk"},
                )
            )

        self.assertFalse(result)
        self.assertEqual(executed, [])

if __name__ == "__main__":
    unittest.main()

if __name__ == "__main__":
    unittest.main()
