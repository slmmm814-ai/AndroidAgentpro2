# OPERATIONS.md — AndroidAgentPro operations guide

This document covers setting up the device, the bridge, the LLM, and
troubleshooting the autonomous agent.

---

## 1. Device setup

### 1.1 Build & install the APK

The app lives in `android/`. Build with the Android Gradle plugin 8.7.3 and
Kotlin 2.0.21 (compile SDK 35, min SDK 24):

```bash
cd android
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

CI build: `.github/workflows/android-build.yml`.

### 1.2 Enable the Accessibility Service

1. Open **AndroidAgentPro** from the launcher.
2. The app guides you to **Settings → Accessibility → AndroidAgentPro**. Enable it.
3. Confirm the service is connected (the app shows a status indicator).

The service (`AgentAccessibilityService`) hosts the bridge server and
performs gestures, UI dumps, text input, screenshots, and global key events.

### 1.3 Retrieve the bridge token

The app generates a per-install token (stored securely, never logged). On the
app screen, copy the **Bridge Token**. Set it on the host:

```bash
export ANDROID_AGENT_PRO_TOKEN=<paste token>
```

### 1.4 Forward the port to the host

The bridge listens on `127.0.0.1:8070` on the device. From the host:

```bash
adb forward tcp:8070 tcp:8070
```

Verify:

```bash
python3 python_core/health_check.py    # prints BRIDGE_OK + latency
```

---

## 2. LLM setup

The agent needs a vision-capable model (it sends a screenshot + UI tree).

### 2.1 OpenAI / OpenRouter / any OpenAI-compatible API

```bash
export AGENTPRO_LLM_API_KEY=sk-...
export AGENTPRO_LLM_MODEL=gpt-4o          # or gpt-4o-mini, gemini-..., etc.
# OpenRouter example:
# export AGENTPRO_LLM_BASE_URL=https://openrouter.ai/api/v1
```

### 2.2 Local model (Ollama / lm-studio)

```bash
ollama serve                              # OpenAI-compatible at :11434/v1
export AGENTPRO_LLM_BASE_URL=http://localhost:11434/v1
export AGENTPRO_LLM_API_KEY=local          # any non-empty string
export AGENTPRO_LLM_MODEL=llama3.2-vision  # or qwen2-vl, minicpm-v, etc.
```

### 2.3 Offline / no key

Two offline paths, both self-contained:

```bash
python3 -m agentpro --self-test     # real v2 loop against a simulated phone (5+5)
python3 -m agentpro --dry-run       # legacy v1 calculator rendering demo
```

You can also write your own `FakeLLMClient` script for deterministic testing.

---

## 3. Running the agent

```bash
python3 -m agentpro "<goal>" [options]
```

The default engine is the v2 `AutonomousAgent` (observe → plan → act → verify →
verify-goal, with recovery, budgets, trace, and kill switch). `--legacy`
switches to the v1 runner.

| Option | Default | Purpose |
|---|---|---|
| `goal` (positional) | — | What the agent should accomplish |
| `--max-steps N` | 400 | Max inner-loop steps |
| `--max-actions N` | 60 | Max device actions |
| `--max-model-calls N` | 150 | Max LLM calls (incl. planner, verifier, goal checks) |
| `--max-wall-seconds N` | unlimited | Hard wall-clock budget |
| `--repeat-threshold N` | 4 | Same screen+action count that triggers recovery |
| `--trace PATH` | `~/.agentpro/trace_v2.jsonl` | Structured JSONL trace |
| `--kill-file PATH` | `~/.agentpro/KILL` | Create it to stop the agent safely |
| `--allow-tool NAME` | — | Pre-authorize a gated tool (dangerous tools still require per-run owner confirmation); repeatable |
| `--self-test` | off | Offline v2 loop proof (no device/key) |
| `--dry-run` | off | Legacy calculator demo (no device/key) |
| `--legacy` | off | Use the v1 runner instead of v2 |
| `--model NAME` | env | Override the LLM model |
| `--base-url URL` | env | Override the LLM endpoint |
| `--api-key-env VAR` | `AGENTPRO_LLM_API_KEY` | Env var holding the LLM key |
| `--token-env VAR` | `ANDROID_AGENT_PRO_TOKEN` | Env var holding the bridge token |
| `--no-screenshot` | off | Send UI tree only (text mode) |

Examples:

```bash
python3 -m agentpro "open WhatsApp and send 'hi' to Ali" --max-actions 80
python3 -m agentpro "search YouTube for lofi hip hop and open the first result"
python3 -m agentpro "build me a calculator app"        # real LLM builds + opens HTML calculator
```

Dangerous tools (`shell`, `install_apk`) stay disabled unless the agent run is
wired to an `AuthorizationService`, and even then they need a per-run owner
confirmation typed at the terminal (non-interactive sessions are refused).

---

## 4. Bridge command reference

All commands are `POST /v1/command` with JSON body
`{"protocol":"ultimate","version":"1.0","request_id":<uuid>,"command":<str>,"args":<obj>}`
and `Authorization: Bearer <token>`. Responses mirror the protocol.

| Command | Args | Returns | Needs accessibility |
|---|---|---|---|
| `health` | — | `server_running`, `accessibility_connected` | no |
| `ui_dump` | — | `root` (UI tree), `truncated` (node cap hit) | yes |
| `get_window` | — | window info (activity, title, geometry) | yes |
| `screenshot` | — | `base64`, `width`, `height` | yes |
| `tap` | `x`, `y` | `operation_id` | yes |
| `long_press` | `x`, `y`, `duration_ms` | `operation_id` | yes |
| `back` | — | `operation_id` | yes |
| `input_text` | `text` | `operation_id` | yes (focused field) |
| `clear_text` | — | `operation_id` | yes (focused field) |
| `erase_text` | — | `operation_id` | yes (focused field; select-all + cut) |
| `swipe` | `x1`,`y1`,`x2`,`y2`,`duration_ms` | `operation_id` | yes |
| `key_event` | `keycode` | `operation_id` | yes |
| `open_url` | `url` (http(s) or `data:text/html`) | `dispatched` | no |
| `launch_app` | `package` | `dispatched` | no |

`ui_dump` caps the tree at 2,000 nodes; larger trees set `"truncated": true`,
which the Python observer surfaces as `[tree truncated]` in the screen summary.

### Supported `key_event` codes

`back`, `home`, `recents` (alias `overview`), `notifications`,
`quick_settings`, `power_dialog`. Other codes return `UNSUPPORTED_KEYCODE`
(`enter`/`delete`/`volume` are NOT supported — use `input_text`/`erase_text`
for editing).

### `data:text/html` URLs

`open_url` with a `data:text/html` payload is the way the agent "builds an app"
without installing anything. The Kotlin bridge decodes the payload, writes it
to the app cache, and launches it via a `FileProvider` content URI (the browser
renders it immediately).

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Bridge refused the connection` | App not running or port not forwarded | `adb forward 8070 8070`; confirm the app shows "connected" |
| `UNAUTHORIZED` | Token mismatch | Re-copy the token from the app; check `ANDROID_AGENT_PRO_TOKEN` |
| `ACCESSIBILITY_NOT_CONNECTED` | Accessibility service disabled | Re-enable in Settings → Accessibility |
| `INPUT_NO_FOCUS` | No focused editable field | Tap the field first so it has focus |
| `UNSUPPORTED_KEYCODE` | Key not in the supported set | Use a supported keycode (see above) |
| `OPEN_URL_FAILED` | No browser / no handler | Install a browser; check `<queries>` in manifest |
| `PACKAGE_NOT_FOUND` | Package has no launcher activity | Use the correct package name (`adb shell pm list packages`) |
| `LLM HTTP 401` | Bad API key | Check `AGENTPRO_LLM_API_KEY` |
| Loop never finishes | LLM never emits `finish` | Lower `--max-steps`; check the trace |
| Agent "stuck" / repeats | LLM loops on the same screen | Increase `--max-steps`, use a stronger model, or press Back via recovery |

### Trace

The v2 engine appends JSONL events to `~/.agentpro/trace_v2.jsonl` (override
with `--trace`). Inspect it to see every state transition, action, verification
result, and error. The dry-run writes to `/tmp/agentpro_calculator_trace.jsonl`.

---

## 6. Port & binding

- Host: `127.0.0.1`, port `8070`, path `/v1/command`.
- The server is single-endpoint; only `POST /v1/command` is served.
- Max request body 1 MB; max response body 4 MB; gesture timeout 5 s; socket
  read timeout 10 s.

---

## 7. Re-establishing a session

1. Read `STATE.md`.
2. `git status` / latest tags.
3. Run `python3 -m pytest tests/ -q` to confirm the core is intact.
4. Re-forward the port, re-export the env vars, and resume.
