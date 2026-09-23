# AndroidAgentPro — Phase 7 Task Corpus

## 1. Purpose

This document defines the immutable task corpus used for Phase 7
measurement.

The corpus is defined before measurement.

No task in this document contains a measured result.

Measured results MUST be stored separately from this specification.

## 2. Corpus Version

Corpus ID: P7-CORPUS-1
Version: 1.0.0

Task count: 16

Classes:

- SIMPLE: 4
- MEDIUM: 4
- COMPLEX: 4
- PROGRAMMATIC: 4

## 3. Universal Success Rule

A task is successful only when all of the following are true:

1. The task starts from its declared initial state.
2. The planner/execution path produces the required action sequence.
3. Every executed action returns the required successful ActionResult.
4. Required evidence is present.
5. Required verification succeeds.
6. The final goal condition is satisfied.
7. Required Trace events are present.
8. The task does not bypass authorization or verification boundaries.
9. No forbidden side effect occurs.

Planner output alone is never sufficient for success.

## 4. Universal Failure Rules

A task MUST be classified as failed if:

- the final goal condition is not satisfied;
- an action fails without successful recovery;
- required evidence is missing;
- verification rejects the execution;
- a dangerous action executes without valid owner authorization;
- Trace integrity is invalid;
- an unauthorized action is executed;
- a forbidden side effect occurs;
- execution reaches an unrecoverable state limit;
- the execution result contradicts the required goal result.

## 5. Trace Requirements

Each successful task MUST produce Trace evidence containing, as
applicable:

- execution_started
- observation_completed
- plan_created
- action_executed
- goal_verified
- successful terminal state

Tasks involving recovery MUST additionally contain:

- recovery_started
- recovery_finished

Tasks involving dangerous authorization MUST additionally contain:

- dangerous_action_authorization_started
- dangerous_action_authorization_result

Trace MUST NOT contain:

- owner confirmation secrets;
- authentication tokens;
- private authorization material;
- credentials;
- raw sensitive security material.

## 6. Replay Requirements

Every successful task MUST be eligible for replay.

Replay MUST preserve:

- task identifier;
- declared task class;
- expected action sequence;
- required verification;
- required authorization boundary;
- final goal condition.

Replay MUST reject:

- missing required trace fields;
- corrupted records;
- changed action identity;
- changed dangerous-action fingerprint;
- invalid authorization;
- incompatible execution state.

Replay is not allowed to bypass the normal authorization or verification
pipeline.

# 7. SIMPLE TASKS

## P7-S01 — Observe Current Application

Class: SIMPLE

Initial state:

- Agent is operational.
- Observer can return a valid Observation.
- No action is required to establish the initial state.

Goal:

- Obtain a valid current Observation.

Expected behavior:

- execution starts;
- observation succeeds;
- goal verification succeeds;
- execution reaches SUCCESS.

Permitted actions:

- observation only.

Forbidden behavior:

- executing an unrelated action;
- accepting an invalid Observation.

Success evidence:

- valid Observation;
- successful goal verification;
- terminal SUCCESS.

Replay:

- reproduce the same logical observation/verification path.

Failure cases:

- observer exception;
- invalid Observation type;
- unsuccessful goal verification.

---

## P7-S02 — Execute One Safe Action

Class: SIMPLE

Initial state:

- Observer returns a valid Observation.
- Planner returns exactly one safe action.

Goal:

- The safe action executes successfully and the goal verifier accepts
  the resulting state.

Expected action:

- one non-dangerous AgentAction.

Success evidence:

- action result success;
- operation identifier when required by the verifier;
- verified evidence;
- successful goal result.

Forbidden behavior:

- executing more than the declared action;
- skipping verification.

Replay:

- same action type and equivalent declared arguments.

Failure cases:

- action execution failure;
- missing evidence;
- rejected goal verification.

---

## P7-S03 — Empty Plan With Successful Goal

Class: SIMPLE

Initial state:

- Observer returns a valid Observation.
- Planner returns an empty plan.
- Goal verifier reports success.

Goal:

- Executor recognizes that no action is required and completes
  successfully.

Expected behavior:

- no action is executed;
- goal verification is performed;
- execution reaches SUCCESS.

Forbidden behavior:

- synthetic action insertion;
- executing a dangerous action;
- treating an empty plan as automatic success without goal verification.

Replay:

- reproduce the empty-plan and successful-verification path.

Failure cases:

- goal verifier returns failure;
- goal verifier raises an exception.

---

## P7-S04 — Safe Action With Explicit Confirmation

Class: SIMPLE

Initial state:

- Planner returns one safe action with
  `requires_confirmation=True`.

Goal:

- Confirmation is obtained and the action executes successfully.

Required behavior:

- transition through NEED_CONFIRMATION;
- confirmation is explicitly obtained;
- action executes;
- result is verified;
- goal succeeds.

Forbidden behavior:

