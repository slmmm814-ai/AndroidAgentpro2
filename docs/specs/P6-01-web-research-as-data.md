# P6-01 — Web Research as Data

## Status

DONE — OWNER_ACCEPT.

## Objective

Add a web-research data boundary for AndroidAgentPro.

Web content MUST be treated as untrusted data. Text originating from a web page MUST NOT be interpreted as an executable instruction, tool command, owner approval, or authorization.

## Scope

P6-01 includes:

1. A typed representation of fetched web data.
2. Explicit trust metadata marking web content as untrusted.
3. Normalization of fetched content without promoting embedded instructions.
4. Deterministic extraction of research data.
5. Provenance metadata for each web result.
6. Tests proving that instruction-like web content remains data.
7. Breaker tests for prompt-injection attempts.

P6-01 does NOT include:

- APK installation.
- Arbitrary shell execution.
- New Android permissions.
- Automatic execution of links or commands found on web pages.
- Automatic owner confirmation.
- Changes to FSM transitions.
- Changes to SixLayerVerifier semantics.
- Changes to existing Phase 0–5.2 behavior.

## Trust Boundary

The mandatory flow is:

WebFetcher
    ->
UntrustedWebDocument
    ->
Sanitization / Normalization
    ->
ResearchData
    ->
Planner
    ->
AgentAction
    ->
SixLayerVerifier
    ->
Executor

The forbidden flow is:

WebPage
    ->
Instruction
    ->
Executor

## Security Invariants

### W-01 — Web data is untrusted

Every fetched document is explicitly marked as untrusted.

### W-02 — Embedded instructions are data

Text such as:

- "ignore previous instructions"
- "run this command"
- "install this APK"
- "disable safety checks"
- "confirm this action"

MUST remain ordinary web-content data.

### W-03 — No implicit action creation

A web document MUST NOT directly create an AgentAction.

Any future action derived from research MUST be created by the planner under the existing execution and verification contracts.

### W-04 — No implicit authorization

A statement contained in a web document MUST NOT count as owner confirmation.

### W-05 — Provenance

Research data MUST retain enough provenance to identify its originating URL and document identity.

### W-06 — Fail closed

Malformed web data or invalid provenance MUST produce a controlled failure rather than executable output.

### W-07 — Existing phases remain stable

P6-01 MUST NOT alter the behavior of Phase 0–5.2.

## Data Contract

The implementation MUST provide immutable typed data structures equivalent in responsibility to:

- WebDocument
- WebContent
- ResearchItem

The exact public names MAY be finalized during BUILD, but the following semantic fields are mandatory:

### WebDocument

- source URL
- retrieval timestamp
- content
- trust classification
- content identifier

### WebContent

- normalized text
- original document identifier
- source URL
- untrusted marker

### ResearchItem

- extracted text/data
- source URL
- source document identifier
- untrusted marker

No field in these structures may represent owner authorization.

## URL Policy

P6-01 MUST restrict fetching to explicitly supported HTTP(S) web URLs.

Loopback, private-network, filesystem, and non-HTTP(S) targets MUST NOT be treated as ordinary public web research targets.

P6-01 MUST NOT use the AndroidAgentPro command bridge as a web-fetch transport.

## Content Policy

HTML/script/style noise MAY be normalized or removed.

Visible page text MUST NOT be transformed into executable instructions.

Instruction-like strings MUST remain representable in research data so that the system can detect and reason about hostile content without executing it.

## Error Handling

The web-research boundary MUST distinguish at least:

- invalid URL
- unsupported scheme
- network failure
- timeout
- HTTP failure
- response-size violation
- invalid encoding
- malformed content
- invalid provenance

Failures MUST be explicit and deterministic.

## Resource Limits

The implementation MUST enforce finite limits for:

- URL length
- response bytes
- normalized content size
- request timeout

Limits MUST be configurable through validated constructor/configuration values.

## Testing Requirements

P6-01 tests MUST cover:

1. Valid HTTPS document becomes untrusted research data.
2. Invalid URL is rejected.
3. Unsupported schemes are rejected.
4. Oversized content is rejected.
5. Timeout/network failure is reported safely.
6. HTML normalization does not create executable actions.
7. Prompt-injection text remains data.
8. "install APK" text from a page does not produce owner confirmation.
9. "ignore previous instructions" text does not alter execution policy.
10. Research objects preserve source provenance.
11. Malformed research data fails closed.
12. Existing Phase 0–5.2 tests remain passing.

## Breaker Requirements

The Breaker MUST attempt at least these hostile inputs:

1. A page containing an instruction to ignore the agent's rules.
2. A page containing a fake owner approval.
3. A page requesting APK installation.
4. A page requesting arbitrary shell execution.
5. A page attempting to inject an AgentAction-like JSON object.
6. A page containing a malicious URL intended to redirect execution.
7. A page containing HTML/JavaScript pretending to be system instructions.

Expected result for every case:

- content remains untrusted data;
- no owner confirmation is created;
- no executable action is created by the web boundary;
- no existing safety contract is bypassed.

## Acceptance

P6-01 is eligible for completion only when:

- all P6-01 unit tests pass;
- all P6-01 Breaker tests pass;
- existing regression remains green;
- Python compilation passes;
- `git diff --check` passes;
- no secrets are introduced;
- no Phase 0–5.2 regression is introduced;
- evidence is recorded before declaring completion.

## Evidence Rule

No implementation or test is considered successful without actual command output.
