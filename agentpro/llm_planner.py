from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .meta_planner import CandidateProvider, PlanCandidate, make_candidate
from .models import ActionType, AgentAction, AgentContext, Observation


class LLMError(RuntimeError):
    """Raised on LLM client failure or unparseable response."""


class LLMClient(Protocol):
    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        json_mode: bool = False,
        timeout: float = 30.0,
    ) -> str:
        ...


_ACTION_MAP: dict[str, ActionType] = {
    "tap": ActionType.TAP,
    "click": ActionType.TAP,
    "back": ActionType.BACK,
    "input_text": ActionType.INPUT_TEXT,
    "type": ActionType.INPUT_TEXT,
    "enter_text": ActionType.INPUT_TEXT,
    "swipe": ActionType.SWIPE,
    "scroll": ActionType.SWIPE,
    "open_url": ActionType.OPEN_URL,
    "url": ActionType.OPEN_URL,
    "navigate": ActionType.OPEN_URL,
    "key_event": ActionType.KEY_EVENT,
    "keyevent": ActionType.KEY_EVENT,
    "key": ActionType.KEY_EVENT,
    "open_app": ActionType.OPEN_APP,
    "launch_app": ActionType.OPEN_APP,
    "launch": ActionType.OPEN_APP,
    "screenshot": ActionType.SCREENSHOT,
    "ui_dump": ActionType.UI_DUMP,
    "wait": ActionType.WAIT,
    "sleep": ActionType.WAIT,
    "finish": ActionType.FINISH,
    "done": ActionType.FINISH,
    "stop": ActionType.FINISH,
    "long_press": ActionType.LONG_PRESS,
    "clear_text": ActionType.CLEAR_TEXT,
    "erase_text": ActionType.ERASE_TEXT,
    "get_window": ActionType.GET_WINDOW,
    "window_info": ActionType.GET_WINDOW,
}


SYSTEM_PROMPT = """\
You are an autonomous Android phone agent. You receive the user's goal, the \
last action and its result, a compact text summary of the current UI tree, \
and optionally a screenshot. You must decide the single next action.

Respond with ONLY a JSON object (no markdown fences, no surrounding prose) \
with exactly these fields:
{
  "thought": "<one short sentence of reasoning>",
  "action": "<one of: tap, back, input_text, swipe, open_url, key_event, \
open_app, screenshot, ui_dump, wait, finish>",
  "args": { ... action-specific arguments, see below ... },
  "done": <true when the goal is fully achieved, else false>,
  "confidence": <float between 0.0 and 1.0>
}

Action argument schemas:
- tap: {"x": <number>, "y": <number>}
- input_text: {"text": "<string to type into the focused field>"}
- swipe: {"x1":<n>, "y1":<n>, "x2":<n>, "y2":<n>, "duration_ms":<int>}
- open_url: {"url": "<http(s) url or a data:text/html ... url>"}
- key_event: {"keycode": "<home|recents|back|volume_up|volume_down|...>"}
- open_app: {"package": "<android package name, e.g. com.android.chrome>"}
- screenshot: {}
- ui_dump: {}
- wait: {"seconds": <number>}
- finish: {}

IMPORTANT: To "build an app" (for example a calculator), you do NOT need to \
install anything. Compose a complete, working HTML document (with inline CSS \
and JS) and open it with open_url using a data:text/html URL. The browser \
will render the app immediately.
"""


