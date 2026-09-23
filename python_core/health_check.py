#!/usr/bin/env python3

from __future__ import annotations

import statistics
import sys
import time
import uuid
from dataclasses import dataclass

sys.path.insert(0, "python_core")

from bridge_client import (
    BridgeClient,
    BridgeClientError,
)


TOTAL_OPERATIONS = 50
HEALTH_COMMAND = "health"


@dataclass(frozen=True)
class OperationResult:
    number: int
    request_id: str
    latency_ms: float


def run_health_check(client: BridgeClient) -> list[OperationResult]:
    results: list[OperationResult] = []

    for number in range(1, TOTAL_OPERATIONS + 1):
        request_id = str(uuid.uuid4())
        started = time.monotonic()

        try:
            response = client.command(
                HEALTH_COMMAND,
                request_id=request_id,
            )

            elapsed_ms = (time.monotonic() - started) * 1000.0

            if not response.ok:
                raise RuntimeError(
                    f"Bridge returned ok=false: "
                    f"{response.error_code}: {response.error_message}"
                )

            if response.request_id != request_id:
                raise RuntimeError(
                    "Response request_id does not match request_id"
                )

            result = OperationResult(
                number=number,
                request_id=request_id,
                latency_ms=elapsed_ms,
            )

            results.append(result)

            print(
                f"[{number:02d}/{TOTAL_OPERATIONS}] "
                f"PASS "
                f"{elapsed_ms:.1f} ms"
            )

        except BridgeClientError as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0

            print(
                f"[{number:02d}/{TOTAL_OPERATIONS}] "
                f"FAIL "
                f"{elapsed_ms:.1f} ms"
            )
            print(f"ERROR: {exc}")

            raise RuntimeError(
                f"Health check stopped at operation {number}"
            ) from exc

        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0

            print(
                f"[{number:02d}/{TOTAL_OPERATIONS}] "
                f"FAIL "
                f"{elapsed_ms:.1f} ms"
            )
            print(
                f"ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

            raise RuntimeError(
                f"Health check stopped at operation {number}"
            ) from exc

    return results


def print_summary(results: list[OperationResult]) -> None:
    latencies = [
        result.latency_ms
        for result in results
    ]

    total_ms = sum(latencies)
    average_ms = statistics.mean(latencies)
    minimum_ms = min(latencies)
    maximum_ms = max(latencies)

    print()
    print("=" * 48)
    print("ANDROIDAGENTPRO PHASE 0 HEALTH CHECK")
    print("=" * 48)
    print(f"Successful operations : {len(results)}/{TOTAL_OPERATIONS}")
    print(f"Total time            : {total_ms:.1f} ms")
    print(f"Average latency       : {average_ms:.1f} ms")
    print(f"Minimum latency       : {minimum_ms:.1f} ms")
    print(f"Maximum latency       : {maximum_ms:.1f} ms")

    if len(results) == TOTAL_OPERATIONS:
        print()
        print("PHASE_0_HEALTH_CHECK: PASS")
        print("50 consecutive health operations completed successfully.")
    else:
        print()
        print("PHASE_0_HEALTH_CHECK: FAIL")


def main() -> int:
    try:
        client = BridgeClient()
    except BridgeClientError as exc:
        print(f"CONFIGURATION_ERROR: {exc}")
        return 2

    print("=" * 48)
    print("AndroidAgentPro Phase 0 Health Check")
    print("=" * 48)
    print(f"Target: 127.0.0.1:8070")
    print(f"Operations: {TOTAL_OPERATIONS}")
    print()

    try:
        results = run_health_check(client)
    except RuntimeError:
        print()
        print("PHASE_0_HEALTH_CHECK: FAIL")
        return 1

    print_summary(results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