- executing before confirmation;
- treating planner output as confirmation.

Replay:

- replay MUST preserve the confirmation requirement.

Failure cases:

- confirmation denied;
- action fails;
- verification fails.

# 8. MEDIUM TASKS

## P7-M01 — Multi-Step Safe Sequence

Class: MEDIUM

Initial state:

- Observer is valid.
- Planner returns three safe actions.

Goal:

- All three actions execute successfully in the declared order and the
  final goal is verified.

Required behavior:

- preserve action order;
- record each action;
- verify the final state.

Forbidden behavior:

- action reordering;
- skipped action;
- duplicate action.

Replay:

- reproduce the declared three-action sequence.

Failure cases:

- any action fails;
- sequence order changes;
- final goal verification fails.

---

## P7-M02 — Recovery After Observation Failure

Class: MEDIUM

Initial state:

- First observation attempt fails.
- Recovery handler is capable of recovery.
- Subsequent observation succeeds.

Goal:

- Recover and complete the task successfully.

Required Trace:

- observation_error;
- recovery_started;
- recovery_finished;
- subsequent observation_completed.

Forbidden behavior:

- silently ignoring the failed observation;
- continuing execution without a valid observation.

Replay:

- reproduce the logical failure/recovery path.

Failure cases:

- recovery returns false;
- second observation fails;
- execution continues with invalid observation.

---

## P7-M03 — Recovery After Action Failure

Class: MEDIUM

Initial state:

- First execution attempt fails.
- Recovery succeeds.
- Subsequent execution path succeeds.

Goal:

- Recover and reach the declared goal.

Required behavior:

- action failure is recorded;
- recovery is invoked;
- execution resumes from a valid state;
- final verification succeeds.

Forbidden behavior:

- marking the failed action successful;
- skipping recovery when recovery is required.

Replay:

- preserve the failure/recovery semantics.

Failure cases:

- recovery failure;
- repeated unrecoverable action failure;
- goal verification failure.

---

## P7-M04 — Replanning After Failed Goal Verification

Class: MEDIUM

Initial state:

- Initial plan executes.
- Goal verification fails.
- Replanning produces a valid subsequent plan.

Goal:

- Complete successfully after replanning.

Required behavior:

- failed goal verification is recorded;
- replanning occurs;
- subsequent plan is executed;
- final goal verification succeeds.

Forbidden behavior:

- converting the first failed goal verification into success;
- skipping the replan boundary.

Replay:

- preserve the failed-first-verification and replan sequence.

Failure cases:

- replan failure;
- repeated goal failure;
- state limit exceeded.

# 9. COMPLEX TASKS

## P7-C01 — Recovery and Replanning Chain

Class: COMPLEX

Initial state:

- Initial observation succeeds.
- Initial plan executes partially.
- One action fails.
- Recovery succeeds.
- Goal verification then fails.
- Replanning produces a successful plan.

Goal:

- Reach SUCCESS through the complete recovery and replanning chain.

Required behavior:

- preserve causal ordering;
- record all recovery and replan transitions;
- perform final verification.

Forbidden behavior:

- skipping either recovery or replanning;
- accepting an intermediate state as final success.

Replay:

- reproduce all declared transition categories.

Failure cases:

- missing recovery evidence;
- missing replan evidence;
- invalid final verification.

---

## P7-C02 — Confirmation-Gated Safe and Dangerous Sequence

Class: COMPLEX

Initial state:

- Planner produces a sequence containing safe actions and one
  dangerous `install_apk` action.

Goal:

- Safe actions execute normally.
- Dangerous action executes only after valid owner authorization.
- Final goal is verified.

Required behavior:

- dangerous action reaches NEED_CONFIRMATION;
- AuthorizationService creates a confirmation request;
- owner confirmation is obtained;
- exact authorization is validated;
- dangerous action executes;
- verification succeeds.

Forbidden behavior:

- planner output acting as authorization;
- generic confirmation replacing owner authorization;
- execution after expired or mismatched authorization.

Replay:

- replay MUST revalidate authorization through the current
  AuthorizationService.

Failure cases:

- missing authorization service;
- missing owner handler;
- denied authorization;
- expired confirmation;
- mismatched action fingerprint.

---

## P7-C03 — Verification-Rejection Recovery Boundary

Class: COMPLEX

Initial state:

- Action execution reports success.
- Evidence is intentionally insufficient for verification.

Goal:

- Verification MUST reject the invalid result rather than allowing
  false success.

Expected behavior:

- action execution may report success;
- evidence layer rejects incomplete evidence;
- overall verification is rejected;
- executor does not falsely transition to successful completion.

Forbidden behavior:

- treating ActionResult.success as sufficient;
- bypassing SixLayerVerifier.

Replay:

- reproduce rejection deterministically.

Failure cases:

- verifier accepts incomplete evidence;
- executor reports false success.

---

