# P7-03 — Final Security Review

## 1. Purpose

P7-03 performs the final architectural security review of the AndroidAgentPro execution core after completion of P6-03 and P7-02.

The review is evidence-driven and must distinguish:

- CONFIRMED behavior;
- TESTED behavior;
- IDENTIFIED_RISK;
- UNRESOLVED_LIMITATION.

No security claim may be made without reproducible supporting evidence.

P7-03 MUST NOT redesign completed phases or introduce a new authorization mechanism.

---

## 2. Security Review Scope

The review SHALL cover the security-sensitive execution path represented in the current repository:

```text
UNTRUSTED WEB DATA
        |
        v
PLANNING / CANDIDATE ACTION
        |
        v
DANGEROUS ACTION CLASSIFICATION
        |
        v
OWNER AUTHORIZATION
        |
        v
SIX-LAYER VERIFICATION
        |
        v
EXECUTION
        |
        v
EXECUTION EVIDENCE / TRACE
The review SHALL cover:
loopback bridge exposure;
authentication and token validation;
malformed requests;
replayed requests;
forged request identifiers;
action/fingerprint mismatch;
expired confirmations;
confirmation ownership;
dangerous-action authorization;
executor fail-closed behavior;
verifier enforcement;
trace confidentiality;
secret leakage;
error-path behavior;
concurrent-request behavior;
replay isolation.
3. Repository Boundary
P7-03 SHALL review only behavior that exists in the current repository.
The current repository contains the Python execution core and its authorization, verification, execution, research, trace, and replay components.
The current repository MUST NOT be assumed to contain an Android HTTP BridgeServer merely because the architecture references:
127.0.0.1:8070
If no implemented BridgeServer exists in the repository, the review SHALL record:
BRIDGE_IMPLEMENTATION_STATUS = NOT_PRESENT_IN_REPOSITORY
BRIDGE_SECURITY_VERIFICATION = UNRESOLVED_LIMITATION
References to 127.0.0.1:8070 inside tests or documentation SHALL NOT be treated as proof that the bridge implementation exists.
P7-03 SHALL NOT create a replacement BridgeServer merely to close this review.
4. Security Baseline
The following existing contracts are in scope and SHALL be verified rather than assumed:
4.1 Dangerous action authorization
The dangerous-action policy currently identifies install_apk as a dangerous action requiring owner authorization.
A dangerous action MUST NOT execute without valid authorization.
4.2 Generic confirmation
Any action explicitly marked:
requires_confirmation = true
MUST fail closed when no confirmation handler is available.
Implicit approval is prohibited.
4.3 Owner confirmation
Owner confirmation MUST be:
bound to the authorization service instance;
bound to the exact request identifier;
bound to the exact action;
bound to the exact action fingerprint;
rejected after expiration;
rejected when timestamps are invalid;
rejected when authorization is absent or denied.
4.4 Web research isolation
Web research data MUST NOT grant owner authorization.
A web document, redirect, URL, search result, or retrieved content MUST NOT be accepted as owner confirmation.
4.5 Verification
Dangerous execution MUST remain subject to the existing verification pipeline.
Verification MUST NOT be bypassed merely because authorization succeeded.
4.6 Trace confidentiality
Trace records MUST NOT expose:
owner confirmation material;
authorization tokens;
authentication secrets;
private credentials.
4.7 Replay isolation
Trace replay MUST remain read-only.
Replay MUST NOT:
execute Android actions;
grant authorization;
mutate AuthorizationService state;
invoke owner confirmation handling;
perform external network requests.
Threat Model
P7-03 SHALL review the following threats.
ID
Threat
T01
Authentication bypass
T02
Malformed request exploitation
T03
Replayed authorization/request
T04
Forged request identifier
T05
Action substitution
T06
Fingerprint substitution
T07
Expired confirmation reuse
T08
Foreign authorization-service confirmation
T09
Web-to-authorization injection
T10
Verification bypass
T11
Fail-open confirmation/error path
T12
Trace secret leakage
T13
Concurrent authorization race
T14
Loopback bridge exposure
Each threat SHALL receive an evidence classification.
6. Required Security Questions
The review SHALL answer, with evidence:
Can an unauthorized dangerous action reach execution?
Can a forged request identifier authorize an action?
Can an authorization for one action authorize another action?
Can a fingerprint for one action authorize another action?
Can an expired confirmation be reused?
Can a confirmation from another AuthorizationService instance be reused?
Can malformed authorization data produce approval?
Can an exception in the confirmation path produce approval?
Can web research content become owner authorization?
Can execution occur without required verification?
Can trace data expose authorization secrets?
Can replay perform side effects?
Can concurrent requests race into unauthorized execution?
Is an HTTP bridge actually present in the repository?
If a bridge is absent, is that absence explicitly recorded as an unresolved limitation?
7. Required Breaker Coverage
P7-03 SHALL provide or reuse tests covering, at minimum:
malformed authorization;
missing authorization;
forged request ID;
action mismatch;
fingerprint mismatch;
expired confirmation;
future/invalid confirmation timestamp;
foreign AuthorizationService;
web content attempting to authorize an action;
verifier bypass attempt;
dangerous action without authorization;
generic confirmation-required action without handler;
confirmation handler exception;
authorization exception;
execution after authorization denial;
trace authorization-secret leakage;
trace token leakage;
replay execution attempt;
replay authorization attempt;
replay AuthorizationService mutation attempt;
replay external-network attempt;
concurrent authorization behavior;
loopback/bridge security status.
Existing tests SHALL be reused where they already provide equivalent coverage. Duplicate tests SHALL NOT be created without a demonstrated coverage gap.
8. Authentication and Loopback Bridge Review
The reviewer SHALL search the repository for:
127.0.0.1
8070
BridgeServer
HTTP
token
Authorization
Bearer
request_id
The review SHALL determine whether an actual HTTP server implementation exists.
If implemented, the review SHALL verify:
loopback binding;
token authentication;
rejection of missing tokens;
rejection of invalid tokens;
malformed-request handling;
request identifier validation;
replay protection;
concurrent-request handling;
safe error responses;
absence of secret leakage.
If not implemented, the review SHALL record:
BRIDGE_IMPLEMENTATION_STATUS = NOT_PRESENT_IN_REPOSITORY
BRIDGE_SECURITY_VERIFICATION = UNRESOLVED_LIMITATION
No inferred security guarantee is permitted.
9. Authorization Review
The reviewer SHALL inspect:
agentpro/authorization.py
agentpro/executor.py
agentpro/verifier.py
The review SHALL verify:
deterministic dangerous-action classification;
exact request identity;
exact action identity;
exact fingerprint matching;
authorization-service ownership;
expiration enforcement;
timestamp validation;
fail-closed behavior;
denial propagation;
absence of execution after denial.
The reviewer SHALL specifically verify that a missing generic confirmation handler cannot implicitly approve an action.
10. Web Research Review
The reviewer SHALL inspect:
agentpro/web_research.py
The review SHALL verify that untrusted web data cannot:
create owner confirmation;
replace owner confirmation;
authorize dangerous actions;
bypass the verifier;
mutate authorization state.
Redirects and private/loopback destinations SHALL be treated according to the existing web-research security contract.
11. Verifier Review
The reviewer SHALL inspect the six-layer verifier and confirm that verification remains mandatory for execution paths that require it.
The review SHALL check:
schema validation;
action-result validation;
safety validation;
evidence validation;
correlation validation;
goal validation.
A successful authorization decision SHALL NOT itself be considered proof that verification succeeded.
12. Trace Review
The reviewer SHALL inspect trace creation and persistence.
Trace data SHALL be checked for accidental exposure of:
authorization secrets;
owner confirmation values;
authentication tokens;
private credentials;
sensitive authorization internals.
The trace SHALL remain useful for forensic review without becoming a secret-storage channel.
13. Replay Review
The reviewer SHALL inspect:
agentpro/replay.py
Replay MUST remain observational and deterministic.
Replay MUST NOT:
execute an AgentAction;
call an Android bridge;
grant owner authorization;
call OwnerConfirmationHandler;
mutate AuthorizationService;
perform network requests;
alter production execution state.
14. Concurrency Review
The reviewer SHALL inspect synchronization around security-sensitive shared state.
The review SHALL identify:
shared mutable authorization state;
locking strategy;
confirmation consumption;
trace writes;
executor shared state;
race-sensitive decisions.
Any identified race MUST be classified as:
CONFIRMED
TESTED
IDENTIFIED_RISK
UNRESOLVED_LIMITATION
No concurrency safety claim may be made solely from source-code appearance.
15. Error-Path Review
Security-sensitive exceptions SHALL fail closed.
The review SHALL explicitly test or inspect:
authorization exceptions;
confirmation-handler exceptions;
malformed inputs;
verifier failures;
executor failures;
trace failures;
replay parsing failures.
An exception MUST NOT silently convert denial into approval or failed execution into success.
16. Evidence Classification
Every material security finding SHALL use exactly one classification:
CONFIRMED
Directly established by source inspection or reproducible behavior.
TESTED
Established by an executable test with a reproducible result.
IDENTIFIED_RISK
A credible security concern exists but is not yet demonstrated as exploitable.
UNRESOLVED_LIMITATION
The repository lacks the implementation or evidence required to establish the security property.
No other evidence category is permitted in the final P7-03 review.
17. Severity
Security findings SHALL use:
BLOCKER
MAJOR
MINOR
Severity SHALL describe the technical security impact and SHALL NOT be used as an overall project ranking.
P7-03 cannot be considered complete while a BLOCKER or MAJOR finding remains unresolved unless the owner explicitly accepts the documented limitation.
18. Minimal-Change Rule
P7-03 SHALL NOT:
redesign Phase 6 authorization;
replace AuthorizationService;
add a second authorization mechanism;
add new dangerous tools;
weaken existing tests;
remove security checks;
bypass verification;
modify P7-02 replay semantics;
implement E2E-01;
implement a new Android BridgeServer;
introduce unrelated features.
If a security defect is demonstrated, the correction SHALL be the smallest safe production change that closes the demonstrated defect without changing unrelated behavior.
19. Required Verification Gates
Before P7-03 can reach OWNER_ACCEPT, all applicable gates SHALL pass:
Phase 1 tests
Phase 2 tests
Phase 3 tests
Phase 4 tests
Phase 5 tests
Phase 6 tests
Phase 7 tests
Python compile check
git diff --check
placeholder/TODO scan
secret-like scan
P7-02 regression
P7-03 breaker coverage
No gate may be reported as PASS without actual execution evidence.
20. Architect Review
Before OWNER_ACCEPT, the final P7-03 review SHALL explicitly state:
what was inspected;
what was tested;
what was fixed;
what remains unresolved;
why no unrelated component was changed;
whether any BLOCKER or MAJOR remains;
whether P7-02 remains intact;
whether E2E-01 remains excluded.
21. Acceptance Criteria
P7-03 is eligible for OWNER_ACCEPT only when:
the security review is documented;
required breaker coverage exists;
existing security controls are not weakened;
no BLOCKER remains unresolved;
no MAJOR remains unresolved unless explicitly owner-accepted;
full regression passes;
compileall passes;
git diff --check passes;
placeholder/TODO scan passes;
secret-like scan passes;
Bridge implementation status is explicitly documented;
every material claim has evidence classification;
Architect Review passes;
P7-02 remains closed and regression-free;
E2E-01 remains excluded.
OWNER_ACCEPT SHALL be performed only by the project owner.
22. Lifecycle
P7-03 follows:
SPEC
  ->
BUILD
  ->
BUILT
  ->
BREAK
  ->
ARCH_REVIEW
  ->
FIX
  ->
REGRESSION
  ->
OWNER_ACCEPT
  ->
DONE
No lifecycle stage may be skipped.
Production changes SHALL NOT be made before a demonstrated finding unless the stage requires a non-production test artifact.
23. Current Status
PHASE = 7
ITEM = P7-03
NAME = Final Security Review

SPEC = COMPLETE
BUILD = NOT_STARTED
BUILT = PENDING
BREAK = PENDING
ARCH_REVIEW = PENDING
FIX = PENDING
REGRESSION = PENDING
OWNER_ACCEPT = PENDING
STATUS = IN_PROGRESS

BRIDGE_IMPLEMENTATION_STATUS = TO_BE_VERIFIED
BRIDGE_SECURITY_VERIFICATION = TO_BE_VERIFIED
24. Explicit Boundary Decisions
The following decisions are binding for P7-03:
Git repository state is authoritative.
P7-02 is closed and SHALL NOT be reopened without new evidence.
P6-03 security correction is owner-accepted.
E2E-01 is excluded from the project path.
The absence of an Android BridgeServer in the repository SHALL be reported rather than hidden or replaced.
P7-03 is a security review, not a feature-development phase.
Security claims require reproducible evidence.
Owner acceptance remains the final authority for phase closure. EOF
printf '%s\n' '--- P7-03 SPEC WRITTEN ---' wc -l docs/phase7/P7-03-FINAL-SECURITY-REVIEW-SPEC.md printf '%s\n' '--- DIFF CHECK ---' git diff --check printf '%s\n' '--- FINAL LINES ---' tail -12 docs/phase7/P7-03-FINAL-SECURITY-REVIEW-SPEC.md
