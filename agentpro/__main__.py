"""Command-line entry point: ``python -m agentpro "<goal>"``.

Default engine is the v2 autonomy loop (:class:`agentpro.agent_v2.
AutonomousAgent`) with budgets, a kill switch, structured tracing, owner
confirmation for dangerous actions, and real goal verification.

``--legacy`` keeps the v1 single-planner runner available for backwards
compatibility, and ``--self-test`` proves the v2 loop works offline (no
device, no API key).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_TRACE = Path.home() / ".agentpro" / "trace_v2.jsonl"
DEFAULT_KILL_FILE = Path.home() / ".agentpro" / "KILL"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentpro",
        description="Autonomous Android phone agent.",
    )
    parser.add_argument(
        "goal",
        nargs="?",
        help="What the agent should accomplish (not needed for "
        "--self-test/--dry-run)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=400,
        help="Maximum number of inner loop steps (default: 400)",
    )
    parser.add_argument(
        "--max-actions",
        type=int,
        default=60,
        help="Maximum number of device actions (default: 60)",
    )
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=150,
        help="Maximum number of LLM calls (default: 150)",
    )
    parser.add_argument(
        "--max-wall-seconds",
        type=float,
        default=None,
        help="Hard wall-clock budget in seconds (default: unlimited)",
    )
    parser.add_argument(
        "--repeat-threshold",
        type=int,
        default=4,
        help="Repeated same screen+action count that triggers recovery",
    )
    parser.add_argument(
        "--trace",
        default=str(DEFAULT_TRACE),
        help="Path of the structured trace JSONL (default: %(default)s)",
    )
    parser.add_argument(
        "--kill-file",
        default=str(DEFAULT_KILL_FILE),
        help="Path to a kill-file: creating it stops the agent "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        help="Pre-authorize a gated tool in the registry (dangerous tools "
        "still require per-run owner confirmation). Repeatable.",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Use the legacy v1 runner instead of the v2 autonomy loop",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run a self-contained offline demo (calculator) that needs no "
        "device and no API key.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the v2 autonomy loop offline against a simulated device "
        "(needs no device and no API key).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the LLM model (default: env AGENTPRO_LLM_MODEL)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override the LLM base URL (default: env AGENTPRO_LLM_BASE_URL)",
    )
    parser.add_argument(
        "--api-key-env",
        default="AGENTPRO_LLM_API_KEY",
        help="Environment variable holding the LLM API key",
    )
    parser.add_argument(
        "--token-env",
        default="ANDROID_AGENT_PRO_TOKEN",
        help="Environment variable holding the bridge auth token",
    )
    parser.add_argument(
        "--no-screenshot",
        action="store_true",
        help="Do not send screenshots to the LLM (text-only UI tree)",
    )
    return parser


def _print_report(report) -> None:
    print(f"RESULT: {'SUCCESS' if report.success else 'FAILED'}")
    print(f"REASON: {report.reason}")
    print(f"STEPS: {report.steps}  ACTIONS: {report.actions}  "
          f"MODEL_CALLS: {report.model_calls}")
    print(f"RECOVERIES: {report.recoveries}  REPLANS: {report.replans}  "
          f"FAILURES: {report.failures}")
    if report.subgoals:
        print("SUBGOALS:")
        for i, subgoal in enumerate(report.subgoals):
            print(f"  {i + 1}. {subgoal}")
    if report.goal_evidence:
        print("GOAL EVIDENCE:")
        for evidence in report.goal_evidence:
            print(f"  - {evidence}")
    if report.trace_path:
        print(f"TRACE: {report.trace_path}")
    print(f"ELAPSED: {report.elapsed_s}s")
    print(f"ACTIVE_SUBGOAL: {report.active_subgoal}")


def _run_self_test(args: argparse.Namespace) -> int:
    from .agent_v2 import V2Limits
    from .self_test import GOAL, run_self_test

    print("=== AgentPro self-test (v2 loop, offline, no device/no key) ===")
    print(f"Goal: {GOAL}\n")

    report = run_self_test(
        trace_path=args.trace,
        limits=V2Limits(
            max_steps=args.max_steps,
            max_actions=args.max_actions,
            max_model_calls=args.max_model_calls,
            max_wall_seconds=args.max_wall_seconds,
            repeat_threshold=args.repeat_threshold,
        ),
    )

    _print_report(report)

    print("\nSELF-TEST: " + ("PASS" if report.success else "FAIL"))
    return 0 if report.success else 1


def _run_dry(max_steps: int) -> int:
    from .demos.calculator_tour import build_calculator_demo

    runner, fake_llm, recording_client = build_calculator_demo(
        max_actions=max_steps
    )

    print("=== AgentPro dry-run: calculator tour ===")
    print("Goal: build me a calculator app\n")

    result = runner.run("build me a calculator app")

    print(f"RESULT: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"REASON: {result.reason}\n")

    print("Bridge commands issued by the agent:")
    for cmd, args in recording_client.calls:
        summary = cmd
        if cmd == "open_url":
            url = args.get("url", "")
            shown = url if len(url) <= 80 else url[:77] + "..."
            summary = f"{cmd} url={shown}"
            if url.startswith("data:text/html"):
                summary += "  [calculator HTML rendered in browser]"
        else:
            summary = f"{cmd} {json.dumps(args, ensure_ascii=False)}"
        print(f"  - {summary}")

    print(f"\nLLM calls: {len(fake_llm.calls)}")
    print(
        f"Trace written to: {Path('/tmp/agentpro_calculator_trace.jsonl')}"
    )
    return 0 if result.success else 1


def _build_owner_confirmation_handler(authorization_service, allow_mapping):
    """Ask the owner on the terminal before any dangerous device action.

    Fails closed: non-interactive sessions deny by default.
    """
    def handler(action, request_id):
        from .authorization import AuthorizationError

        request = authorization_service.get_confirmation_request(request_id)
        if request is None:
            return None

        what = f"{action.action_type.value} {dict(action.arguments)}"
        if action.action_type.value in allow_mapping:
            print(f"[owner] pre-authorized dangerous action: {what}")
            use = "y"
        else:
            try:
                use = input(
                    f"[owner] APPROVE dangerous action '{what}'? [y/N] "
                ).strip().lower()
            except EOFError:
                use = "n"

        granted = use in {"y", "yes"}
        try:
            return authorization_service.grant(
                request,
                action,
                granted=granted,
            )
        except AuthorizationError as exc:
            print(f"[owner] authorization failed: {exc}")
            return None

    return handler


def _run_real(args: argparse.Namespace, *, legacy: bool) -> int:
    import os

    try:
        from python_core.bridge_client import BridgeClient
    except ImportError as exc:
        print(
            "ERROR: could not import the bridge client. "
            f"Run from the repository root. ({exc})"
        )
        return 2

    token = os.environ.get(args.token_env, "")
    if not token:
        print(
            f"ERROR: {args.token_env} is not set. Retrieve the bridge token "
            "from the AndroidAgentPro app on the device."
        )
        return 2

    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        print(
            f"ERROR: {args.api_key_env} is not set. Set it to your LLM "
            "provider API key."
        )
        return 2

    try:
        client = BridgeClient(token=token)

        if legacy:
            from .agent_runner import AgentRunner
            from .llm_planner import OpenAICompatibleClient

            llm = OpenAICompatibleClient(
                api_key=api_key,
                base_url=args.base_url,
                model=args.model,
            )
            runner = AgentRunner(
                client,
                llm,
                max_actions=args.max_actions,
                include_screenshot=not args.no_screenshot,
                model=args.model,
            )
            print("=== AgentPro legacy (v1) runner ===")
            result = runner.run(args.goal)
        else:
            from .agent_v2 import AutonomousAgent, V2Limits
            from .authorization import AuthorizationService
            from .budgets import KillSwitch
            from .llm_planner import OpenAICompatibleClient
            from .bridge_tools import build_default_registry

            llm = OpenAICompatibleClient(
                api_key=api_key,
                base_url=args.base_url,
                model=args.model,
            )
            authorization_service = AuthorizationService()
            pre_authorized = set(args.allow_tool)
            owner_handler = _build_owner_confirmation_handler(
                authorization_service,
                pre_authorized,
            )
            registry = build_default_registry()
            from .mcp_tools import attach_mcp_from_env

            mcp = attach_mcp_from_env(registry)
            if mcp.error:
                print(f"MCP unavailable ({mcp.url}): {mcp.error}")
            elif mcp.names:
                print(
                    f"MCP: {len(mcp.names)} tools from {mcp.url} "
                    f"(e.g. {', '.join(mcp.names[:5])})"
                )
            for name in pre_authorized:
                if registry.get(name) is not None:
                    registry.enable(name, enabled=True)

            agent = AutonomousAgent(
                client,
                llm,
                model=args.model,
                trace_path=args.trace,
                limits=V2Limits(
                    max_steps=args.max_steps,
                    max_actions=args.max_actions,
                    max_model_calls=args.max_model_calls,
                    max_wall_seconds=args.max_wall_seconds,
                    repeat_threshold=args.repeat_threshold,
                ),
                include_screenshot=not args.no_screenshot,
                kill_switch=KillSwitch(
                    kill_file=args.kill_file,
                    env_var="AGENTPRO_KILL",
                ),
                authorization_service=authorization_service,
                owner_confirmation_handler=owner_handler,
                registry=registry,
            )
            print("=== AgentPro v2 autonomy loop ===")
            print("[ctrl-c-safe] create the kill file at "
                  f"{args.kill_file} to stop the agent.")
            result = agent.run(args.goal)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    _print_report(result)
    return 0 if result.success else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return _run_self_test(args)

    if args.dry_run:
        return _run_dry(args.max_actions)

    if not args.goal:
        parser.error("a goal is required unless --self-test or --dry-run "
                     "is used")

    return _run_real(args, legacy=args.legacy)


if __name__ == "__main__":
    raise SystemExit(main())