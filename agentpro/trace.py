from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class TraceEvent:
    timestamp: float
    event: str
    state: str
    step: int
    payload: Mapping[str, Any]


class TraceRecorder:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        event: str,
        state: str,
        step: int = 0,
        payload: Mapping[str, Any] | None = None,
    ) -> TraceEvent:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event must not be empty")

        if not isinstance(state, str) or not state.strip():
            raise ValueError("state must not be empty")

        if not isinstance(step, int) or isinstance(step, bool) or step < 0:
            raise ValueError("step must be a non-negative integer")

        if payload is not None and not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")

        trace_event = TraceEvent(
            timestamp=time.time(),
            event=event,
            state=state,
            step=step,
            payload=dict(payload or {}),
        )

        record = {
            "timestamp": trace_event.timestamp,
            "event": trace_event.event,
            "state": trace_event.state,
            "step": trace_event.step,
            "payload": dict(trace_event.payload),
        }

        encoded = (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )

        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()

        return trace_event

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []

        records: list[dict[str, Any]] = []

        with self._lock:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    stripped = line.strip()

                    if not stripped:
                        continue

                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"Invalid trace JSON at line {line_number}"
                        ) from exc

                    if not isinstance(record, dict):
                        raise ValueError(
                            f"Invalid trace record at line {line_number}"
                        )

                    required = {"timestamp", "event", "state", "step", "payload"}
                    if not required.issubset(record):
                        raise ValueError(
                            f"Invalid trace record at line {line_number}: "
                            "missing required fields"
                        )

                    if not isinstance(record["payload"], dict):
                        raise ValueError(
                            f"Invalid trace payload at line {line_number}"
                        )

                    records.append(record)

        return records

    def read(self) -> list[dict[str, Any]]:
        return self.read_all()