def _extract_json(text: str) -> dict[str, Any]:
    """Robustly extract the first JSON object from a possibly noisy string."""
    if not isinstance(text, str):
        raise LLMError("LLM response is not a string")
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        raise LLMError("LLM JSON root is not an object")
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        raise LLMError("LLM response contains no JSON object")

    depth = 0
    in_str = False
    esc = False
    end = -1

    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break

    if end < 0:
        raise LLMError("LLM response JSON object is not balanced")

    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM JSON parse failed: {exc}") from exc

    if not isinstance(obj, dict):
        raise LLMError("LLM JSON root must be an object")

    return obj


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _build_action(obj: dict[str, Any]) -> AgentAction | None:
    """Build an AgentAction from the parsed LLM object, or None on invalid args."""
    action_str = obj.get("action")
    if not isinstance(action_str, str):
        return None

    action_type = _ACTION_MAP.get(action_str.strip().lower())
    if action_type is None:
        return None

    args = obj.get("args", {})
    if not isinstance(args, dict):
        args = {}

    try:
        if action_type is ActionType.TAP:
            x = args.get("x")
            y = args.get("y")
            if not _is_number(x) or not _is_number(y):
                return None
            return AgentAction(action_type, {"x": float(x), "y": float(y)})

        if action_type is ActionType.INPUT_TEXT:
            text = args.get("text")
            if not isinstance(text, str) or not text:
                return None
            return AgentAction(action_type, {"text": text})

        if action_type is ActionType.SWIPE:
            x1, y1, x2, y2 = (
                args.get("x1"),
                args.get("y1"),
                args.get("x2"),
                args.get("y2"),
            )
            if not all(_is_number(v) for v in (x1, y1, x2, y2)):
                return None
            dur = args.get("duration_ms", 300)
            if not isinstance(dur, int) or isinstance(dur, bool):
                dur = 300
            return AgentAction(
                action_type,
                {
                    "x1": float(x1),
                    "y1": float(y1),
                    "x2": float(x2),
                    "y2": float(y2),
                    "duration_ms": int(dur),
                },
            )

        if action_type is ActionType.OPEN_URL:
            url = args.get("url")
            if not isinstance(url, str) or not url.strip():
                return None
            return AgentAction(action_type, {"url": url})

        if action_type is ActionType.KEY_EVENT:
            keycode = args.get("keycode")
            if not isinstance(keycode, str) or not keycode.strip():
                return None
            return AgentAction(action_type, {"keycode": keycode.strip()})

        if action_type is ActionType.OPEN_APP:
            package = args.get("package")
            if not isinstance(package, str) or not package.strip():
                return None
            return AgentAction(action_type, {"package": package.strip()})

        if action_type is ActionType.LONG_PRESS:
            x = args.get("x")
            y = args.get("y")
            if not _is_number(x) or not _is_number(y):
                return None
            dur = args.get("duration_ms", 600)
            return AgentAction(
                action_type,
                {"x": float(x), "y": float(y), "duration_ms": int(dur)},
            )

        if action_type is ActionType.CLEAR_TEXT:
            return AgentAction(action_type)

        if action_type is ActionType.ERASE_TEXT:
            return AgentAction(action_type)

        if action_type is ActionType.GET_WINDOW:
            return AgentAction(action_type)

        if action_type is ActionType.WAIT:
            seconds = args.get("seconds", 1.0)
            if not _is_number(seconds):
                seconds = 1.0
            return AgentAction(action_type, {"seconds": float(seconds)})

        # screenshot, ui_dump, finish, back: no required args
        return AgentAction(action_type)
    except (TypeError, ValueError):
        return None


def _summarize_ui_tree(
    ui_root: Mapping[str, Any] | None,
    max_nodes: int = 120,
) -> str:
    """Compact text representation of the UI tree for the LLM."""
    if not isinstance(ui_root, Mapping) or not ui_root:
        return "(no UI tree available)"

    lines: list[str] = []
    count = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal count
        if count >= max_nodes:
            return
        if not isinstance(node, Mapping):
            return

        cls = node.get("class") or node.get("className") or "?"
        text = node.get("text") or ""
        content_desc = (
            node.get("content_desc") or node.get("contentDescription") or ""
        )
        bounds = node.get("bounds") or ""
        clickable = node.get("clickable")

        cls_short = (
            str(cls).split(".")[-1] if isinstance(cls, str) else str(cls)
        )
        parts = [f"{'  ' * depth}- {cls_short}"]
        if text:
            parts.append(f'text="{text}"')
        if content_desc:
            parts.append(f'desc="{content_desc}"')
        if bounds:
            parts.append(f"bounds={bounds}")
        if clickable is not None:
            parts.append(f"clickable={clickable}")
        lines.append(" ".join(parts))
        count += 1

        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                walk(child, depth + 1)

    walk(ui_root, 0)

    if count >= max_nodes:
        lines.append("... (truncated)")

    return "\n".join(lines) if lines else "(empty UI tree)"


