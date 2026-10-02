from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .executor import AgentExecutor, ExecutionResult
from .fsm import AgentFSM, FSMLimits
from .llm_planner import LLMClient, LLMPlanner
from .models import (
    ActionResult,
    ActionType,
    AgentAction,
    AgentContext,
    GoalResult,
    Observation,
)
from .trace import TraceRecorder


@dataclass
class FakeResponse:
    """Minimal bridge response shape (duck-typed by observer/executor)."""

    data: dict | None = None
    ok: bool = True
    request_id: str = "fake"


class RecordingBridgeClient:
    """
    In-memory bridge client for offline demos and tests.

    Records every command (with its arguments) and returns canned successful
    responses. No network or device is required. It duck-types the same method
    surface as ``python_core.bridge_client.BridgeClient``.
    """

    def __init__(
        self,
        *,
        ui_root: Mapping[str, Any] | None = None,
        screenshot_base64: str = "ZmFrZQ==",
    ) -> None:
        self._ui_root = (
            ui_root
            if isinstance(ui_root, Mapping)
            else {
                "class": "android.widget.FrameLayout",
                "package": "com.android.launcher3",
                "children": [
                    {
                        "class": "android.widget.TextView",
                        "text": "Calculator",
                        "bounds": "[100,200][400,260]",
                        "clickable": True,
                    }
                ],
            }
        )
        self._screenshot = screenshot_base64
        self.calls: list[tuple[str, dict]] = []
        self._next_op = 0

    def _record(self, command: str, args: Mapping[str, Any] | None = None) -> FakeResponse:
        self.calls.append((command, dict(args or {})))
        self._next_op += 1
        return FakeResponse(data={"operation_id": self._next_op})

    def health(self) -> FakeResponse:
        return FakeResponse(
            data={"server_running": True, "accessibility_connected": True}
        )

    def ui_dump(self) -> FakeResponse:
        self.calls.append(("ui_dump", {}))
        return FakeResponse(data={"root": dict(self._ui_root)})

    def screenshot(self) -> FakeResponse:
        self.calls.append(("screenshot", {}))
        return FakeResponse(
            data={
                "base64": self._screenshot,
                "width": 1080,
                "height": 2400,
                "format": "jpeg",
            }
        )

    def take_photo(self, facing: str = "front") -> FakeResponse:
        self.calls.append(("take_photo", {"facing": facing}))
        return FakeResponse(
            data={
                "base64": self._screenshot,
                "facing": facing,
                "width": 1280,
                "height": 720,
                "format": "jpeg",
                "byte_count": len(self._screenshot) if self._screenshot else 0,
            }
        )

    def shizuku_status(self) -> FakeResponse:
        self.calls.append(("shizuku_status", {}))
        return FakeResponse(data={"available": True, "has_permission": True})

    def shizuku_shell(self, command: str) -> FakeResponse:
        self.calls.append(("shizuku_shell", {"command": command}))
        return FakeResponse(data={"exit_code": 0, "output": "mock output", "error": ""})

    def ghost_start(self) -> FakeResponse:
        self.calls.append(("ghost_start", {}))
        return FakeResponse(data={"display_id": 2, "status": "ACTIVE"})

    def ghost_stop(self) -> FakeResponse:
        self.calls.append(("ghost_stop", {}))
        return FakeResponse(data={"status": "STOPPED"})

    def tap(self, x: float, y: float) -> FakeResponse:
        return self._record("tap", {"x": x, "y": y})

    def back(self) -> FakeResponse:
        return self._record("back", {})

    def input_text(self, text: str) -> FakeResponse:
        return self._record("input_text", {"text": text})

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration_ms: int = 300,
    ) -> FakeResponse:
        return self._record(
            "swipe",
            {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "duration_ms": duration_ms,
            },
        )

    def long_press(
        self,
        x: float,
        y: float,
        duration_ms: int = 600,
    ) -> FakeResponse:
        return self._record(
            "long_press",
            {"x": x, "y": y, "duration_ms": duration_ms},
        )

    def clear_text(self) -> FakeResponse:
        return self._record("clear_text", {})

    def erase_text(self) -> FakeResponse:
        return self._record("erase_text", {})

    def get_window(self) -> FakeResponse:
        self.calls.append(("get_window", {}))
        return FakeResponse(
            data={
                "package_name": "com.android.launcher3",
                "activity_name": "launcher",
                "window_title": "Launcher",
            }
        )

    def open_url(self, url: str) -> FakeResponse:
        return self._record("open_url", {"url": url})

    def key_event(self, keycode: str) -> FakeResponse:
        return self._record("key_event", {"keycode": keycode})

    def launch_app(self, package: str) -> FakeResponse:
        return self._record("launch_app", {"package": package})

    @property
    def open_urls(self) -> list[str]:
        return [args["url"] for cmd, args in self.calls if cmd == "open_url"]

    @property
    def commands(self) -> list[str]:
        return [cmd for cmd, _ in self.calls]


