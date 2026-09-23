from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentpro.models import AgentState
from agentpro.replay import ReplayFailureCode, TraceReplay
from agentpro.trace import TraceRecorder


class TestTraceReplay(unittest.TestCase):
    def setUp(self) -> None:
        self.replay = TraceReplay()

    def _path(self, directory: str) -> Path:
        return Path(directory) / "trace.jsonl"

    def _write(self, path: Path, records: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _valid_records(self) -> list[dict]:
        return [
            {
                "timestamp": 100.0,
                "event": "execution_started",
                "state": AgentState.OBSERVE.value,
                "step": 0,
                "payload": {"goal": "test"},
            },
            {
                "timestamp": 101.0,
                "event": "state_transition",
                "state": AgentState.PLAN.value,
                "step": 1,
                "payload": {"target": AgentState.PLAN.value},
            },
            {
                "timestamp": 102.0,
                "event": "action_started",
                "state": AgentState.EXECUTE.value,
                "step": 2,
                "payload": {"step": 2},
            },
        ]

    def test_valid_trace_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(path, self._valid_records())

            result = self.replay.replay(path)

            self.assertTrue(result.eligible)
            self.assertTrue(result.success)
            self.assertEqual(result.event_count, 3)
            self.assertIsNone(result.first_failure_code)
            self.assertEqual(result.final_state, AgentState.EXECUTE.value)

    def test_empty_trace_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            path.write_text("", encoding="utf-8")

            result = self.replay.replay(path)

            self.assertTrue(result.eligible)
            self.assertTrue(result.success)
            self.assertEqual(result.event_count, 0)
            self.assertEqual(result.state_sequence, ())
            self.assertEqual(result.event_sequence, ())

    def test_malformed_json_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            path.write_text("{broken\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                self.replay._read_records(path)

    def test_missing_field_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            record = self._valid_records()[0]
            del record["payload"]
            self._write(path, [record])

            result = self.replay.replay(path)

            self.assertFalse(result.eligible)
            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_RECORD,
            )

    def test_invalid_state_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            record = self._valid_records()[0]
            record["state"] = "NOT_A_STATE"
            self._write(path, [record])

            result = self.replay.replay(path)

            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_STATE,
            )

    def test_invalid_step_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            record = self._valid_records()[0]
            record["step"] = -1
            self._write(path, [record])

            result = self.replay.replay(path)

            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_STEP,
            )

    def test_invalid_payload_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            record = self._valid_records()[0]
            record["payload"] = ["not", "a", "mapping"]
            self._write(path, [record])

            result = self.replay.replay(path)

            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_PAYLOAD,
            )

    def test_non_monotonic_step_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            records = self._valid_records()
            records[2]["step"] = 0
            self._write(path, records)

            result = self.replay.replay(path)

            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_ORDER,
            )

    def test_deterministic_repeated_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(path, self._valid_records())

            first = self.replay.replay(path)
            second = self.replay.replay(path)

            self.assertEqual(first, second)

    def test_replay_does_not_execute_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(
                path,
                [
                    {
                        "timestamp": 1.0,
                        "event": "action_started",
                        "state": AgentState.EXECUTE.value,
                        "step": 0,
                        "payload": {
                            "action": {
                                "action_type": "install_apk",
                                "arguments": {"path": "/untrusted.apk"},
                            }
                        },
                    }
                ],
            )

            result = self.replay.replay(path)

            self.assertTrue(result.success)
            self.assertTrue(result.eligible)

    def test_replay_does_not_grant_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(
                path,
                [
                    {
                        "timestamp": 1.0,
                        "event": "authorization_granted",
                        "state": AgentState.NEED_CONFIRMATION.value,
                        "step": 0,
                        "payload": {
                            "request_id": "recorded-only",
                            "granted": True,
                        },
                    }
                ],
            )

            result = self.replay.replay(path)

            self.assertTrue(result.success)
            self.assertTrue(result.eligible)

    def test_dangerous_action_trace_remains_inert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(
                path,
                [
                    {
                        "timestamp": 1.0,
                        "event": "action_started",
                        "state": AgentState.EXECUTE.value,
                        "step": 0,
                        "payload": {
                            "action_type": "install_apk",
                            "path": "/untrusted.apk",
                        },
                    }
                ],
            )

            result = self.replay.replay(path)

            self.assertTrue(result.success)
            self.assertEqual(result.event_count, 1)

    def test_original_trace_remains_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(path, self._valid_records())
            original = path.read_bytes()

            self.replay.replay(path)

            self.assertEqual(path.read_bytes(), original)

    def test_first_deterministic_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            records = self._valid_records()
            records[0]["state"] = "INVALID"
            records[1]["step"] = -10
            self._write(path, records)

            result = self.replay.replay(path)

            self.assertEqual(
                result.first_failure_code,
                ReplayFailureCode.INVALID_STATE,
            )

    def test_trace_recorder_output_is_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            recorder = TraceRecorder(path)
            recorder.record(
                "execution_started",
                AgentState.OBSERVE.value,
                0,
                {"goal": "replay"},
            )
            recorder.record(
                "state_transition",
                AgentState.PLAN.value,
                1,
                {"target": AgentState.PLAN.value},
            )

            result = self.replay.replay(path)

            self.assertTrue(result.eligible)
            self.assertTrue(result.success)
            self.assertEqual(result.event_count, 2)


if __name__ == "__main__":
    unittest.main()
