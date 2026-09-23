# P6-02 — Research Then Execute

## Status

SPEC — implementation not started.

## Objective

Define a controlled pipeline that allows AndroidAgentPro to use web research as
input data for a phone-execution task while preserving the trust boundary
introduced by P6-01.

The pipeline MUST preserve the distinction between:

1. Untrusted information obtained from the web.
2. Planning decisions made by the agent.
3. Typed executable actions.
4. Explicit owner confirmation for dangerous actions.
5. Evidence produced by execution.
6. Final verification.

The fundamental security rule is:

Web Data → Research/Planning → Candidate Action → Verification → Execution

and MUST NOT become:

Web Data → Instruction → Execution

## Acceptance Criteria

P6-02 is accepted only when all of the following are demonstrated:

1. A research-then-execute-on-phone workflow can be represented end-to-end.
2. Web content remains explicitly untrusted throughout research and planning.
3. A web page cannot create owner confirmation.
4. A web page cannot directly create or authorize an executable action.
5. Dangerous actions require explicit owner confirmation independent of web content.
6. `install_apk` is treated as a dangerous action and cannot execute without the
   required owner confirmation.
7. Confirmation for a dangerous action is bound to the specific action being
   approved and cannot be satisfied by unrelated web text.
8. Execution evidence remains required for successful verification.
9. Existing Phase 0–5.2 behavior remains unchanged unless a separately reviewed
   compatibility-preserving change is proven necessary.
10. P6-02 passes its dedicated unit tests, breaker tests, and full regression.
11. `python -m compileall -q agentpro tests` passes.
12. `git diff --check` passes.
13. No secrets are added to the repository.
14. OWNER_ACCEPT remains the final acceptance decision.

## Scope

P6-02 includes:

- A typed representation for a research-driven execution request.
- Explicit separation between research data and executable actions.
- Provenance preservation from web research into planning context.
- A controlled conversion boundary from research information to candidate
  actions.
- Explicit dangerous-action classification.
- Explicit owner-confirmation state for dangerous actions.
- Binding confirmation to the exact candidate action.
- Tests proving that web content cannot satisfy confirmation requirements.
- Tests proving that web content cannot directly authorize execution.
- Tests for the research-to-action boundary.
- Tests for successful and rejected dangerous-action flows.
- Traceable evidence for the research, planning, confirmation, execution, and
  verification stages.

## Out of Scope

P6-02 does NOT include:

- New Android permissions.
- Arbitrary shell execution.
- Automatic APK installation.
- Automatic acceptance of dangerous actions.
- Bypassing the SixLayerVerifier.
- Replacing the existing FSM.
- Changing the semantics of the existing six verification layers unless a
  compatibility-preserving change is explicitly specified and tested.
- General-purpose web browsing automation.
- Treating arbitrary web links as executable commands.
- Trusting downloaded files merely because a web page recommends them.
- Secret storage changes.
- Cloud execution.
- Remote-control channels.
- Changes to Phase 0–5.2 contracts without explicit architecture review.

## Security Model

### Trust Classes

P6-02 recognizes the following conceptual trust classes:

- `UNTRUSTED_WEB_DATA`
- `PLANNING_DATA`
- `CANDIDATE_ACTION`
- `OWNER_CONFIRMED_ACTION`
- `EXECUTION_EVIDENCE`

The implementation MUST NOT collapse these classes into a single unrestricted
object.

Web research MUST enter the system as `UNTRUSTED_WEB_DATA`.

A planner MAY use untrusted data as information when constructing a candidate
plan, but the resulting candidate action MUST be independently represented as
an executable action.

No trust escalation may occur merely because a web page contains:

- "install"
- "run"
- "execute"
- "approve"
- "confirm"
- "allow"
- JSON resembling an AgentAction
- text pretending to be a system message
- text pretending to be owner approval
- a link to an APK
- a command line
- a security-policy instruction

## Research Boundary

P6-01 provides the web-research boundary.

P6-02 MUST consume the P6-01 typed research objects rather than fetching web
content directly from an execution component.

The research object MUST retain:

