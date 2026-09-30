#!/usr/bin/env python3
"""One-shot live APEX check: drive a real device, log everything, exit.

Run it detached and leave the phone alone:

    setsid nohup python3 tools/apex_live_check.py "<goal>" com.android.settings \\
        > /tmp/apex_live.log 2>&1 &

The whole run is self-contained on purpose. Anything that needs a human to
look at the screen between steps cannot be trusted as a measurement, and the
phone is shared with whatever else the operator is doing.

Progress is appended to the log as it happens, so a partial run is still
evidence: it shows exactly where the chain stopped.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, "/root/AndroidAgentpro2")


def _log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _build_models() -> Any:
    """A real planner over whatever OpenAI-compatible key is present."""
    from agentpro.apex.tiered import OpenAIChatModel, TierConfig, TieredModels
    from agentpro.llm_planner import OpenAICompatibleClient

    providers = (
        (
            "groq",
            "https://api.groq.com/openai/v1",
            os.environ.get("GROQ_API_KEY", ""),
            ("openai/gpt-oss-20b", "llama-3.3-70b-versatile"),
        ),
        (
            "mistral",
            "https://api.mistral.ai/v1",
            os.environ.get("MISTRAL_API_KEY", ""),
            (
                "ministral-8b-latest",
                "codestral-latest",
                "ministral-3b-latest",
                "open-mistral-7b",
            ),
        ),
    )
    for name, base_url, key, models in providers:
        if not key:
            continue
        for model in models:
            try:
                client = OpenAICompatibleClient(
                    api_key=key, base_url=base_url, model=model, timeout=45.0
                )
                # Prove the key and this specific model work before touching
                # the phone: a 403 or a tier error must not be discovered
                # halfway through a run.
                probe = client.complete(
                    [{"role": "user", "content": "reply with the single word: ok"}],
                    max_tokens=8,
                )
            except Exception as exception:  # noqa: BLE001
                _log(f"provider {name} model={model} unavailable: {str(exception)[:90]}")
                continue
            _log(f"planner provider: {name} model={model} probe={str(probe)[:20]!r}")
            adapter = OpenAIChatModel(client, timeout=45.0)
            config = TierConfig(
                fast_model=model,
                big_model=model,
                fast_max_tokens=300,
                big_max_tokens=300,
                fast_temperature=0.0,
                big_temperature=0.0,
            )
            return TieredModels(adapter, config)

    raise SystemExit("no LLM provider available (need a working GROQ or MISTRAL key)")


def main() -> int:
    goal = sys.argv[1] if len(sys.argv) > 1 else "افتح اهتزاز المكالمات"
    package = sys.argv[2] if len(sys.argv) > 2 else "com.android.settings"

    from agentpro.apex.apex_agent import ApexAgent
    from agentpro.apex.bridge_driver import BridgeApexDriver

    driver = BridgeApexDriver()
    client = driver._driver.client
    health = client.health()
    _log(f"bridge health ok={health.ok} size={driver.screen_size()}")

    client.launch_app(package)
    _log(f"launched {package}")
    time.sleep(2.5)

    # Walk back to the app's root screen. launch_app resumes whatever
    # activity was last open, so without this a run can start on a profile
    # from a previous attempt and the "route" it walks is not the real one.
    for _ in range(8):
        state = driver.snapshot()
        activity = str(state.get("activity") or "")
        if state.get("package") != package or ".MainActivity" in activity:
            break
        client.key_event("back")
        time.sleep(1.2)
    time.sleep(1.0)

    first = driver.snapshot()
    _log(
        f"start package={first['package']} activity={first.get('activity')} "
        f"texts={[t for t in first['texts'] if t][:8]}"
    )

    models = _build_models()
    agent = ApexAgent(driver, models, max_steps=14, expect_package=package)

    started = time.monotonic()
    report = agent.run(goal)
    elapsed = time.monotonic() - started

    _log("---- steps ----")
    for step in report.steps:
        _log(
            f"  #{step.index} {step.action:12} {step.result:16} "
            f"{step.seconds:5.2f}s  {step.detail[:70]}"
        )
    _log("---- result ----")
    _log(f"success={report.success} reason={report.reason}")
    _log(f"taps={report.taps} flings={report.flings} steps={len(report.steps)} "
         f"elapsed={elapsed:.1f}s")
    _log(f"model_stats={report.model_stats}")

    out = {
        "goal": goal,
        "package": package,
        "success": report.success,
        "reason": report.reason,
        "taps": report.taps,
        "flings": report.flings,
        "elapsed_seconds": round(elapsed, 1),
        "steps": [
            {
                "index": s.index,
                "action": s.action,
                "result": s.result,
                "detail": s.detail,
                "tier": s.tier,
                "seconds": round(s.seconds, 2),
            }
            for s in report.steps
        ],
        "model_stats": report.model_stats,
        "end_package": driver.snapshot()["package"],
    }
    with open("/tmp/apex_live_result.json", "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=2)
    _log(f"wrote /tmp/apex_live_result.json (success={report.success})")
    return 0 if report.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
