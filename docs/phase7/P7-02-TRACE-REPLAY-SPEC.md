# AndroidAgentPro — P7-02 Trace Replay Specification

## 1. Purpose

P7-02 defines deterministic replay of recorded Phase 7 task traces.

Replay MUST use persisted TraceRecorder output as its source of evidence.

Replay MUST NOT execute real Android actions.

Replay MUST NOT grant owner authorization.

Replay MUST NOT modify AuthorizationService state.

Replay MUST NOT modify the original trace.

## 2. Scope

P7-02 covers:

1. Loading a persisted JSONL trace.
2. Validating its structural integrity.
3. Reconstructing the recorded execution sequence.
4. Comparing replayed state/event progression with the recorded sequence.
5. Producing a deterministic replay result.
6. Identifying replay-ineligible traces without executing actions.

P7-02 does not redesign TraceRecorder.

P7-02 does not change AgentExecutor behavior.

P7-02 does not change authorization semantics.

P7-02 does not execute Android, shell, installation, network, or other external actions.

## 3. Replay Input

The replay input is an existing TraceRecorder JSONL file.

Each trace record MUST contain:

- timestamp
- event
- state
- step
- payload

The trace loader MUST reject:

- malformed JSON
- missing required fields
- invalid event values
- invalid state values
- invalid step values
- non-mapping payloads
- records with invalid ordering

## 4. Replay Safety Boundary

Replay is an analysis operation only.

A replay implementation MUST NOT call:

- ActionExecutor
- Accessibility APIs
- Android installation APIs
- AuthorizationService.grant
- AuthorizationService.authorize
- OwnerConfirmationHandler
- external network services

A replay result MUST never be interpreted as owner confirmation.

A replay result MUST never be used as authorization evidence for a dangerous action.

## 5. Determinism

Replay MUST be deterministic for identical trace input.

The same trace bytes MUST produce the same:

- replay eligibility
- event sequence
- state sequence
- validation result
- failure classification

Wall-clock time MUST NOT determine replay correctness.

The replay engine MUST treat recorded timestamps as trace data, not as execution delays.

## 6. Sequence Validation

Replay MUST validate:

1. Steps are non-negative integers.
2. Event records preserve their persisted order.
3. State values are valid AgentState values.
4. State-transition records contain a valid target state.
5. Execution records remain correlated to their recorded steps.
6. A malformed record causes fail-closed replay rejection.

Replay MUST NOT silently repair malformed traces.

## 7. Replay Result

The replay API MUST return a typed result containing:

- eligible
- success
- event_count
- first_failure_code
- first_failure_message
- final_state
- state_sequence
- event_sequence

The result MUST be immutable after creation.

A replay failure MUST identify the first deterministic validation failure.

## 8. Security Requirements

Replay MUST be read-only with respect to its input trace.

Replay MUST NOT persist authorization credentials, owner confirmations, authentication tokens, or secrets.

Replay MUST reject attempts to treat trace payloads as executable instructions.

Payload contents MUST be treated as inert recorded data.

A trace containing an `install_apk` event MUST remain inert during replay.

Replay MUST NOT infer that a dangerous action was authorized merely because an authorization-related event exists in the trace.

## 9. Integration Requirements

The implementation MUST use the existing:

`agentpro.trace.TraceRecorder`

and existing:

`agentpro.models.AgentState`

where applicable.

Existing Phase 1–6 behavior MUST remain unchanged.

P7-02 changes MUST be isolated to new replay implementation and dedicated tests unless a minimal compatibility export is required.

## 10. Test Requirements

Dedicated P7-02 tests MUST cover at minimum:

1. Valid trace replay.
2. Empty trace replay.
3. Malformed JSON rejection.
4. Missing-field rejection.
5. Invalid state rejection.
6. Invalid step rejection.
7. Invalid payload rejection.
8. Non-monotonic step rejection.
9. Deterministic repeated replay.
10. Replay does not execute actions.
11. Replay does not grant authorization.
12. Dangerous-action trace remains inert.
13. Original trace remains unchanged.
14. First deterministic failure is reported.

All tests MUST use deterministic local fixtures.

No real Android device, package installation, external network, credentials, or owner confirmation MUST be required.

## 11. Regression Requirements

Before P7-02 can be considered BUILT:

- P7-02 dedicated tests MUST pass.
- Phase 1 tests MUST pass.
- Phase 2 tests MUST pass.
- Phase 3 tests MUST pass.
- Phase 4–6 tests MUST pass.
- Python compilation MUST pass.
- `git diff --check` MUST pass.
- Placeholder/TODO scan MUST pass.
- Secret-like scan MUST pass.

## 12. Acceptance Criteria

P7-02 is complete only when:

1. The replay implementation is complete.
2. Replay is deterministic.
3. Replay is read-only.
4. Replay never executes actions.
5. Replay cannot grant authorization.
6. Malformed traces fail closed.
7. Dedicated tests cover the required boundaries.
8. Full regression passes.
9. ARCH_REVIEW passes.
10. The owner explicitly issues OWNER_ACCEPT.

## 13. Lifecycle

SPEC → BUILD → BUILT → BREAK → ARCH_REVIEW → FIX → REGRESSION → OWNER_ACCEPT → DONE