class BridgeObserver:
    """Observes the device by fetching the UI tree and a screenshot."""

    def __init__(self, client: Any, *, include_screenshot: bool = True) -> None:
        self._client = client
        self._include_screenshot = include_screenshot

    def observe(self) -> Observation:
        ui_root = None
        package_name = None
        try:
            resp = self._client.ui_dump()
            if resp is not None and resp.data:
                root = resp.data.get("root")
                if isinstance(root, Mapping):
                    ui_root = root
                    package_name = root.get("package") or root.get(
                        "package_name"
                    )
        except Exception as exc:
            return Observation(
                success=False,
                error_code="OBSERVE_UI_DUMP_FAILED",
                error_message=str(exc),
            )

        screenshot_b64 = None
        if self._include_screenshot:
            try:
                shot = self._client.screenshot()
                if shot is not None and shot.data:
                    b64 = shot.data.get("base64")
                    if isinstance(b64, str) and b64:
                        screenshot_b64 = b64
            except Exception:
                # Screenshot is optional; never fail observation because of it.
                pass

        return Observation(
            success=True,
            package_name=package_name if isinstance(package_name, str) else None,
            ui_root=ui_root,
            screenshot_base64=screenshot_b64,
        )


class BridgeActionExecutor:
    """Executes agent actions by dispatching them to the bridge client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def execute(self, action: AgentAction) -> ActionResult:
        at = action.action_type
        args = dict(action.arguments)

        try:
            if at is ActionType.TAP:
                resp = self._client.tap(args.get("x"), args.get("y"))
                return self._ok(resp)

            if at is ActionType.BACK:
                resp = self._client.back()
                return self._ok(resp)

            if at is ActionType.INPUT_TEXT:
                resp = self._client.input_text(args.get("text", ""))
                return self._ok(resp)

            if at is ActionType.SWIPE:
                resp = self._client.swipe(
                    args.get("x1"),
                    args.get("y1"),
                    args.get("x2"),
                    args.get("y2"),
                    args.get("duration_ms", 300),
                )
                return self._ok(resp)

            if at is ActionType.LONG_PRESS:
                resp = self._client.long_press(
                    args.get("x"),
                    args.get("y"),
                    args.get("duration_ms", 600),
                )
                return self._ok(resp)

            if at is ActionType.CLEAR_TEXT:
                resp = self._client.clear_text()
                return self._ok(resp)

            if at is ActionType.ERASE_TEXT:
                resp = self._client.erase_text()
                return self._ok(resp)

            if at is ActionType.GET_WINDOW:
                resp = self._client.get_window()
                return self._ok(resp)

            if at is ActionType.OPEN_URL:
                resp = self._client.open_url(args.get("url", ""))
                return self._ok(resp)

            if at is ActionType.KEY_EVENT:
                resp = self._client.key_event(args.get("keycode", ""))
                return self._ok(resp)

            if at is ActionType.OPEN_APP:
                resp = self._client.launch_app(args.get("package", ""))
                return self._ok(resp)

            if at is ActionType.SCREENSHOT:
                resp = self._client.screenshot()
                return self._ok(resp)

            if at is ActionType.UI_DUMP:
                resp = self._client.ui_dump()
                return self._ok(resp)

            if at is ActionType.WAIT:
                seconds = float(args.get("seconds", 1.0))
                time.sleep(min(max(seconds, 0.0), 5.0))
                return ActionResult(success=True, data={"waited": seconds})

            if at is ActionType.FINISH:
                return ActionResult(success=True, data={"finish": True})

            if at is ActionType.INSTALL_APK:
                return ActionResult(
                    success=False,
                    error_code="DANGEROUS_ACTION_GATED",
                    error_message=(
                        "install_apk requires owner authorization and is "
                        "not dispatched directly by the runner"
                    ),
                )

            return ActionResult(
                success=False,
                error_code="UNSUPPORTED_ACTION",
                error_message=f"unsupported action: {at.value}",
            )
        except Exception as exc:
            return ActionResult(
                success=False,
                error_code="EXECUTION_EXCEPTION",
                error_message=str(exc),
            )

    @staticmethod
    def _ok(resp: Any) -> ActionResult:
        data: dict = {}
        if resp is not None and getattr(resp, "data", None):
            try:
                data = dict(resp.data)
            except (TypeError, ValueError):
                data = {}
        op_id = data.get("operation_id") if isinstance(data, dict) else None
        return ActionResult(
            success=True,
            operation_id=op_id,
            data=data,
        )


class LastActionGoalVerifier:
    """
    Deterministic goal verifier.

    The LLM signals completion by emitting a ``finish`` action; this verifier
    reports success once that action has been executed. No LLM round-trip is
    needed for verification.
    """

    def verify(
        self,
        context: AgentContext,
        observation: Observation | None,
    ) -> GoalResult:
        last = context.last_action
        if last is not None and last.action_type is ActionType.FINISH:
            return GoalResult(success=True, reason="Agent signaled finish")
        return GoalResult(
            success=False, reason="Goal not yet achieved"
        )


class SimpleRecovery:
    """Recovery handler that presses Back and reports recovery."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def recover(self, context: AgentContext) -> bool:
        try:
            self._client.back()
        except Exception:
            pass
        return True


