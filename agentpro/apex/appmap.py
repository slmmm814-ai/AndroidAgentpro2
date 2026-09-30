"""APEX appmap — app-state graph and scroll-position memory.

The v2 planner re-decides what to do from a single screen, which is why it can
oscillate (scroll up, scroll down) or retry a dead end forever. ``AppMap`` adds
the one thing the loop was missing: **memory of where it has already been**.

Concepts:

* ``ScreenNode``   — one screen the agent has seen, keyed by a stable
                     fingerprint. Carries the package, a guess at the screen's
                     role, and *scroll facts*: which directions were already
                     exhausted.
* ``AppMap``       — the graph. Records observations, answers the two questions
                     the planner asks every step:

        - "Have I already been to the bottom of this screen?"
        - "Which action took me from screen A to screen B?"

  Both answers are pure structure — no model call, no device round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


# --------------------------------------------------------------------------- #
# scroll facts
# --------------------------------------------------------------------------- #


@dataclass
class ScrollFacts:
    """What the agent learned about scrolling on one screen."""

    reached_bottom: bool = False
    reached_top: bool = True
    last_direction: str | None = None
    consecutive_no_move: int = 0
    max_depth_seen: int = 0
    """How many distinct pages were observed below the anchor screen."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reached_bottom": self.reached_bottom,
            "reached_top": self.reached_top,
            "last_direction": self.last_direction,
            "consecutive_no_move": self.consecutive_no_move,
            "max_depth_seen": self.max_depth_seen,
        }


# --------------------------------------------------------------------------- #
# screen nodes
# --------------------------------------------------------------------------- #


@dataclass
class ScreenNode:
    """One screen the agent has visited."""

    fingerprint: str
    package: str | None = None
    activity: str | None = None
    title: str | None = None
    visit_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    scroll: ScrollFacts = field(default_factory=ScrollFacts)
    visible_texts: tuple[str, ...] = ()
    transitions: dict[str, str] = field(default_factory=dict)
    """Action digest -> fingerprint of the screen it led to."""

    @property
    def key(self) -> str:
        return self.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "package": self.package,
            "activity": self.activity,
            "title": self.title,
            "visit_count": self.visit_count,
            "scroll": self.scroll.to_dict(),
            "visible_texts": list(self.visible_texts[:10]),
            "transitions": dict(self.transitions),
        }


# --------------------------------------------------------------------------- #
# the map
# --------------------------------------------------------------------------- #


class AppMap:
    """In-memory app-state graph.

    The map is deliberately tiny and dependency-free: it is pure bookkeeping so
    the planner's questions can be answered without any model call.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, ScreenNode] = {}
        self._current_key: str | None = None
        self._anchor_key: str | None = None

    # -- properties -------------------------------------------------------- #

    @property
    def size(self) -> int:
        return len(self._nodes)

    @property
    def current(self) -> ScreenNode | None:
        if self._current_key is None:
            return None
        return self._nodes.get(self._current_key)

    @property
    def anchor(self) -> ScreenNode | None:
        if self._anchor_key is None:
            return None
        return self._nodes.get(self._anchor_key)

    def __contains__(self, fingerprint: object) -> bool:
        return fingerprint in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self) -> Iterator[ScreenNode]:
        return iter(self._nodes.values())

    # -- observation ------------------------------------------------------- #

    def observe(
        self,
        fingerprint: str,
        *,
        package: str | None = None,
        activity: str | None = None,
        title: str | None = None,
        visible_texts: tuple[str, ...] = (),
        now: float = 0.0,
        is_anchor: bool = False,
    ) -> ScreenNode:
        """Record (or refresh) a screen and make it the current screen."""
        node = self._nodes.get(fingerprint)

        if node is None:
            node = ScreenNode(
                fingerprint=fingerprint,
                package=package,
                activity=activity,
                title=title,
                first_seen=now,
                visible_texts=visible_texts,
            )
            self._nodes[fingerprint] = node

        node.visit_count += 1
        node.last_seen = now
        if package:
            node.package = package
        if activity:
            node.activity = activity
        if title:
            node.title = title
        if visible_texts:
            node.visible_texts = visible_texts

        self._current_key = fingerprint
        if is_anchor:
            self._anchor_key = fingerprint
        return node

    def mark_anchor(self, fingerprint: str | None = None) -> None:
        """Set the anchor (entry) screen, defaults to the current screen."""
        self._anchor_key = fingerprint or self._current_key

    def reset_anchor(self) -> None:
        self._anchor_key = None

    # -- scroll memory ----------------------------------------------------- #

    def record_scroll(
        self,
        direction: str,
        *,
        moved: bool,
        fingerprint: str | None = None,
    ) -> ScrollFacts:
        """Update scroll facts after a fling. Returns the facts for the screen."""
        key = fingerprint or self._current_key
        if key is None:
            return ScrollFacts()
        node = self._nodes.setdefault(key, ScreenNode(fingerprint=key))
        facts = node.scroll

        facts.last_direction = direction

        if moved:
            facts.consecutive_no_move = 0
            if direction == "down":
                facts.reached_bottom = False
                facts.reached_top = False
                facts.max_depth_seen += 1
            elif direction == "up":
                facts.reached_top = False
                facts.reached_bottom = False
        else:
            facts.consecutive_no_move += 1
            if direction == "down":
                # A downward fling that changed nothing means we are at the
                # bottom of this list.
                facts.reached_bottom = True
            elif direction == "up":
                facts.reached_top = True

        return facts

    def reached_bottom(self, fingerprint: str | None = None) -> bool:
        node = self._nodes.get(fingerprint or self._current_key or "")
        return bool(node and node.scroll.reached_bottom)

    def reached_top(self, fingerprint: str | None = None) -> bool:
        node = self._nodes.get(fingerprint or self._current_key or "")
        return bool(node and node.scroll.reached_top)

    # -- transitions ------------------------------------------------------- #

    def record_transition(self, action_digest: str, to_fingerprint: str) -> None:
        if self._current_key is None:
            return
        node = self._nodes.setdefault(
            self._current_key, ScreenNode(fingerprint=self._current_key)
        )
        node.transitions[action_digest] = to_fingerprint

    def known_transition(self, action_digest: str) -> str | None:
        node = self.current
        if node is None:
            return None
        return node.transitions.get(action_digest)

    def visited(self, fingerprint: str) -> int:
        node = self._nodes.get(fingerprint)
        return node.visit_count if node else 0

    # -- serialisation ----------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "current": self._current_key,
            "anchor": self._anchor_key,
            "nodes": {key: node.to_dict() for key, node in self._nodes.items()},
        }
