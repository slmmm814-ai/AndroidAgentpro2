from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentpro.models import AgentState
from agentpro.replay import ReplayFailureCode, TraceReplay


class TestPhase7TraceReplayBreaker(unittest.TestCase):
    def setUp(self) -> None:
        self.replay = TraceReplay()

    def _path(self, directory: str) -> Path:
        return Path(directory) / "trace.jsonl"

    def _write(self, path: Path, records: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def _record(
        self,
        event: str = "execution_started",
        state: str = AgentState.OBSERVE.value,
        step: int = 0,
        payload: dict | list | str | None = None,
    ) -> dict:
        return {
            "timestamp": 100.0,
            "event": event,
            "state": state,
            "step": step,
            "payload": {} if payload is None else payload,
        }

    def test_invalid_transition_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(
                path,
                [
                    self._record(
                        event="state_transition",
                        state=AgentState.PLAN.value,
                        payload={"target": "FORGED_STATE"},
                    )
                ],
            )

            result = self.replay.replay(path)

            self.assertFalse(result.eligible)
            self.assertFalse(result.success)
            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_TRANSITION,
            )

    def test_execution_payload_step_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(
                path,
                [
                    self._record(
                        event="action_started",
                        state=AgentState.EXECUTE.value,
                        step=5,
                        payload={"step": 4},
                    )
                ],
            )

            result = self.replay.replay(path)

            self.assertFalse(result.eligible)
            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_CORRELATION,
            )

    def test_boolean_step_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(
                path,
                [self._record(step=True)],
            )

            result = self.replay.replay(path)

            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_STEP,
            )

    def test_boolean_timestamp_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            record = self._record()
            record["timestamp"] = True
            self._write(path, [record])

            result = self.replay.replay(path)

            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_TIMESTAMP,
            )

    def test_sensitive_payload_is_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            marker = Path(directory) / "should_not_exist"
            self._write(
                path,
                [
                    self._record(
                        event="action_started",
                        state=AgentState.EXECUTE.value,
                        payload={
                            "action_type": "install_apk",
                            "arguments": {
                                "path": str(marker),
                                "command": "touch " + str(marker),
                            },
                        },
                    )
                ],
            )

            result = self.replay.replay(path)

            self.assertTrue(result.success)
            self.assertFalse(marker.exists())

    def test_authorization_payload_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(
                path,
                [
                    self._record(
                        event="authorization_granted",
                        state=AgentState.NEED_CONFIRMATION.value,
                        payload={
                            "request_id": "forged",
                            "action_fingerprint": "forged",
                            "granted": True,
                        },
                    )
                ],
            )

            result = self.replay.replay(path)

            self.assertTrue(result.success)
            self.assertTrue(result.eligible)

    def test_first_failure_is_stable_under_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            records = [
                self._record(
                    state="FORGED_STATE",
                ),
                self._record(
                    state="ALSO_FORGED",
                    step=1,
                ),
            ]
            self._write(path, records)

            first = self.replay.replay(path)
            second = self.replay.replay(path)

            self.assertEqual(first, second)
            self.assertEqual(
                first.first_failure_code,
                ReplayFailureCode.INVALID_STATE,
            )


if __name__ == "__main__":
    unittest.main()
