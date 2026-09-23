from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import AgentState


class ReplayFailureCode:
    IO_ERROR = "REPLAY_IO_ERROR"
    INVALID_JSON = "REPLAY_INVALID_JSON"
    INVALID_RECORD = "REPLAY_INVALID_RECORD"
    INVALID_TIMESTAMP = "REPLAY_INVALID_TIMESTAMP"
    INVALID_EVENT = "REPLAY_INVALID_EVENT"
    INVALID_STATE = "REPLAY_INVALID_STATE"
    INVALID_STEP = "REPLAY_INVALID_STEP"
    INVALID_PAYLOAD = "REPLAY_INVALID_PAYLOAD"
    INVALID_ORDER = "REPLAY_INVALID_ORDER"
    INVALID_TRANSITION = "REPLAY_INVALID_TRANSITION"
    INVALID_CORRELATION = "REPLAY_INVALID_CORRELATION"


@dataclass(frozen=True)
class ReplayResult:
    eligible: bool
    success: bool
    event_count: int
    first_failure_code: str | None
    first_failure_message: str | None
    final_state: str | None
    state_sequence: tuple[str, ...]
    event_sequence: tuple[str, ...]


class TraceReplay:
    _REQUIRED_FIELDS = frozenset(
        {"timestamp", "event", "state", "step", "payload"}
    )
    _TRANSITION_EVENT = "state_transition"

    def replay(self, path: str | Path) -> ReplayResult:
        trace_path = Path(path)

        try:
            raw_records = self._read_records(trace_path)
        except OSError as exc:
            return self._failure(
                ReplayFailureCode.IO_ERROR,
                f"Unable to read trace: {exc}",
            )
        except UnicodeDecodeError as exc:
            return self._failure(
                ReplayFailureCode.INVALID_RECORD,
                f"Trace is not valid UTF-8: {exc}",
            )
        except ValueError as exc:
            return self._failure(
                ReplayFailureCode.INVALID_JSON,
                str(exc),
            )

        if not raw_records:
            return ReplayResult(
                eligible=True,
                success=True,
                event_count=0,
                first_failure_code=None,
                first_failure_message=None,
                final_state=None,
                state_sequence=(),
                event_sequence=(),
            )

        states: list[str] = []
        events: list[str] = []
        previous_step: int | None = None
        previous_timestamp: float | None = None

        for index, record in enumerate(raw_records, start=1):
            failure = self._validate_record(
                record=record,
                record_number=index,
                previous_step=previous_step,
                previous_timestamp=previous_timestamp,
            )
            if failure is not None:
                return ReplayResult(
                    eligible=False,
                    success=False,
                    event_count=len(raw_records),
                    first_failure_code=failure[0],
                    first_failure_message=failure[1],
                    final_state=states[-1] if states else None,
                    state_sequence=tuple(states),
                    event_sequence=tuple(events),
                )

            timestamp = record["timestamp"]
            step = record["step"]
            event = record["event"]
            state = record["state"]
            payload = record["payload"]

            if event == self._TRANSITION_EVENT:
                target = payload.get("target")
                if not isinstance(target, str):
                    return ReplayResult(
                        eligible=False,
                        success=False,
                        event_count=len(raw_records),
                        first_failure_code=ReplayFailureCode.INVALID_TRANSITION,
                        first_failure_message=(
                            f"Record {index}: state_transition requires "
                            "a string target"
                        ),
                        final_state=states[-1] if states else None,
                        state_sequence=tuple(states),
                        event_sequence=tuple(events),
                    )
                if target not in AgentState._value2member_map_:
                    return ReplayResult(
                        eligible=False,
                        success=False,
                        event_count=len(raw_records),
                        first_failure_code=ReplayFailureCode.INVALID_TRANSITION,
                        first_failure_message=(
                            f"Record {index}: invalid transition target "
                            f"{target!r}"
                        ),
                        final_state=states[-1] if states else None,
                        state_sequence=tuple(states),
                        event_sequence=tuple(events),
                    )

            if event in {
                "action_started",
                "action_result",
                "action_failed",
                "action_error",
            }:
                action_step = payload.get("step")
                if action_step is not None and (
                    not isinstance(action_step, int)
                    or isinstance(action_step, bool)
                    or action_step != step
                ):
                    return ReplayResult(
                        eligible=False,
                        success=False,
                        event_count=len(raw_records),
                        first_failure_code=ReplayFailureCode.INVALID_CORRELATION,
                        first_failure_message=(
                            f"Record {index}: execution payload step "
                            "does not match trace step"
                        ),
                        final_state=states[-1] if states else None,
                        state_sequence=tuple(states),
                        event_sequence=tuple(events),
                    )

            states.append(state)
            events.append(event)
            previous_step = step
            previous_timestamp = timestamp

        return ReplayResult(
            eligible=True,
            success=True,
            event_count=len(raw_records),
            first_failure_code=None,
            first_failure_message=None,
            final_state=states[-1],
            state_sequence=tuple(states),
            event_sequence=tuple(events),
        )

    @staticmethod
    def _read_records(path: Path) -> list[Mapping[str, Any]]:
        records: list[Mapping[str, Any]] = []

        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON at line {line_number}: {exc}"
                    ) from exc

                if not isinstance(record, Mapping):
                    raise ValueError(
                        f"Invalid trace record at line {line_number}: "
                        "expected object"
                    )

                records.append(record)

        return records

    @classmethod
    def _validate_record(
        cls,
        record: Mapping[str, Any],
        record_number: int,
        previous_step: int | None,
        previous_timestamp: float | None,
    ) -> tuple[str, str] | None:
        missing = cls._REQUIRED_FIELDS.difference(record.keys())
        if missing:
            return (
                ReplayFailureCode.INVALID_RECORD,
                f"Record {record_number}: missing fields "
                f"{sorted(missing)!r}",
            )

        timestamp = record["timestamp"]
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or timestamp < 0
        ):
            return (
                ReplayFailureCode.INVALID_TIMESTAMP,
                f"Record {record_number}: invalid timestamp",
            )

        event = record["event"]
        if not isinstance(event, str) or not event.strip():
            return (
                ReplayFailureCode.INVALID_EVENT,
                f"Record {record_number}: event must be a non-empty string",
            )

        state = record["state"]
        if (
            not isinstance(state, str)
            or state not in AgentState._value2member_map_
        ):
            return (
                ReplayFailureCode.INVALID_STATE,
                f"Record {record_number}: invalid state {state!r}",
            )

        step = record["step"]
        if not isinstance(step, int) or isinstance(step, bool) or step < 0:
            return (
                ReplayFailureCode.INVALID_STEP,
                f"Record {record_number}: step must be a non-negative integer",
            )

        if previous_step is not None and step < previous_step:
            return (
                ReplayFailureCode.INVALID_ORDER,
                f"Record {record_number}: step {step} precedes "
                f"previous step {previous_step}",
            )

        if previous_timestamp is not None and timestamp < previous_timestamp:
            return (
                ReplayFailureCode.INVALID_ORDER,
                f"Record {record_number}: timestamp precedes "
                "previous timestamp",
            )

        payload = record["payload"]
        if not isinstance(payload, Mapping):
            return (
                ReplayFailureCode.INVALID_PAYLOAD,
                f"Record {record_number}: payload must be an object",
            )

        return None

    @staticmethod
    def _failure(code: str, message: str) -> ReplayResult:
        return ReplayResult(
            eligible=False,
            success=False,
            event_count=0,
            first_failure_code=code,
            first_failure_message=message,
            final_state=None,
            state_sequence=(),
            event_sequence=(),
        )
