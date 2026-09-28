# AndroidAgentPro

An autonomous Android phone agent. You give it a goal in natural language
("compute five plus five", "build me a calculator app", "open WhatsApp and
message Ali"), and an LLM brain observes the screen (UI tree + screenshot),
decides the next action, executes it through an on-device bridge, verifies the
effect, recovers when stuck, and verifies the goal before reporting success —
all behind layered budgets, a kill switch, and an owner-confirmation gate.

> **Status:** Security scaffold (Phases 0–6) + full **autonomy v2 loop** are
> implemented and tested (412 unit tests green). The recommended engine is the
> v2 `AutonomousAgent` (default in the CLI); `--legacy` keeps the v1 runner.
> Connect a real device + an LLM API key to drive it for real.

---

## Architecture

```
                +-----------------+        goal
                |   Your goal    |----------+
                +-----------------+          |
                                             v
+------------------+   UI tree + screenshot   +-------------------+
| Android device   |<--------------------------| LLM Brain        |
| (Accessibility + |  tap_element/tap/back/    | (GPT / Gemini /  |
|  BridgeServer.kt)|  input_text/swipe/...      |  Claude / local) |
+------------------|-------------------------->+-------------------+
        ^          |   ActionResult            +--------+----------+
        |          +----------------------------------+            |
        |                                                 v
        |   +----------------------------------------------------------+
        +---| AutonomousAgent (v2): observe -> plan -> act -> verify ->|
            |   verify-goal; recover/replan on failure; SmartRecovery   |
            |   Budgets + LoopGuard + KillSwitch + Trace + Authorization|
            +----------------------------------------------------------+
```

- **agentpro/** — pure-Python agent core (no device needed, fully tested).
  - `screen.py` — observation layer: parses the Kotlin UI-tree contract,
    filters invisible nodes, computes a stable `fingerprint`, and produces a
    compact LLM summary (`summarize_screen`) + smart `wait_for_*`.
  - `planner_v2.py` — hierarchical tool-based planner: decompose the goal into
    subgoals once, then choose the single next tool call per step.
  - `verification.py` — three separate verifiers: **action** (screen deltas +
    text-content checks), **progress** (advancing/repeating/stalled), and
    **goal** (LLM, invoked only on completion signals).
  - `bridge_tools.py` — unified Tool System (`ToolRegistry`, JSON-schema arg
    validation); dangerous tools (`shell`, `install_apk`) are disabled and
    owner-gated by default.
  - `budgets.py` / `memory.py` / `model_manager.py` — hard limits + kill
    switch, working/episodic/failure memory with summarization, and an
    independent LLM layer with retries/fallback/call-counting.
  - `agent_v2.py` — `AutonomousAgent`, the full autonomy loop.
- **python_core/** — the on-host bridge client (HTTP over loopback to the device).
- **android/** — the on-device Kotlin service: `BridgeServer.kt` (HTTP server + auth), `AgentAccessibilityService.kt` (gestures, UI dump with a node cap, input, screenshot, erase), `ScreenshotEngine.kt`.

## What the agent can do (tools)

| Tool | What it does | Bridge command |
|---|---|---|
| `tap_element` | Tap an element by `[N]` index or text from the screen summary (preferred) | `tap` |
| `tap` / `long_press` | Tap / press-and-hold a coordinate | `tap` / `long_press` |
| `back` / `home` / `recents` | System navigation | `back` / `key_event` |
| `input_text` | Type text into the focused field | `input_text` |
| `clear_text` / `erase_text` | Empty the focused field (erase = select-all + cut) | `clear_text` / `erase_text` |
| `swipe` / `scroll` | Swipe / scroll using real screen dims | `swipe` |
| `open_url` | Open a URL in the browser (incl. `data:text/html`) | `open_url` |
| `open_app` | Launch an app by package name | `launch_app` |
| `screenshot` / `dump_ui` / `get_window_info` | Observe the screen | `screenshot` / `ui_dump` / `get_window` |
| `wait` / `wait_for_text` / `wait_for_screen_stable` | Pause / poll until condition | (no bridge call) |
| `shell` / `install_apk` | **Dangerous — disabled + owner-confirmation by default** | (authorization required) |

## Quickstart

### Offline self-test (no device, no API key)

```bash
python3 -m agentpro --self-test
```

Runs the real v2 autonomy loop against a simulated phone that really computes
`5+5`, using element-targeted taps, per-action verification, and LLM goal
verification. Prints a structured report and `SELF-TEST: PASS`.

### Legacy dry-run demo (no device, no API key)

```bash
python3 -m agentpro --dry-run
```

The v1 agent composes a working HTML calculator and opens it via a
`data:text/html` URL.

### Run tests

```bash
python3 -m unittest discover -s tests -q   # 412 tests, all green
python3 -m compileall agentpro python_core
```

### Run for real (device + LLM)

1. Build & install the Android app (see `docs/operations/OPERATIONS.md`).
2. Enable the Accessibility Service on the device.
3. Retrieve the bridge token from the app.
4. Set environment variables:

```bash
export ANDROID_AGENT_PRO_TOKEN=<token from the app>
export AGENTPRO_LLM_API_KEY=<your LLM key>
export AGENTPRO_LLM_MODEL=gpt-4o          # or gemini-2.5-flash, etc.
# Optional: AGENTPRO_LLM_BASE_URL=https://api.openai.com/v1
```

5. Run (the v2 autonomy loop is the default):

```bash
python3 -m agentpro "open Chrome and search for the weather in Cairo"
```

Useful flags: `--max-actions 80`, `--max-model-calls 200`,
`--max-wall-seconds 600`, `--repeat-threshold 4`, `--trace path.jsonl`,
`--kill-file /path/to/KILL` (create it to stop the agent safely),
`--no-screenshot` (text-only), `--legacy` (v1 runner).

> The host running this command must be able to reach `127.0.0.1:8070` on the
> device (e.g. via `adb forward 8070 8070`).

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANDROID_AGENT_PRO_TOKEN` | yes (real run) | — | Bridge auth token (from the app) |
| `AGENTPRO_LLM_API_KEY` | yes (real run) | — | LLM provider API key |
| `AGENTPRO_LLM_MODEL` | no | `gpt-4o-mini` | Model name |
| `AGENTPRO_LLM_BASE_URL` | no | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint (OpenRouter, Ollama, lm-studio) |
| `AGENTPRO_KILL` | no | — | If set (any value), the agent stops immediately |
| `AGENTPRO_MCP` | no | off | Set to `1` to attach remote MCP tools at startup |
| `AGENTPRO_MCP_URL` | no | `https://mcp.higgsfield.ai/mcp` | MCP endpoint (setting it also enables MCP) |
| `AGENTPRO_MCP_INCLUDE` | no | all | Comma-separated shell patterns of remote tools to expose |
| `AGENTPRO_MCP_EXCLUDE` | no | none | Comma-separated patterns to skip |
| `AGENTPRO_MCP_PREFIX` | no | `hf_` | Prefix for the local tool names |
| `AGENTPRO_MCP_TOKEN` | no | — | Bearer token; otherwise `~/.agentpro/mcp_token.json` is used |
| `AGENTPRO_MCP_USER_AGENT` | no | `AndroidAgentPro/1.0` | Some gateways reject the stdlib default |

To use a **local** model (no external API): run
`OLLAMA_HOST=... ollama serve` (OpenAI-compatible at `http://localhost:11434/v1`)
and set `AGENTPRO_LLM_BASE_URL` accordingly with any non-empty `AGENTPRO_LLM_API_KEY`.

## Remote MCP tools (Higgsfield, or any MCP server)

The agent speaks the MCP protocol directly over stdlib HTTP, so image/video
generation tools advertised by a server land in the **same registry** as `tap`
and `screenshot`: the planner sees their JSON schemas, the authorization hook
and trace recorder treat them like any other tool.

**1. Log in once** (Higgsfield is an OAuth-protected resource):

```bash
python3 -m agentpro.mcp_oauth login      # prints a URL; approve it in the phone browser
python3 -m agentpro.mcp_oauth whoami     # token state, scope, expiry
```

`login` registers a public OAuth client dynamically (RFC 7591), starts a loopback
listener on `127.0.0.1:8765`, and exchanges the returned code with PKCE (S256).
The token is stored at `~/.agentpro/mcp_token.json` (mode `0600`) and refreshed
automatically. `logout` removes it. `AGENTPRO_MCP_TOKEN` overrides the file.

**2. Enable it for a run:**

```bash
AGENTPRO_MCP=1 python3 -m agentpro --goal "make a 10s ad video for a coffee shop"
```

MCP is **off by default**, so startup never touches the network; a failing or
unauthenticated endpoint is reported and the agent continues with its local
tools. Use `AGENTPRO_MCP_INCLUDE=hf_*` to keep the catalogue small, and
`--allow-tool <name>` to pre-authorize a specific generated tool.

Programmatic use:

```python
from agentpro.bridge_tools import build_default_registry
from agentpro.mcp_tools import mcp_client_from_env, register_mcp_tools

registry = build_default_registry()                 # or build_default_registry(mcp=...)
names = register_mcp_tools(registry, mcp_client_from_env())
```

## Safety model

- The bridge is bound to `127.0.0.1` and requires a per-install Bearer token
  compared in constant time.
- `shell` and `install_apk` are the **dangerous** tools; they are disabled in
  the registry, cannot execute without an `AuthorizationService`, and require
  an owner confirmation bound to that specific action (typed on the terminal;
  non-interactive sessions fail closed).
- The v2 loop enforces budgets (steps, actions, model calls, wall time), a
  repeat-loop guard, and a kill switch; it can never run away forever.
- Goal success is never assumed from a "finish" token: the LLM goal verifier
  must see screen evidence for the outcome.

## Repository layout

```
agentpro/            # Python agent core (testable)
  agent_v2.py        # AutonomousAgent — full v2 autonomy loop
  screen.py          # observation + screen summary + smart waits
  planner_v2.py      # hierarchical tool-based planning
  verification.py    # action / progress / goal verifiers
  bridge_tools.py    # unified Tool System + dangerous-tool gating
  mcp_client.py      # MCP client: JSON-RPC 2.0 over Streamable HTTP + SSE
  mcp_tools.py       # remote MCP tools -> local Tool objects
  mcp_oauth.py       # OAuth 2.1 (discovery, dynamic registration, PKCE, device flow)
  budgets.py / memory.py / model_manager.py / authorization.py
  __main__.py        # CLI (v2 default): python -m agentpro --self-test
  self_test.py       # offline v2 loop proof (simulated phone)
  demos/calculator_tour.py
python_core/        # on-host bridge client
android/            # on-device Kotlin (BridgeServer, AccessibilityService)
tests/              # 644 unit tests (unittest)
docs/operations/OPERATIONS.md
```

## License

Proprietary — owner-controlled.