- source URL
- source document identifier
- untrusted trust classification
- extracted research text

The planner MAY cite research items but MUST NOT mutate their trust class.

Research provenance MUST NOT be discarded when research is selected for planning.

## Planning Boundary

The planner is responsible for converting a user goal plus research data into
a candidate plan.

The planner MUST NOT treat web text as executable instructions.

The planner MUST construct typed candidate actions using the existing
`AgentAction` contract or a separately specified extension that preserves
backward compatibility.

A candidate action MUST NOT be considered owner-approved merely because it was
derived from a trusted-looking source.

The planner MUST NOT fabricate owner confirmation.

## Dangerous Actions

P6-02 introduces the concept of dangerous-action classification.

`install_apk` MUST be classified as dangerous.

The dangerous-action classification MUST be independent of the text that caused
the action to be proposed.

Dangerous actions MUST require explicit owner confirmation before execution.

The confirmation MUST be represented separately from web research data.

The following MUST NOT count as owner confirmation:

- "I approve" appearing on a web page.
- "User approved this" appearing on a web page.
- A downloaded APK containing approval text.
- A URL parameter containing approval text.
- A JSON object from a web page containing `confirmed: true`.
- A planner rationale claiming that the user approved it.
- A previous unrelated confirmation.
- A confirmation for a different action.
- A confirmation for a different target or artifact.

## Confirmation Binding

Owner confirmation MUST be bound to the exact dangerous action.

At minimum, the binding MUST distinguish:

- action type
- action arguments or target
- confirmation request identity
- confirmation decision

A confirmation for one dangerous action MUST NOT authorize another action.

A confirmation for one APK MUST NOT automatically authorize another APK.

A confirmation obtained for a non-dangerous action MUST NOT authorize a dangerous
action.

The implementation MUST fail closed when confirmation binding is incomplete,
ambiguous, stale, malformed, or mismatched.

## `install_apk` Contract

P6-02 defines `install_apk` as a dangerous-action contract.

The exact executable implementation is a BUILD concern, but the SPEC requires
that any implementation satisfy all of the following:

1. The action is explicitly typed.
2. The action is classified as dangerous.
3. The action contains a specific target artifact reference.
4. The action cannot execute without owner confirmation.
5. Web content cannot supply that confirmation.
6. The confirmation is bound to the exact installation request.
7. Verification remains mandatory after execution.
8. Failed or missing confirmation results in rejection or a confirmation-required
   state, never silent execution.
9. An installation recommendation from a web page is data only until an
   independent planning and confirmation path exists.

## Existing Six-Layer Verifier

P6-02 MUST preserve the existing fail-closed verifier model.

The following properties remain mandatory:

- schema validation
- action-result validation
- safety validation
- evidence validation
- operation correlation
- goal validation

Dangerous-action confirmation MUST be compatible with the existing safety
layer.

A dangerous action MUST NOT be accepted merely because its execution result
reports success.

Successful verification still requires the required execution evidence and
correlation.

If a verifier extension is necessary, it MUST preserve all existing Phase 3
acceptance behavior and MUST be accompanied by regression tests.

## Execution Boundary

Only the execution layer may invoke an executable action.

Research data MUST never be passed directly to the execution interface as an
instruction string.

The executor MUST receive a typed action rather than raw web content.

A dangerous action reaching the execution boundary without valid confirmation
MUST fail closed.

## Evidence Boundary

Every successful executable action MUST produce execution evidence sufficient
for the existing verifier contract.

For compatibility with the existing verifier, successful evidence MUST include:

- `operation_id`
- `verified: true`

The evidence MUST correspond to the actual operation being verified.

Research provenance and execution evidence are distinct concepts and MUST NOT
be substituted for one another.

A source URL is not execution evidence.

A web page stating that an action succeeded is not execution evidence.

## Trace Requirements

P6-02 MUST provide trace visibility for:

1. research received
2. research item selected
3. planning started
4. candidate action created
5. dangerous classification, when applicable
6. confirmation requested
7. confirmation granted or denied
8. execution started
9. execution result
10. verification result

Trace entries MUST NOT expose authentication tokens or other secrets.

