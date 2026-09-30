"""APEX CLI entry point — run the integrated loop against the live bridge.

Usage::

    python3 -m agentpro.apex.run "open the chat named Team"

This wires the whole APEX layer together through ``BridgeApexDriver``:
deterministic physics primitives, the app-state map, the hybrid grounder
(tree-first, vision fallback) and the tiered models.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_google_key() -> str | None:
    """Read the Google API key from the opencode provider config, if present."""
    for path in (
        os.path.expanduser("~/.config/opencode/opencode.jsonc"),
        os.path.expanduser("~/.config/opencode/opencode.json"),
    ):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            raw = "\n".join(
                line for line in raw.splitlines()
                if not line.strip().startswith("//")
            )
            cfg = json.loads(raw, strict=False)
            key = (
                cfg.get("provider", {})
                .get("google", {})
                .get("options", {})
                .get("apiKey")
            )
            if key:
                return str(key)
        except Exception:  # noqa: BLE001 - best effort only
            continue
    return None


def _build_vision_grounder(args: argparse.Namespace):
    """Construct a Gemini-backed vision grounder when a key is available."""
    if args.no_vision:
        return None
    key = args.gemini_key or _load_google_key()
    if not key:
        return None
    from .vision import GeminiConfig, GeminiGrounder

    return GeminiGrounder(GeminiConfig(api_key=key, model=args.gemini_model))


def _build_models():
    """Build ``TieredModels`` from the LLM environment, or ``None``.

    ``AGENTPRO_LLM_FAST_MODEL`` / ``AGENTPRO_LLM_BIG_MODEL`` select the tiers;
    both fall back to ``AGENTPRO_LLM_MODEL``.
    """
    from ..llm_planner import OpenAICompatibleClient
    from .tiered import OpenAIChatModel, TierConfig, TieredModels

    api_key = os.environ.get("AGENTPRO_LLM_API_KEY")
    if not api_key:
        return None

    base_url = os.environ.get("AGENTPRO_LLM_BASE_URL") or "https://api.openai.com/v1"
    default_model = os.environ.get("AGENTPRO_LLM_MODEL", "gpt-4o-mini")
    fast_model = os.environ.get("AGENTPRO_LLM_FAST_MODEL", default_model)
    big_model = os.environ.get("AGENTPRO_LLM_BIG_MODEL", default_model)

    # one transport, two model names — the router decides which tier is asked
    client = OpenAIChatModel(OpenAICompatibleClient(api_key, base_url, fast_model))

    return TieredModels(client, TierConfig(fast_model=fast_model, big_model=big_model))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentpro.apex.run",
        description="Run the APEX loop against the live bridge.",
    )
    parser.add_argument(
        "goal",
        help='natural-language goal, e.g. "open the chat named Team"',
    )
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--max-repeat", type=int, default=3)
    parser.add_argument("--kill-file", default=None)
    parser.add_argument(
        "--app",
        default=None,
        help=(
            "package the run must stay inside, e.g. com.instagram.android. "
            "Success is only accepted while this app is in the foreground."
        ),
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help=(
            "drive the app with node actions instead of screen gestures, so "
            "the app never has to be in the foreground. Requires --app."
        ),
    )
    parser.add_argument(
        "--no-vision", action="store_true", help="disable the vision fallback"
    )
    parser.add_argument(
        "--gemini-key",
        default=None,
        help="Google API key for the vision grounder (default: read from opencode config)",
    )
    parser.add_argument("--gemini-model", default="gemini-3.6-flash")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if not os.environ.get("ANDROID_AGENT_PRO_TOKEN"):
        print("error: ANDROID_AGENT_PRO_TOKEN is not set", file=sys.stderr)
        return 2

    from .apex_agent import ApexAgent
    from .bridge_driver import BridgeApexDriver

    if args.background:
        if not args.app:
            print(
                "error: --background requires --app",
                file=sys.stderr,
            )
            return 2

        from ..background.node_driver import NodeActionDriver
        from python_core.bridge_client import BridgeClient

        driver = NodeActionDriver(BridgeClient(), package=args.app)
    else:
        driver = BridgeApexDriver(vision_grounder=_build_vision_grounder(args))

    models = _build_models()
    if models is None:
        print(
            "error: no LLM configuration found (set AGENTPRO_LLM_API_KEY)",
            file=sys.stderr,
        )
        return 2

    agent = ApexAgent(
        driver,
        models,
        max_steps=args.max_steps,
        max_repeat=args.max_repeat,
        kill_file=args.kill_file,
        expect_package=args.app,
    )
    report = agent.run(args.goal)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"APEX result: success={report.success}")
        print(f"reason: {report.reason}")
        print(f"taps={report.taps} flings={report.flings} steps={len(report.steps)}")
        print(f"duration={report.duration_seconds:.1f}s")
        print(f"models: {models.summary()}")
        for step in report.steps:
            print(f"  #{step.index} {step.action} [{step.tier}] {step.detail}")

    return 0 if report.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