@dataclass(frozen=True)
class ParsedDecision:
    action: AgentAction | None
    done: bool
    thought: str
    confidence: float


def _parse_llm_response(text: str) -> ParsedDecision:
    obj = _extract_json(text)

    done = bool(obj.get("done", False))

    thought = obj.get("thought", "")
    if not isinstance(thought, str):
        thought = str(thought)

    confidence = obj.get("confidence", 0.6)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.6
    if not 0.0 <= confidence <= 1.0:
        confidence = max(0.0, min(1.0, confidence))

    action = _build_action(obj)

    return ParsedDecision(
        action=action,
        done=done,
        thought=thought,
        confidence=confidence,
    )


class LLMPlanner:
    """
    LLM-driven candidate provider.

    Implements the ``CandidateProvider`` protocol: :meth:`generate` returns a
    single ``PlanCandidate`` describing the next action the LLM chose.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        model: str | None = None,
        include_screenshot: bool = True,
        max_ui_nodes: int = 120,
        timeout: float = 30.0,
    ) -> None:
        if not hasattr(client, "complete"):
            raise TypeError("client must implement complete()")
        self._client = client
        self._model = model
        self._include_screenshot = include_screenshot
        self._max_ui_nodes = max_ui_nodes
        self._timeout = timeout

    def _build_messages(
        self,
        context: AgentContext,
        observation: Observation | None,
    ) -> list[Mapping[str, Any]]:
        last = ""
        if context.last_action is not None:
            last = (
                f"last_action={context.last_action.action_type.value} "
                f"args={dict(context.last_action.arguments)}"
            )
        if context.last_action_result is not None:
            last += f" -> success={context.last_action_result.success}"
            if context.last_action_result.error_code:
                last += f" error={context.last_action_result.error_code}"

        ui_summary = "(no observation yet)"
        screenshot_b64 = None

        if observation is not None:
            ui_summary = _summarize_ui_tree(
                observation.ui_root, self._max_ui_nodes
            )
            if observation.package_name:
                ui_summary = (
                    f"package={observation.package_name}\n" + ui_summary
                )
            if self._include_screenshot and observation.screenshot_base64:
                screenshot_b64 = observation.screenshot_base64

        user_text = (
            f"GOAL: {context.goal}\n"
            f"STEP: {context.step}  ACTION_COUNT: {context.action_count}\n"
            f"LAST: {last or '(none)'}\n\n"
            f"UI TREE:\n{ui_summary}\n"
        )

        if screenshot_b64:
            user_content: list[Mapping[str, Any]] = [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{screenshot_b64}"
                    },
                },
            ]
        else:
            user_content = user_text  # type: ignore[assignment]

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def decide(
        self,
        context: AgentContext,
        observation: Observation | None,
    ) -> ParsedDecision:
        messages = self._build_messages(context, observation)
        raw = self._client.complete(
            messages, json_mode=True, timeout=self._timeout
        )
        if not isinstance(raw, str):
            raise LLMError("LLM client must return a string")
        return _parse_llm_response(raw)

    def generate(
        self,
        context: AgentContext,
        observation: Observation | None,
    ) -> Sequence[PlanCandidate]:
        context.validate()

        try:
            decision = self.decide(context, observation)
        except LLMError as exc:
            return [
                make_candidate(
                    "llm_error_wait",
                    [AgentAction(ActionType.WAIT, {"seconds": 1.0})],
                    confidence=0.2,
                    risk=0.1,
                    rationale=f"LLM error: {exc}",
                    metadata={"fallback": True, "error": str(exc)},
                )
            ]

        if decision.done:
            return [
                make_candidate(
                    "llm_done",
                    [AgentAction(ActionType.FINISH)],
                    confidence=max(0.5, decision.confidence),
                    risk=0.0,
                    rationale=decision.thought or "goal achieved",
                    metadata={"done": True},
                )
            ]

        if decision.action is None:
            return [
                make_candidate(
                    "llm_invalid_wait",
                    [AgentAction(ActionType.WAIT, {"seconds": 1.0})],
                    confidence=0.2,
                    risk=0.1,
                    rationale=(
                        "LLM produced an invalid or unparseable action: "
                        f"{decision.thought}"
                    ),
                    metadata={"fallback": True},
                )
            ]

        risk = 0.25
        if decision.action.action_type in (
            ActionType.OPEN_URL,
            ActionType.OPEN_APP,
        ):
            risk = 0.4
        elif decision.action.action_type is ActionType.KEY_EVENT:
            risk = 0.4

        return [
            make_candidate(
                "llm_step",
                [decision.action],
                confidence=decision.confidence,
                risk=risk,
                rationale=decision.thought,
                metadata={"done": False},
            )
        ]


class OpenAICompatibleClient:
    """
    Synchronous OpenAI-compatible chat client.

    Works with OpenAI, OpenRouter, and any server exposing
    ``POST {base_url}/chat/completions``. All settings are read from
    constructor args or environment variables.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get(
            "AGENTPRO_LLM_API_KEY", ""
        )
        self._base_url = (
            base_url
            or os.environ.get(
                "AGENTPRO_LLM_BASE_URL", "https://api.openai.com/v1"
            )
        ).rstrip("/")
        self._model = model or os.environ.get(
            "AGENTPRO_LLM_MODEL", "gpt-4o-mini"
        )
        t = timeout if timeout is not None else os.environ.get(
            "AGENTPRO_LLM_TIMEOUT"
        )
        self._timeout = float(t) if t is not None else 30.0

        if not self._api_key:
            raise LLMError("AGENTPRO_LLM_API_KEY is not configured")

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        json_mode: bool = False,
        timeout: float | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        import json as _json
        import urllib.error
        import urllib.request

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if temperature is not None:
            payload["temperature"] = float(temperature)

        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self._base_url}/chat/completions"
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout or self._timeout,
            ) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                detail = ""
            raise LLMError(
                f"LLM HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM request failed: {exc.reason}") from exc

        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError as exc:
            raise LLMError(
                f"LLM response is not valid JSON: {exc}"
            ) from exc

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"Unexpected LLM response shape: {exc}"
            ) from exc


class FakeLLMClient:
    """
    Scripted LLM client for tests and offline demos.

    Pass either a ``script`` (a list of canned response strings, consumed in
    order) or a ``responder`` callable ``(messages) -> str``.
    """

    def __init__(
        self,
        script: Sequence[str] | None = None,
        *,
        responder=None,
    ) -> None:
        if script is not None and responder is not None:
            raise ValueError(
                "provide either script or responder, not both"
            )
        self._script = list(script) if script is not None else None
        self._responder = responder
        self._index = 0
        self.calls: list[list[Mapping[str, Any]]] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        json_mode: bool = False,
        timeout: float = 30.0,
    ) -> str:
        self.calls.append(list(messages))

        if self._script is not None:
            if self._index >= len(self._script):
                raise LLMError("FakeLLMClient script exhausted")
            text = self._script[self._index]
            self._index += 1
            return text

        if self._responder is not None:
            return self._responder(list(messages))

        raise LLMError("FakeLLMClient has no script or responder")