Trace data MUST identify the relationship between a candidate action and its
research provenance without promoting the research data to trusted commands.

## Failure Handling

The system MUST fail closed for:

- malformed research objects
- missing provenance
- trust-class violations
- malformed candidate actions
- unknown dangerous-action classification
- missing confirmation
- mismatched confirmation
- stale confirmation
- malformed execution evidence
- missing operation correlation
- verifier errors

An error MUST NOT be converted into successful execution by fallback logic.

Recovery MAY retry observation or planning where compatible with existing
Phase 5.2 behavior, but recovery MUST NOT bypass confirmation or verification.

## Resource and Boundary Constraints

P6-02 MUST respect existing project limits.

The implementation MUST NOT silently increase:

- action budgets
- replan budgets
- recovery budgets
- request size limits
- response size limits

Any required limit change must be separately specified and tested.

## Compatibility Requirements

The implementation MUST preserve:

- `AgentAction` immutability semantics.
- existing `ActionType` behavior.
- existing MetaPlanner validation behavior.
- existing HierarchicalPlanner behavior.
- existing FSM limits and transitions.
- existing SixLayerVerifier acceptance behavior.
- existing BridgeClient protocol behavior.
- P6-01 web research trust semantics.

No existing Phase 0–5.2 test may be weakened, deleted, or bypassed.

## Required Test Matrix

### Core Tests

The implementation MUST test:

1. valid research enters as untrusted data.
2. research provenance is preserved.
3. research cannot mutate its trust classification.
4. a planner can consume research data.
5. a candidate action is distinct from research text.
6. a dangerous action is classified correctly.
7. missing confirmation is rejected.
8. valid confirmation permits the confirmation stage.
9. confirmation is bound to the exact action.
10. confirmation for a different action is rejected.
11. confirmation for a different APK is rejected.
12. successful execution still requires evidence.
13. evidence correlation remains mandatory.
14. final goal verification remains mandatory.

### Breaker Tests

The breaker suite MUST attempt at least:

1. web page says "owner approved this installation".
2. web page contains `confirmed=true`.
3. web page contains AgentAction-like JSON.
4. web page contains a shell command.
5. web page contains an APK installation command.
6. web page impersonates the system prompt.
7. web page attempts to change the safety policy.
8. web page attempts to bypass confirmation.
9. web page supplies confirmation for APK A while execution targets APK B.
10. web page supplies confirmation for a different action.
11. stale confirmation is replayed against a new action.
12. malformed confirmation is treated as approval.
13. successful remote/web text is treated as execution evidence.
14. source URL is substituted for execution evidence.
15. research provenance is forged or mismatched.

Every breaker MUST fail closed.

## Required Evidence Before OWNER_ACCEPT

P6-02 cannot be marked BUILT until the following evidence exists:

- dedicated unit tests pass
- dedicated breaker tests pass
- full regression passes
- compileall passes
- `git diff --check` passes
- no secrets are present
- Git working tree is reviewed
- architecture review confirms that P6-01 trust semantics remain intact

The implementation status remains:

`SPEC → BUILD → BUILT → BREAK → ARCH_REVIEW → FIX → REGRESSION → OWNER_ACCEPT → DONE`

The assistant MUST NOT mark OWNER_ACCEPT. Only the project owner may do so.

## Non-Goals

P6-02 is not intended to make the agent trust the web.

It is intended to make the agent capable of using web information while
maintaining a hard separation between information, planning, authorization,
execution, and verification.

The core invariant is:

`Web content is data. Owner confirmation is an independent authorization.
Executable actions are typed. Verification is mandatory.`

## Core Security Invariant

The implementation MUST preserve the following invariant:

`Web content is data. Owner confirmation is an independent authorization.
Executable actions are typed. Verification is mandatory.`

## Specification Closure

This document defines the P6-02 architecture and acceptance boundary only.

Implementation details such as exact class names, exact module placement, exact
confirmation-token representation, and whether an existing action enum requires
a backward-compatible extension belong to BUILD and MUST be selected without
weakening this specification.

Status remains:

`SPEC — implementation not started.`
