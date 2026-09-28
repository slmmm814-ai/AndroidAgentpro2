"""Hierarchical, tool-based planner for the autonomy loop.

* decomposes the goal into ordered subgoals (LLM, once)
* per step, decides the single next **tool call** given the live screen,
  recent history, failures, and the active subgoal
* decides when a subgoal is done, when to replan the plan, and when the
  whole goal is done (which triggers *goal* verification, not claimed success)
* falls back to a safe ``wait`` whenever the model output is unusable
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .bridge_tools import ToolRegistry
from .llm_planner import LLMError, _extract_json
from .memory import MemoryStore
from .model_manager import ModelManager
from .models import ActionType, AgentAction, AgentContext
from .screen import ScreenSnapshot, summarize_screen


DECOMPOSE_SYSTEM = """\
You are the planner of an Android automation agent. Break the user's goal into \
an ordered list of concrete, verifiable subgoals that can be executed on a \
phone. Each subgoal is one logical stage (e.g. "open the app", "create a new \
item", "fill the form", "save", "verify it appears in the list").

Respond with ONLY JSON:
{"subgoals": [{"text": "<subgoal>", "verifiable": "<short observable outcome>"}]}
Use at most 6 subgoals. Do not invent UI details; stay at the level of intent.
"""

DECIDE_SYSTEM_TEMPLATE = """\
You are the step-planner of an Android automation agent working toward a goal \
through ordered subgoals. You see one step: current screen, active subgoal, \
recent actions, failures. Choose the SINGLE next tool call.

Available tools:\n{tools}

Respond with ONLY JSON with exactly these fields:
{{
  "thought": "<one short sentence>",
  "tool": "<tool name>",
  "args": {{... tool arguments ...}},
  "confidence": <0..1>,
  "subgoal_done": <bool: active subgoal now achieved>,
  "goal_done": <bool: the entire goal is achieved and verified>,
  "replan": <bool: current plan is wrong, rebuild subgoals>,
}}