class LLMPlannerAdapter:
    """
    Adapts :class:`LLMPlanner` (a CandidateProvider) to the executor's
    ``Planner`` protocol, which expects ``plan(context) -> list[AgentAction]``.
    """

    def __init__(self, planner: LLMPlanner) -> None:
        self._planner = planner

    def plan(self, context: AgentContext) -> list[AgentAction]:
        try:
            candidates = self._planner.generate(context, context.observation)
        except Exception:
            return [AgentAction(ActionType.WAIT, {"seconds": 1.0})]

        if not candidates:
            return [AgentAction(ActionType.WAIT, {"seconds": 1.0})]

        return list(candidates[0].actions)


class AgentRunner:
    """
    Wires the LLM brain, the bridge client, and the deterministic executor
    into a single autonomous loop.
    """

    def __init__(
        self,
        client: Any,
        llm_client: LLMClient,
        *,
        max_actions: int = 50,
        trace_path: str | Path | None = None,
        include_screenshot: bool = True,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._llm_client = llm_client
        self._max_actions = max_actions
        self._trace_path = (
            Path(trace_path)
            if trace_path
            else Path.home() / ".agentpro" / "trace.jsonl"
        )
        self._include_screenshot = include_screenshot
        self._model = model

    def build_executor(self, goal: str) -> AgentExecutor:
        context = AgentContext(goal=goal)
        limits = FSMLimits(
            max_steps=max(200, self._max_actions * 6),
            max_actions=self._max_actions,
            max_recoveries=max(3, self._max_actions // 4),
            max_replans=self._max_actions,
        )
        fsm = AgentFSM(context, limits)
        trace = TraceRecorder(self._trace_path)

        llm_planner = LLMPlanner(
            self._llm_client,
            model=self._model,
            include_screenshot=self._include_screenshot,
        )

        return AgentExecutor(
            context=context,
            fsm=fsm,
            trace=trace,
            observer=BridgeObserver(
                self._client, include_screenshot=self._include_screenshot
            ),
            planner=LLMPlannerAdapter(llm_planner),
            action_executor=BridgeActionExecutor(self._client),
            goal_verifier=LastActionGoalVerifier(),
            recovery_handler=SimpleRecovery(self._client),
        )

    def run(self, goal: str) -> ExecutionResult:
        executor = self.build_executor(goal)
        return executor.run()


def build_runner(
    client: Any,
    llm_client: LLMClient,
    *,
    max_actions: int = 50,
    trace_path: str | Path | None = None,
    include_screenshot: bool = True,
    model: str | None = None,
) -> AgentRunner:
    """Convenience factory."""
    return AgentRunner(
        client,
        llm_client,
        max_actions=max_actions,
        trace_path=trace_path,
        include_screenshot=include_screenshot,
        model=model,
    )
