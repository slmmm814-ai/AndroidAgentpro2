"""APEX agent — the integrated observe → plan → act → verify loop.

This is the layer that makes the four components behave like one agent:

* ``physics`` decides scrolling without a model;
* ``appmap`` answers "have I already been here / am I at the bottom" before the
  planner even has to ask;
* ``grounder`` resolves every tap tree-first, so input is 100% accurate whenever
  the accessibility tree can name the target;
* ``tiered`` keeps routine steps on a fast model and reserves the big model for
  planning, recovery, and goal verification.

The loop is deliberately *rule-first*: anything the deterministic layers can
answer is never sent to a model. That is what removes the v2 loop's two loudest
failure modes — random scrolling and per-step latency.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .appmap import AppMap
from .grounder import GroundCandidate
from .physics import Physics
from .tiered import TieredModels

_LOGGER = logging.getLogger("agentpro.apex.agent")


# --------------------------------------------------------------------------- #
# the device-facing surface the loop needs (a thin protocol, easy to fake)
# --------------------------------------------------------------------------- #


class ApexDriver(Protocol):
    """Everything the APEX loop touches on the device."""

    def snapshot(self) -> dict[str, Any]:
        """Return {'package', 'activity', 'fingerprint', 'texts', 'candidates'}."""
        ...

    def screenshot_b64(self) -> str | None:
        ...

    def screen_size(self) -> tuple[int, int]:
        ...

    def tap(self, x: int, y: int) -> dict[str, Any]:
        ...

    def physics(self) -> Physics:
        ...

    def back(self) -> dict[str, Any]:
        ...

    def input_text(self, text: str) -> dict[str, Any]:
        ...


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


@dataclass
class StepRecord:
    """One executed step, for the trace."""

    index: int
    action: str
    detail: str
    tier: str
    seconds: float
    result: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "detail": self.detail,
            "tier": self.tier,
            "seconds": round(self.seconds, 3),
            "result": self.result,
        }


@dataclass
class ApexReport:
    """Final report of an APEX run."""

    goal: str
    success: bool
    steps: list[StepRecord] = field(default_factory=list)
    reason: str = ""
    appmap: dict[str, Any] = field(default_factory=dict)
    model_stats: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    flings: int = 0
    taps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success": self.success,
            "reason": self.reason,
            "duration_seconds": round(self.duration_seconds, 3),
            "flings": self.flings,
            "taps": self.taps,
            "steps": [s.to_dict() for s in self.steps],
            "appmap": self.appmap,
            "model_stats": self.model_stats,
        }


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #


class ApexAgent:
    """Rule-first autonomous loop.

    Hard safety rails (like the v2 agent): bounded steps, a repeat guard, and an
    optional kill check. Nothing here can run away forever.
    """

    def __init__(
        self,
        driver: ApexDriver,
        models: TieredModels,
        *,
        max_steps: int = 60,
        max_repeat: int = 3,
        kill_file: str | None = None,
        expect_package: str | None = None,
        scroll_flings: int = 8,
        scroll_deadline_seconds: float = 25.0,
    ) -> None:
        self._driver = driver
        self._models = models
        self.max_steps = max(1, max_steps)
        self.max_repeat = max(1, max_repeat)
        self.kill_file = kill_file
        self.map = AppMap()
        self.steps: list[StepRecord] = []
        self._seen_states: list[str] = []
        # targets already tapped without effect: retrying the same coordinate
        # just hammers a dead widget, so they are skipped in favour of scrolling
        self._failed_targets: set[str] = set()
        #: Actions already executed on the current screen. Without this the
        #: planner re-proposes the same tap every step and the loop spins
        #: until the repeat guard stops it, because a fresh prompt with the
        #: same screen invites the same answer.
        self._planner_tried: set[str] = set()
        #: packages seen that are not the app under test
        self._wrong_app_seen: set[str | None] = set()
        self._last_fingerprint: str | None = None
        #: The app this run is supposed to stay inside. Verification used to
        #: accept a screen that merely *contained* the target text, and on a
        #: live run the goal string was sitting in the terminal title bar of
        #: the very process driving the phone — so leaving the app under test
        #: produced a confident, completely fake success.
        self.expect_package = expect_package
        # Bounds on one deterministic scroll search. A feed that keeps
        # producing content never reaches a stable fingerprint, so an
        # unbounded fling loop is not "careful", it is stuck.
        self.scroll_flings = max(1, scroll_flings)
        self.scroll_deadline_seconds = float(scroll_deadline_seconds)

    # -- safety ------------------------------------------------------------- #

    def _killed(self) -> bool:
        import os

        if not self.kill_file:
            return False
        try:
            return os.path.exists(self.kill_file)
        except OSError:
            return False

    def _register_state(self, fingerprint: str) -> int:
        """Return how many times we have seen this exact screen before."""
        self._seen_states.append(fingerprint)
        return self._seen_states.count(fingerprint)

    # -- observe -> act -> verify ------------------------------------------- #

    def _settle_after_action(self) -> str:
        """Wait for the UI to stop changing and return the new fingerprint.

        A tap fires an animation or a screen transition; reading the tree
        immediately would catch the *old* screen and would make every
        verification lie.
        """
        settle = getattr(self._driver, "settle", None)
        if callable(settle):
            try:
                return str(settle())
            except Exception:  # noqa: BLE001 - verification is best effort
                pass

        # settle the animation first...
        self._driver.physics().wait_settled()
        # ...then read the identity of the screen from the driver itself: the
        # physics fingerprint only tracks scrolling, not navigation.
        state = self._driver.snapshot()
        return str(state.get("fingerprint") or "unknown")

    def _left_expected_app(self, package: str | None = None) -> bool:
        """True when the screen is no longer the app this run is driving.

        A run that wanders out of the app under test cannot claim success, no
        matter what the text happens to say. On a live device the goal string
        was visible in the terminal that was driving the phone, so a
        text-only check happily "verified" a Termux window as a finished
        Instagram task.

        Recording the step is idempotent per screen: the loop calls this every
        iteration, and a single wrong_app step is the useful signal.
        """
        if not self.expect_package:
            return False
        if package is None:
            package = self._driver.snapshot().get("package")
        if package == self.expect_package:
            return False
        if package not in self._wrong_app_seen:
            self._wrong_app_seen.add(package)
            self.steps.append(
                StepRecord(
                    index=len(self.steps),
                    action="app-guard",
                    detail=(
                        f"left {self.expect_package}: now showing {package!r}, "
                        "so the goal cannot be verified here"
                    ),
                    tier="none",
                    seconds=0.0,
                    result="wrong_app",
                )
            )
        return True

    def _goal_reached(
        self,
        goal: str,
        target: str,
        fingerprint: str,
        *,
        previous: str,
    ) -> bool:
        """Decide whether the goal actually happened on the new screen.

        A tap is not proof. Success requires evidence from the screen *after*
        the action, so a no-op tap on the right-looking widget does not end the
        run with a false positive.
        """
        if fingerprint == previous:
            # the screen did not react: the tap was a no-op
            return False

        # The screen must still be the app this run is driving, and this is
        # checked *before* the no-op test: leaving the app is not a tap that
        # failed to land, it is the run leaving its own sandbox.
        if self._left_expected_app():
            return False

        state = self._driver.snapshot()
        texts = [str(t).strip().lower() for t in (state.get("texts") or ())]

        # Deterministic evidence: the new screen carries the target's name.
        # Opening a chat or a contact shows its name in the toolbar, so this is
        # the signal we get without paying for a model call.
        if any(target in text for text in texts):
            return True

        # No such evidence. The screen changed but the goal is not proven, so
        # ask the big model rather than assuming success.
        return self._ask_verifier(goal, fingerprint, texts)

    def _ask_verifier(self, goal: str, fingerprint: str, texts: list[str]) -> bool:
        system = (
            "You verify whether a phone task actually finished. Reply with "
            'exactly {"done": true} or {"done": false}.'
        )
        user = (
            f"Goal: {goal}\n"
            f"Screen fingerprint: {fingerprint}\n"
            f"Visible text: {', '.join(texts) if texts else '(none)'}\n"
            "Did the task finish on this screen?"
        )
        try:
            answer = self._models.ask("verify_goal", system, user)
        except Exception:  # noqa: BLE001 - a failed check is not a success
            return False
        return "true" in answer.lower() and "false" not in answer.lower()

    # -- the loop ----------------------------------------------------------- #

    def run(self, goal: str) -> ApexReport:
        started = time.monotonic()
        report = ApexReport(goal=goal, success=False)
        flings = 0
        taps = 0

        system = (
            "You are APEX, a precise Android phone agent. You act through a "
            "small set of tools. Prefer deterministic facts (already-known "
            "screen transitions, scroll memory) over guessing. Answer with a "
            "single JSON action only."
        )

        for index in range(self.max_steps):
            if self._killed():
                report.reason = "kill switch triggered"
                report.steps = list(self.steps)
                break

            state = self._driver.snapshot()
            fingerprint = state.get("fingerprint") or "unknown"
            package = state.get("package")
            texts = tuple(state.get("texts") or [])
            candidates: list[GroundCandidate] = state.get("candidates") or []

            repeats = self._register_state(fingerprint)
            if repeats > self.max_repeat:
                report.reason = f"stuck: screen seen {repeats}x"
                break

            # Stop the moment the phone leaves the app under test. Without
            # this the loop keeps planning and flinging against whatever else
            # is on screen, which is how a live run burned 41 flings inside
            # the terminal that was driving it.
            if self._left_expected_app(package):
                report.reason = (
                    f"left the app under test {self.expect_package!r}: "
                    f"phone is showing {package!r}"
                )
                break

            if fingerprint != self._last_fingerprint:
                # a new screen makes previously-tried actions irrelevant
                self._planner_tried.clear()
                self._last_fingerprint = fingerprint

            self.map.observe(
                fingerprint,
                package=package,
                activity=state.get("activity"),
                title=None,
                visible_texts=texts,
                now=time.time(),
                is_anchor=index == 0,
            )

            # --- rule 1: is the target already visible? tap it exactly ----
            target = _extract_target(goal)
            if target and target not in self._failed_targets:
                hit = _find_candidate(target, candidates)
                if hit is not None:
                    cx, cy = hit.center()
                    t0 = time.monotonic()
                    self._driver.tap(cx, cy)
                    taps += 1
                    self.steps.append(
                        StepRecord(
                            index=index,
                            action="tap",
                            detail=f"tree-exact {target!r} at ({cx},{cy})",
                            tier="none",
                            seconds=time.monotonic() - t0,
                            result="tapped",
                        )
                    )
                    # observe -> act -> verify: a tap is only a success when
                    # the screen afterwards actually shows the goal.
                    settled = self._settle_after_action()
                    self.map.record_transition(f"tap:{target}", settled)
                    if self._goal_reached(goal, target, settled, previous=fingerprint):
                        report.success = True
                        report.reason = f"tapped goal target {target!r} and verified"
                        break
                    # the tap did not do anything: remember it, so the loop
                    # stops hammering this coordinate and tries something else
                    self._failed_targets.add(target)
                    self.steps.append(
                        StepRecord(
                            index=index,
                            action="verify",
                            detail=(
                                f"goal not yet reached after tapping {target!r} "
                                f"(screen {settled!r})"
                            ),
                            tier="none",
                            seconds=0.0,
                            result="unverified",
                        )
                    )
                    continue

            # --- rule 2: if we know we are at the bottom, do not scroll ---
            if (
                target
                and target not in self._failed_targets
                and not self.map.reached_bottom(fingerprint)
            ):
                t0 = time.monotonic()
                scroll_result = self._driver.physics().scroll_to_text(
                    target, max_flings=self.scroll_flings
                )
                flings += scroll_result.flings
                if scroll_result.duration_seconds > self.scroll_deadline_seconds:
                    # A home feed never settles: it keeps producing fresh
                    # posts, so flinging "until the text appears" can spend
                    # minutes moving through a screen that was never going to
                    # contain it. Time-box the search and let the planner
                    # decide what to do next.
                    self._failed_targets.add(target)
                    self.steps.append(
                        StepRecord(
                            index=index,
                            action="scroll",
                            detail=(
                                f"gave up scrolling for {target!r} after "
                                f"{scroll_result.duration_seconds:.0f}s / "
                                f"{scroll_result.flings} flings"
                            ),
                            tier="none",
                            seconds=time.monotonic() - t0,
                            result="scroll_timeout",
                        )
                    )
                    continue
                self.map.record_scroll(
                    "down",
                    moved=scroll_result.moved,
                    fingerprint=fingerprint,
                )
                if scroll_result.final_fingerprint:
                    self.map.observe(
                        scroll_result.final_fingerprint,
                        package=package,
                        activity=state.get("activity"),
                        title=None,
                        visible_texts=(),
                        now=time.time(),
                        is_anchor=False,
                    )
                self.steps.append(
                    StepRecord(
                        index=index,
                        action="scroll",
                        detail=(
                            f"scroll_to_text {target!r} found={scroll_result.found} "
                            f"moved={scroll_result.moved}"
                        ),
                        tier="none",
                        seconds=time.monotonic() - t0,
                        result="found" if scroll_result.found else "not_found",
                    )
                )
                if scroll_result.found and scroll_result.center:
                    self._driver.tap(*scroll_result.center)
                    taps += 1
                    settled = self._settle_after_action()
                    self.map.record_transition(f"scroll_tap:{target}", settled)
                    if self._goal_reached(goal, target, settled, previous=fingerprint):
                        report.success = True
                        report.reason = (
                            f"scrolled to and tapped goal target {target!r} "
                            "and verified"
                        )
                        break
                    # the row was reachable but not tappable, or tapping it did
                    # nothing: stop coming back to the same place
                    self._failed_targets.add(target)
                    self.steps.append(
                        StepRecord(
                            index=index,
                            action="verify",
                            detail=(
                                f"goal not yet reached after scroll+tap of "
                                f"{target!r} (screen {settled!r})"
                            ),
                            tier="none",
                            seconds=0.0,
                            result="unverified",
                        )
                    )
                    continue

            # --- rule 3: ask the planner (big model) what to do ----------
            t0 = time.monotonic()
            user = _build_planner_prompt(
                goal,
                package,
                list(texts),
                self.map,
                repeats,
                tried=sorted(self._planner_tried),
            )
            try:
                answer = self._models.ask("plan", system, user)
                outcome = "planned"
            except Exception as exception:  # noqa: BLE001
                answer = ""
                outcome = f"model_error: {exception}"
            self.steps.append(
                StepRecord(
                    index=index,
                    action="plan",
                    detail=answer[:120],
                    tier="big",
                    seconds=time.monotonic() - t0,
                    result=outcome,
                )
            )

            # The planner must *act*, not merely talk about the goal. A reply
            # that mentions the target is not evidence that anything happened.
            action = _parse_action(answer)
            kind = ""
            outcome = "unparsable"
            if action is not None:
                kind = str(action.get("action") or action.get("op") or "").strip().lower()
                if kind in ("scroll", "swipe"):
                    direction = str(action.get("direction", "down"))
                    if direction not in ("down", "up"):
                        direction = "down"
                    self._driver.physics().scroll_once(direction)
                    flings += 1
                    outcome = "scrolled"
                elif kind == "back":
                    self._driver.back()
                    outcome = "went_back"
                elif kind in ("type", "input_text"):
                    text_value = str(action.get("text", ""))
                    if text_value:
                        self._driver.input_text(text_value)
                        outcome = "typed"
                elif kind == "tap":
                    x, y = action.get("x"), action.get("y")
                    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                        self._driver.tap(int(x), int(y))
                        taps += 1
                        outcome = "tapped"
                    else:
                        # A label is more useful than a coordinate: the model
                        # cannot measure pixels, but it can read the tree. Raw
                        # coordinates were the only accepted form, so every
                        # model that replied {"action":"tap","label":...} was
                        # silently unparsable and the loop never advanced.
                        label = action.get("label") or action.get("text")
                        if not label:
                            outcome = "unsupported_action"
                        else:
                            hit = _find_candidate(str(label), list(candidates))
                            if hit is None:
                                outcome = "label_not_found"
                            else:
                                cx, cy = hit.center()
                                self._driver.tap(cx, cy)
                                taps += 1
                                outcome = "tapped_by_label"
                elif kind in ("done", "finish", "success"):
                    # The planner's word is not proof, and neither is the text
                    # on screen unless we are still inside the app being driven.
                    if self._left_expected_app(package):
                        report.reason = (
                            f"planner reported done but the phone left "
                            f"{self.expect_package!r} (now {package!r})"
                        )
                        outcome = "done_rejected"
                        break
                    if self._ask_verifier(goal, fingerprint, [str(t).lower() for t in texts]):
                        report.success = True
                        report.reason = "planner reported done and the verifier agreed"
                        break
                    outcome = "done_rejected"
                else:
                    outcome = "unsupported_action"

                # Remember what was actually done, so the next prompt can ask
                # for something different instead of repeating this action.
                if outcome not in ("unparsable", "unsupported_action"):
                    detail = str(action.get("label") or action.get("text") or "")
                    self._planner_tried.add(f"{kind}:{detail or 'x,y'}")

            self.steps.append(
                StepRecord(
                    index=index,
                    action=f"plan:{kind}" if kind else "plan",
                    detail=answer[:120],
                    tier="big",
                    seconds=time.monotonic() - t0,
                    result=outcome,
                )
            )

        else:
            report.reason = f"max_steps ({self.max_steps}) reached"

        report.steps = list(self.steps)
        report.appmap = self.map.to_dict()
        report.model_stats = self._models.stats.to_dict()
        report.duration_seconds = time.monotonic() - started
        report.flings = flings
        report.taps = taps
        return report


# --------------------------------------------------------------------------- #
# helpers (pure, unit-testable)
# --------------------------------------------------------------------------- #


def _parse_action(answer: str) -> dict[str, Any] | None:
    """Pull a single JSON action object out of a planner reply.

    Models wrap JSON in prose or code fences often enough that a strict
    ``json.loads`` would discard good plans, so the first balanced object is
    accepted.
    """
    if not answer:
        return None
    text = answer.strip()
    fence = text.find("```")
    if fence != -1:
        rest = text[fence + 3 :]
        if rest[:4].lower().startswith("json"):
            rest = rest[4:]
        end = rest.find("```")
        if end != -1:
            text = rest[:end]
    text = text.strip()

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : index + 1])
                    except (ValueError, TypeError):
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    return None


#: Words that describe the thing rather than name it. Dropped from the front
#: of an extracted target so "open page Sounds" looks for "Sounds", not
#: "page Sounds" — a label that appears nowhere on screen.
_TARGET_FILLER = frozenset(
    {
        "the", "a", "an", "item", "row", "page", "screen", "option", "setting",
        "account", "profile", "user", "contact", "chat",
        "صفحة", "شاشة", "العنصر", "إعداد", "اعداد", "خيار", "صف", "القسم",
        "الحساب", "حساب", "الملف", "ملف", "المستخدم", "مستخدم", "الدردشة",
    }
)


def _extract_target(goal: str) -> str | None:
    """Pull the target phrase out of a natural-language goal.

    This is a heuristic for the fast path, not a parser; when it fails the
    loop falls back to the planner. Two rules learned from live use:

    - A single trailing token is too small an anchor. "open اهتزاز المكالمات"
      is one label on screen, and "المكالمات" also matches call-history rows,
      so the tap lands on the wrong widget. Keep the whole trailing phrase.
    - Verb prefixes ("open", "افتح", "go to") are dropped, because the verb is
      an instruction rather than part of the label being looked for.
    """
    if not goal:
        return None
    text = goal.strip()

    # A goal is often several instructions: "ابحث عن الحساب serveai ثم افتح
    # صفحته". The thing to look for on the current screen is the *last*
    # instruction's object, because the earlier steps are what got the agent
    # here. Splitting on a coordinating word keeps the trailing target and
    # discards the clause that described how to get there.
    for separator in (" ثم ", " وثم ", "ثم ", " and then ", " then "):
        if separator in text:
            text = text.rsplit(separator, 1)[1]
            break

    # drop leading instruction verbs, in either language
    verbs = (
        "open the ", "open ", "go to ", "navigate to ", "scroll to ",
        "tap the ", "tap ", "find ", "search for ", "show ", "select ",
        "افتح ", "افتح صفحة ", "اذهب إلى ", "انتقل إلى ", "ابحث عن ",
        "اعرض ", "اختر ", "أظهر ",
    )
    lowered = text.lower()
    for verb in verbs:
        if lowered.startswith(verb):
            text = text[len(verb) :]
            lowered = lowered[len(verb) :]
            break
    text = text.strip().strip(".,!?؟")
    if not text:
        return None

    # Explicit quotes/markers win outright. The label is returned lower-cased:
    # matching is case-insensitive anyway, and a lower-cased target keeps the
    # step records and the planner prompt stable across input casing.
    for marker in ("named ", "called ", "المسماة ", "بعنوان "):
        if marker in lowered:
            index = lowered.index(marker) + len(marker)
            word = lowered[index:].strip().split()[0] if lowered[index:].strip() else ""
            word = word.strip(".,!?؟")
            if word:
                return word

    # A multi-word tail is a better anchor than one token: keep it whole, but
    # drop short leading filler ("the", "the item").
    tokens = [t for t in text.split() if t]
    while len(tokens) > 1 and tokens[0].lower() in _TARGET_FILLER:
        tokens = tokens[1:]
    if not tokens:
        return None
    return " ".join(tokens).lower()


def _find_candidate(target: str, candidates: list[GroundCandidate]) -> GroundCandidate | None:
    """Best UI-tree match for ``target``, preferring something tappable.

    Accessibility trees repeat a label on non-interactive wrappers (a hint bar,
    a tooltip, a disabled row), so a plain substring match happily returns a
    label that cannot be clicked. Clickable rows win; a non-clickable match is
    only used when nothing better exists.
    """
    lowered = target.strip().lower()
    exact_clickable: GroundCandidate | None = None
    exact_any: GroundCandidate | None = None
    partial_clickable: GroundCandidate | None = None
    partial_any: GroundCandidate | None = None

    for cand in candidates:
        labels = [
            label.lower()
            for label in (cand.text, cand.content_desc, cand.resource_id)
            if label
        ]
        if not labels:
            continue
        exact = any(label == lowered for label in labels)
        partial = any(lowered in label for label in labels)
        if not partial:
            continue
        if exact and cand.clickable and exact_clickable is None:
            exact_clickable = cand
        elif exact and exact_any is None:
            exact_any = cand
        elif partial and cand.clickable and partial_clickable is None:
            partial_clickable = cand
        elif partial and partial_any is None:
            partial_any = cand

    for candidate in (exact_clickable, partial_clickable, exact_any, partial_any):
        if candidate is not None:
            return candidate
    return None


def _build_planner_prompt(
    goal: str,
    package: str | None,
    texts: list[str],
    map_: AppMap,
    repeats: int,
    tried: list[str] | None = None,
) -> str:
    lines = [
        f"Goal: {goal}",
        f"Current package: {package or 'unknown'}",
        f"Known screens: {len(map_)}",
    ]
    current = map_.current
    if current is not None:
        lines.append(f"At bottom of this screen: {map_.reached_bottom()}")
        lines.append(f"Times on this screen: {current.visit_count}")
    if texts:
        shown = ", ".join(texts[:12])
        lines.append(f"Visible text: {shown}")
    if repeats > 1:
        lines.append(f"Note: this exact screen has been seen {repeats} times.")
    if tried:
        lines.append(
            "Already tried on this screen and it did not get you closer: "
            + ", ".join(tried[:6])
            + ". Choose a DIFFERENT action; repeating one of these will not help."
        )
    lines.append(
        'Reply with ONE JSON object using the "action" key, e.g. '
        '{"action": "tap", "label": "visible row text"} or '
        '{"action": "scroll", "direction": "down"} or '
        '{"action": "type", "text": "hello"} or '
        '{"action": "back"} or '
        '{"action": "done"}.'
    )
    lines.append(
        "Prefer tapping by label over raw coordinates: you can read the text "
        "above but you cannot measure pixels."
    )
    lines.append(
        "Navigate one step at a time: pick the single action that makes the "
        "most progress toward the goal from the text you can see now."
    )
    return "\n".join(lines)
