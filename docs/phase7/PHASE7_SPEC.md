# AndroidAgentPro — Phase 7 Specification

## 1. Purpose

Phase 7 is the final integration, measurement, replay, security-review,
and operational-readiness phase of AndroidAgentPro.

Phase 7 MUST NOT introduce unrelated product features.

Phase 7 MUST preserve the completed behavior of Phases 0 through 6.

The Git repository is the executable source of truth.

## 2. Current Baseline

Phase 0: COMPLETE
Phase 1: COMPLETE
Phase 2: COMPLETE
Phase 3: COMPLETE / OWNER_ACCEPTED
Phase 4: COMPLETE / OWNER_ACCEPTED
Phase 5.1: COMPLETE / OWNER_ACCEPTED
Phase 5.2: COMPLETE / OWNER_ACCEPTED
Phase 6.1: COMPLETE / OWNER_ACCEPTED
Phase 6.2: COMPLETE / OWNER_ACCEPTED
Phase 6.3: COMPLETE / OWNER_ACCEPTED

E2E-01 is explicitly removed from the Phase 7 prerequisite chain by
owner decision.

## 3. Phase 7 Lifecycle

Phase 7 SHALL follow:

SPEC
→ BUILD
→ BUILT
→ BREAK
→ ARCH_REVIEW
→ FIX
→ REGRESSION
→ OWNER_ACCEPT
→ DONE

No Phase 7 implementation item may be declared DONE before its dedicated
verification and regression evidence exists.

## 4. Work Breakdown

### P7-00 — Phase 7 Specification

Define:

- Phase 7 boundaries
- task corpus structure
- measurement methodology
- Trace replay requirements
- security review requirements
- operational documentation requirements
- regression gates
- final acceptance gates

P7-00 produces this specification document.

Acceptance:

- specification is version-controlled
- scope is explicit
- no Phase 7 requirement depends on E2E-01
- existing Phase 0–6 behavior is protected

### P7-01 — Task Corpus

Create a deterministic Phase 7 task corpus.

The corpus SHALL contain four classes:

1. Simple tasks
2. Medium tasks
3. Complex tasks
4. Programmatic tasks

Every task SHALL have:

- stable task identifier
- task description
- initial-state requirements
- expected observable result
- permitted action classes
- verification requirements
- timeout policy
- failure classification
- replay requirements

The task definition SHALL exist before its measurement is performed.

The corpus SHALL be version-controlled.

No task SHALL be counted as successful merely because the planner
generated an action.

A successful task requires the complete execution and verification
chain required by the architecture.

### P7-02 — Trace Replay

Implement and test deterministic replay of previously recorded
successful tasks.

Replay SHALL use persisted Trace information.

Replay SHALL verify:

- task identity
- action sequence
- relevant state transitions
- authorization requirements
- verification results
- final goal result

Replay SHALL fail closed when required trace information is missing,
corrupt, inconsistent, unauthorized, or incompatible with the current
execution contract.

Sensitive authorization material MUST NOT be persisted in Trace.

Replay MUST NOT provide a mechanism for bypassing AuthorizationService,
SixLayerVerifier, or other existing safety boundaries.

### P7-03 — Final Security Review

Perform an architectural security review of the complete execution path.

The review SHALL cover at minimum:

- loopback bridge exposure
- authentication/token validation
- malformed requests
- replayed requests
- forged request identifiers
- action/fingerprint mismatch
- expired confirmations
- confirmation ownership
- dangerous-action authorization
- executor fail-closed behavior
- verifier enforcement
- trace confidentiality
- secret leakage
- error-path behavior
- concurrent-request behavior

The review SHALL distinguish:

- confirmed behavior
- tested behavior
- identified risk
- unresolved limitation

No security claim may be made without supporting evidence.

### P7-04 — Operational Documentation

Provide sufficient documentation for a clean installation and operation
of the project.

Documentation SHALL cover:

- repository preparation
- Android component preparation
- Python/Termux preparation
- required permissions
- Bridge startup
- health verification
- test execution
- Git workflow
- Phase/state inspection
- failure diagnosis
- recovery procedure
- Trace inspection
- clean restart procedure

Documentation SHALL avoid undocumented machine-specific assumptions.

No secret, token, credential, or private authorization material may be
committed to documentation.

### P7-05 — Measurement and Closure

Execute the complete Phase 7 corpus.

Record raw results.

Calculate class-specific success rates.

Required target thresholds:

- Simple: >= 95%
- Medium: >= 85%
- Complex: >= 70%
- Programmatic: >= 60%

The thresholds are acceptance targets and SHALL NOT be converted into
a ranking of political, commercial, or external systems.

A task counts as successful only when its predefined success condition
and verification condition are both satisfied.

The final closure package SHALL contain:

- corpus version
- raw execution results
- aggregate measurements
- failed-task records
- Trace replay evidence
- security review
- documentation verification
- regression results
- repository integrity checks

## 5. Regression Gate

Before Phase 7 OWNER_ACCEPT:

- Phase 1 tests SHALL pass.
- Phase 2 tests SHALL pass.
- Phase 3 tests SHALL pass.
- Existing Phase 4–6 tests SHALL pass.
- Phase 7 dedicated tests SHALL pass.
- Python compile checks SHALL pass.
- Git whitespace checks SHALL pass.
- Placeholder/TODO checks SHALL pass where required by project policy.
- Secret-like material checks SHALL pass.
- No previously accepted phase may be silently modified.

Any regression is a blocking defect until resolved.

## 6. Evidence Rules

Every claimed result SHALL have reproducible evidence.

The following statements are forbidden without execution evidence:

- "works"
- "fully tested"
- "secure"
- "production ready"
- "all tests pass"

Source-of-truth priority:

1. Git repository state
2. committed project artifacts
3. reproducible test output
4. current STATE.md
5. conversational claims

## 7. Failure Classification

BLOCKER:

Prevents execution, invalidates security boundaries, corrupts state,
or makes acceptance impossible.

MAJOR:

Breaks a Phase 7 acceptance requirement or causes material regression.

MINOR:

Does not block the acceptance criteria but requires documented
resolution or explicit disposition.

Phase 7 SHALL NOT be declared DONE while an unresolved BLOCKER or
MAJOR remains.

## 8. Security Boundary

Phase 7 SHALL NOT weaken existing authorization.

In particular:

- Web Research SHALL NOT grant dangerous-action authorization.
- Trace Replay SHALL NOT grant dangerous-action authorization.
- Planner output SHALL NOT constitute owner confirmation.
- Executor SHALL NOT execute a dangerous action without the existing
  authorization requirements.
- Verification SHALL remain mandatory after execution.

## 9. Scope Boundary

Phase 7 SHALL NOT:

- redesign completed Phase 0–6 architecture without evidence
- remove existing security controls
- bypass tests
- replace reproducible measurements with manual claims
- introduce unrelated features
- resurrect E2E-01
- change accepted behavior merely for convenience

## 10. Final Acceptance

Phase 7 may enter OWNER_ACCEPT only when:

1. P7-01 is complete.
2. P7-02 is complete.
3. P7-03 is complete.
4. P7-04 is complete.
5. P7-05 measurements are complete.
6. All required regression tests pass.
7. Security review has no unresolved BLOCKER or MAJOR.
8. Required documentation exists.
9. Evidence is committed or otherwise reproducibly referenced.
10. The owner can inspect the complete Phase 7 result.

Only the owner may provide OWNER_ACCEPT.

After OWNER_ACCEPT, the project state may transition to:

Phase 7 — DONE