Rules:
- Pick targets from the CURRENT screen summary only.
- Use tap_element with the [N] index (or its text) from the summary to tap \
an element by identity; use the raw tap coordinates only when no element \
matches.
- To open an app: pass open_app with "name" (the label you see on the \
launcher, e.g. "Calculator"). Only pass "package" if that exact package name \
is already known from this session; never invent a package name.
- Never repeat a call that already failed with the same arguments. After a \
failure, change approach: different target, scroll, search, or another tool.
- Use type_text only when an editable (focused) field is present; use \
erase_text (or clear_text) to empty a field before typing into it.
- Use wait_for_text / wait_for_screen_stable instead of guess-and-tap when a \
transition (load, save, search) is expected.
- Never claim goal_done unless the screen evidence shows the final outcome \
(e.g. the saved item appears in the list).
- For building an app (calculator, notes, todo, etc.), use open_url with a \
data:text/html URL that embeds a complete working app; then the browser shows \
it. No install is needed.
"""


@dataclass(frozen=True)
class PlanDecision:
    tool: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    thought: str = ""
    confidence: float = 0.2
    goal_done: bool = False
    subgoal_done: bool = False
    replan: bool = False

    @property
    def is_finish(self) -> bool:
        return self.goal_done

    @property
    def is_action(self) -> bool:
        return not self.goal_done and self.tool is not None


class SubgoalPlanner:
    """Decomposes the goal once, then chooses tool calls per step."""

    def __init__(
        self,
        model_manager: ModelManager,
        registry: ToolRegistry,
        memory: MemoryStore,
        *,
        max_subgoals: int = 6,
        timeout: float = 30.0,
    ) -> None:
        if not hasattr(model_manager, "complete_json"):
            raise TypeError("model_manager must implement complete_json()")
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be a ToolRegistry")
        self._mm = model_manager
        self._registry = registry
        self._memory = memory
        self._max_subgoals = max_subgoals
        self._timeout = timeout

    # -- decomposition ----------------------------------------------------

    def ensure_subgoals(self, goal: str) -> None:
        if self._memory.subgoal_count > 0:
            return

        def validator(obj: Mapping[str, Any]) -> None:
            subgoals = obj.get("subgoals")
            if not isinstance(subgoals, list) or not subgoals:
                raise ValueError("subgoals must be a non-empty list")
            if len(subgoals) > self._max_subgoals:
                raise ValueError(f"too many subgoals (max {self._max_subgoals})")
            for sg in subgoals:
                if not isinstance(sg, Mapping) or not isinstance(sg.get("text"), str):
                    raise ValueError("each subgoal needs a 'text' string")

        messages = [
            {"role": "system", "content": DECOMPOSE_SYSTEM},
            {"role": "user", "content": f"GOAL: {goal}"},
        ]

        try:
            obj = self._mm.complete_json(
                messages,
                validator=validator,  # type: ignore[arg-type]
                timeout=self._timeout,
            )
        except Exception as exc:
            # Safe fallback: a single subgoal = the whole goal.
            self._memory.set_subgoal(0, 1, goal)
            self._memory.record_episode(
                0, "decompose_fallback", f"LLM decomposition failed: {exc}"
            )
            return

        subgoals = [str(sg["text"]).strip() for sg in obj["subgoals"]]
        self._memory.record_episode(
            0, "decomposed", json.dumps(subgoals, ensure_ascii=False)
        )
        self._memory.subgoal_list = subgoals
        self._memory.set_subgoal(0, len(subgoals), subgoals[0])

    def _advance_subgoal(self) -> None:
        memory = self._memory
        next_index = memory.subgoal_index + 1
        if next_index < memory.subgoal_count and len(memory.subgoal_list) > next_index:
            memory.set_subgoal(
                next_index,
                memory.subgoal_count,
                memory.subgoal_list[next_index],
            )
        else:
            memory.set_subgoal(
                memory.subgoal_index, memory.subgoal_count, memory.active_subgoal or ""
            )

    def replan(self, goal: str) -> None:
        self._memory.set_subgoal(0, 0, "")  # force re-decomposition
        self.ensure_subgoals(goal)

    def advance_subgoal(self) -> None:
        """Move to the next subgoal after the current one reports done."""
        self._advance_subgoal()

    # -- step decision ----------------------------------------------------

    def decide(
        self,
        context: AgentContext,
        snapshot: ScreenSnapshot | None,
        last_tool_result: str | None = None,
    ) -> PlanDecision:
        self.ensure_subgoals(context.goal)

        tools_prompt = self._registry.specs_for_prompt()
        screen_summary = (
            summarize_screen(snapshot, max_chars=2600)
            if snapshot is not None and snapshot.success
            else "(screen unreadable)"
        )

        memory_text = self._memory.build_context(context.goal)

        last_line = "last tool result: " + (last_tool_result or "(none)")

        user_text = (
            f"STEP: {context.step}  ACTION_COUNT: {context.action_count}\n"
            f"\n{memory_text}\n\n"
            f"CURRENT SCREEN SUMMARY:\n{screen_summary}\n\n"
            f"{last_line}"
        )

        messages = [
            {
                "role": "system",
                "content": DECIDE_SYSTEM_TEMPLATE.format(tools=tools_prompt),
            },
            {"role": "user", "content": user_text},
        ]

        try:
            obj = self._mm.complete_json(
                messages,
                validator=self._validate_decision,
                timeout=self._timeout,
            )
        except Exception as exc:
            self._memory.record_failure(
                context.step,
                action_line="planner",
                error_code="PLANNER_RS_ERROR",
                detail=str(exc),
                recovered_by="wait",
            )
            return PlanDecision(
                tool="wait",
                args={"seconds": 1.0},
                thought=f"plan decision failed: {exc}",
                confidence=0.1,
            )

        tool = obj.get("tool")
        args = obj.get("args") if isinstance(obj.get("args"), Mapping) else {}

        if tool is not None:
            valid, errors = self._registry.validate_args(str(tool), args)
            if not valid:
                return PlanDecision(
                    tool="wait",
                    args={"seconds": 1.0},
                    thought=f"planner chose invalid args for {tool}: {'; '.join(errors)}",
                    confidence=0.1,
                )

        goal_done = bool(obj.get("goal_done"))
        if tool is None and not goal_done:
            return PlanDecision(
                tool="wait",
                args={"seconds": 1.0},
                thought="planner returned no tool without signaling completion",
                confidence=0.1,
            )

        return PlanDecision(
            tool=tool,
            args=dict(args),
            thought=str(obj.get("thought", "")),
            confidence=float(obj.get("confidence", 0.2)),
            goal_done=goal_done,
            subgoal_done=bool(obj.get("subgoal_done")),
            replan=bool(obj.get("replan")),
        )

    def _validate_decision(self, obj: Mapping[str, Any]) -> None:
        tool = obj.get("tool")
        if tool is not None and not isinstance(tool, str):
            raise ValueError("tool must be a string or null")
        if tool is not None and self._registry.get(tool) is None:
            raise ValueError(f"unknown tool '{tool}'")
        args = obj.get("args")
        if args is not None and not isinstance(args, Mapping):
            raise ValueError("args must be an object")
        for key in ("confidence",):
            value = obj.get(key)
            if not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be numeric")
            if value < 0.0 or value > 1.0:
                raise ValueError(f"{key} must be in [0,1]")
        for flag in ("goal_done", "subgoal_done", "replan"):
            if obj.get(flag) is not None and not isinstance(obj.get(flag), bool):
                raise ValueError(f"{flag} must be boolean")

    # -- executor compatibility -------------------------------------------

    def plan(self, context: AgentContext) -> list[AgentAction]:
        """Adapter for the ``Planner`` protocol (returns a single action)."""
        decision = self.decide(context, None)
        if decision.is_finish:
            return [AgentAction(ActionType.FINISH)]
        if decision.is_action:
            action_type = self._registry.get(decision.tool).to_action_type()
            return [AgentAction(action_type, dict(decision.args))]
        return [AgentAction(ActionType.WAIT, {"seconds": 1.0})]


def build_planner(
    model_manager: ModelManager,
    registry: ToolRegistry,
    memory: MemoryStore,
) -> SubgoalPlanner:
    return SubgoalPlanner(model_manager, registry, memory)