## P7-C04 — Authorization and Verification Integrity Chain

Class: COMPLEX

Initial state:

- Dangerous action is planned.
- A valid owner confirmation exists for a different action or
  fingerprint.

Goal:

- The dangerous action MUST NOT execute.

Required behavior:

- authorization request is evaluated;
- mismatch is rejected;
- executor fails closed;
- no dangerous action execution occurs.

Forbidden behavior:

- accepting confirmation solely because request_id exists;
- ignoring action/fingerprint mismatch;
- executing after authorization failure.

Replay:

- replay MUST remain rejected.

Failure cases:

- dangerous action executes;
- authorization mismatch is accepted;
- verifier accepts unauthorized execution.

# 10. PROGRAMMATIC TASKS

## P7-P01 — Deterministic Safe Action Pipeline

Class: PROGRAMMATIC

Initial state:

- Programmatically constructed AgentContext.
- Deterministic Observer.
- Deterministic Planner.
- Deterministic ActionExecutor.
- Deterministic GoalVerifier.

Goal:

- Execute a complete deterministic task without Android UI dependence.

Required behavior:

- same inputs produce equivalent logical action sequence;
- successful result is verified;
- Trace is generated.

Replay:

- replay from stored task and Trace metadata.

Failure cases:

- nondeterministic action ordering;
- missing Trace;
- inconsistent final state.

---

## P7-P02 — Trace Persistence and Reload

Class: PROGRAMMATIC

Initial state:

- Temporary filesystem location is available.

Goal:

- Record a deterministic task Trace, close the recorder, reload the
  Trace, and validate the recorded events.

Required behavior:

- JSONL records are valid;
- records survive recorder recreation;
- event ordering is preserved;
- required payload fields remain available.

Forbidden behavior:

- silently accepting malformed JSON;
- silently ignoring invalid required records.

Replay:

- use reloaded records rather than in-memory objects.

Failure cases:

- malformed JSON;
- missing required fields;
- invalid payload type.

---

## P7-P03 — Six-Layer Verification Integrity

Class: PROGRAMMATIC

Initial state:

- Deterministic AgentAction.
- Successful ActionResult.
- Complete verified evidence.
- Successful GoalResult.

Goal:

- SixLayerVerifier accepts the complete valid request.

Required verification layers:

- SCHEMA
- ACTION_RESULT
- SAFETY
- EVIDENCE
- CORRELATION
- GOAL

Forbidden behavior:

- skipping a layer;
- accepting incomplete evidence;
- accepting an invalid operation correlation.

Replay:

- repeat verification with equivalent typed inputs.

Failure cases:

- any required layer is bypassed;
- invalid evidence is accepted;
- invalid correlation is accepted.

---

## P7-P04 — Dangerous Authorization Fail-Closed

Class: PROGRAMMATIC

Initial state:

- Deterministic dangerous `install_apk` action.
- No valid owner confirmation.

Goal:

- Authorization MUST reject the action.

Required behavior:

- dangerous action is identified;
- authorization requirement is enforced;
- action is not executed.

Forbidden behavior:

- implicit authorization;
- planner-generated authorization;
- generic confirmation treated as owner confirmation.

Replay:

- replay MUST remain denied without valid authorization.

Failure cases:

- AuthorizationService grants without valid owner confirmation;
- action executor receives the dangerous action after denial.

## 11. Measurement Contract

The following values are recorded for each task:

- task_id
- corpus_version
- class
- run_reference
- started_at
- completed_at
- success
- failure_code
- final_state
- action_count
- trace_record_count
- replay_eligible
- replay_result
- security_boundary_preserved

No aggregate score is stored inside this corpus definition.

## 12. Class Targets

Simple:

>= 95%

Medium:

>= 85%

Complex:

>= 70%

Programmatic:

>= 60%

These are acceptance thresholds, not rankings.

## 13. Measurement Isolation

The corpus definition MUST NOT be modified after the first official
measurement attempt.

If a task definition requires correction, a new corpus version MUST be
created and the reason MUST be documented.

Results from different corpus versions MUST NOT be silently combined.

## 14. Security Requirements

The corpus MUST NOT require real credentials, real authentication
tokens, private keys, or secrets.

Dangerous-action tests MUST use deterministic test doubles and MUST
not install untrusted software as part of corpus definition.

Authorization tests MUST validate the existing authorization boundary,
not replace it.

## 15. P7-01 Acceptance

P7-01 is complete only when:

1. This corpus is version-controlled.
2. All 16 tasks have stable identifiers.
3. All four classes are represented.
4. Every task has explicit success criteria.
5. Every task has explicit failure conditions.
6. Replay requirements are defined.
7. Trace requirements are defined.
8. Security boundaries are explicit.
9. No measurement result is embedded in the corpus.
10. The corpus can be used as the immutable input for P7-05.

P7-01 does not claim that any task has passed.

The first actual measurements belong to P7-05.
