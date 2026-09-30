# APEX — Adaptive Phone EXecution

APEX is the reliability layer that sits on top of the v2 bridge. It does not
replace the v2 loop; it removes the two failure modes that make a phone agent
unreliable in practice: **guessing where to tap**, and **paying full model
latency on every step**.

The v2 loop is rule-first: anything the deterministic layers can answer is
never sent to a model.

---

## The five layers

| Layer | Module | What it owns | Model in the loop? |
|---|---|---|---|
| Physics | `agentpro/apex/physics.py` | fling until end, scroll until a text appears, wait-until-stable | never |
| App map | `agentpro/apex/appmap.py` | which screens exist, scroll position, "already at the bottom", known transitions | never |
| Grounder | `agentpro/apex/grounder.py`, `vision.py` | resolve a target to a coordinate — UI tree first, vision only as fallback | only on fallback |
| Tiered models | `agentpro/apex/tiered.py` | one fast model per step, a big model for planning/recovery/verification | only when needed |
| Agent loop | `agentpro/apex/apex_agent.py` | observe → act → verify, with the rules above deciding the order | rarely |

Two adapters connect them to a real device:

- `agentpro/apex/bridge_physics.py` — `PhysicsDriver` over `FastDriver`
- `agentpro/apex/bridge_driver.py` — the full `ApexDriver` for the live bridge

---

## Running it

```bash
export ANDROID_AGENT_PRO_TOKEN=...        # bridge auth
export AGENTPRO_LLM_API_KEY=...           # planner
export AGENTPRO_LLM_FAST_MODEL=...        # routine steps
export AGENTPRO_LLM_BIG_MODEL=...         # planning / recovery / verification

python3 -m agentpro.apex.run "open the chat named Team"
```

Useful flags: `--max-steps`, `--max-repeat`, `--kill-file PATH`, `--no-vision`,
`--json`.

---

## Why the layers are ordered this way

**Rule 1 — tap what the tree already names.** A label match in the
accessibility tree is exact, costs no model call, and is the only input path
with 100% known accuracy. Tapping is therefore never a model decision when the
tree can answer it.

Candidate selection prefers, in order: an exact clickable match, a clickable
partial match, an exact non-clickable label, and only then any partial match.
Accessibility trees repeat labels on inert wrappers (hint bars, tooltips,
disabled rows), and a plain substring match will happily return one of those.

**Rule 2 — scroll physically, not statistically.** `scroll_to_text` flings
until the label appears or the list stops moving. A fling that does not change
the fingerprint means the list ended, which is what `AppMap` records as "seen
bottom" so the planner is never asked something the device already answered.

**Rule 3 — only then ask the big model.** The planner receives a single JSON
action (`scroll`, `back`, `type`, `tap`, `done`) and the loop **executes it**.
Prose that merely mentions the target is not an action and is not success.

---

## A tap is not proof

The loop is `observe → act → verify`. After every action it waits for the UI to
settle, re-reads the screen, and only reports success with evidence:

1. the screen reacted at all (the fingerprint changed), **and**
2. the new screen carries the target's name — a chat or detail view shows the
   thing you just opened, so this is deterministic evidence that costs no
   model call.

If the screen changed but shows no such evidence, the big model is asked to
verify. A failed check, a silent model, or an unparsable answer is treated as
**not reached** — never as success. A tap that changes nothing is remembered,
so the loop stops hammering a dead widget and escalates to scrolling and
planning instead.

`ScrollToTextResult` therefore reports `moved` and `final_fingerprint`
separately from `found`: "the text was not there" says nothing about whether the
list moved, and conflating the two puts a wrong "bottom of list" into the app
map.

---

## Tiered models

Routing is conservative — anything uncertain goes to the big model:

- **big**: `plan`, `decompose`, `replan`, `recover`, `verify_goal`, `summarize`
- **fast**: `next_action`, `classify`, `extract`, `label`, `grounding`
- **unknown purpose** → big

`TieredModels` passes the concrete model name to the client
(`OpenAIChatModel` overrides the client's default model per call, along with
`max_tokens` and `temperature`). A tier split that never reaches the provider is
just a config field that lies, so the routing is covered by tests that assert
the model name the client actually received.

Per-tier call counts and latency are recorded in `TierStats`, so the speed-up is
measurable rather than claimed.

---

## Grounding

`HybridGrounder` resolves a target against UI-tree candidates first and only
asks the vision model when the tree cannot name it. When it does fall back to
vision, the point is snapped to the nearest candidate only within
`snap_threshold_px`, and never beyond `max_snap_px`.

The tree-first path is measurably better than the general-purpose model: on a
live Telegram screen the tree located a chat row exactly (`confidence=1.0`,
zero vision calls), while `gemini-3.6-flash` pointed ~200px away from the real
bounds. Vision is a fallback, not the default.

---

## Device constraints worth knowing

- **Screenshots are rate-limited to one per 250 ms** by the on-device
  `ScreenshotEngine`. `BridgeClient.screenshot()` waits the interval out and
  retries instead of surfacing `SCREENSHOT_CAPTURE_FAILED`.
- **Screen size comes from the window, not the UI tree.** Tree bounds only
  cover the content area, so deriving the display height from them makes every
  scroll distance short. `BridgeClient.screen_size()` reports the real
  `1080×2400`.
- The live UI tree is nested under `root.children`, not a flat `nodes` list.

---

## Tests

```bash
python3 -m unittest discover -s tests -p "test_apex_*.py"   # 117 APEX tests
python3 -m unittest discover -s tests                        # 814 total
```

The suite asserts the behaviours that matter rather than the call sequence: an
inert tap must not be reported as success, planner chatter is not an action, a
failed verification is not a success, a dead target is not tapped twice, and a
verifier that says "not done" is believed.
