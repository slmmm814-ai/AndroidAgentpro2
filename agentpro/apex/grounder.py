"""APEX grounder — hybrid tap targeting.

The experiment that motivated this layer: a general-purpose VLM asked to return
tap coordinates for "the chat named Team" pointed ~200px off the real row. The
reason is not the model's understanding — it correctly identified the element —
but that coordinate regression is a *learned* skill general image models are
not trained for.

So APEX inverts the priority order:

1. **Exact tree match** — if the target has an accessibility node (text,
   content description, resource id), tap its computed centre. Deterministic,
   100% accurate, no model.
2. **VLM fallback** — only for *pixel-only* targets that the accessibility tree
   cannot name (canvas content, image-only buttons, games). The model is asked
   for a point and the tree is used to *validate* it: we accept the point only
   if it lands inside a clickable node, and otherwise snap to the nearest
   clickable node's centre. This is what turns an imprecise model into a
   usable one.

Every decision returns a ``GroundingResult`` carrying the confidence and the
reason, so the loop can learn and the trace can explain it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

_LOGGER = logging.getLogger("agentpro.apex.grounder")


# --------------------------------------------------------------------------- #
# vision backend protocol
# --------------------------------------------------------------------------- #


class VisionGrounder(Protocol):
    """A model that maps (screenshot, goal) to a tap point."""

    def ground(self, screenshot_b64: str, goal: str, *, width: int, height: int) -> tuple[int, int] | None:
        """Return the suggested (x, y) tap point, or None if unwilling to answer."""
        ...


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GroundingResult:
    """Where and how a target was resolved."""

    target: str
    method: str  # "tree" | "vision" | "vision_snapped" | "none"
    center: tuple[int, int] | None
    confidence: float
    reason: str
    element_text: str | None = None
    element_bounds: tuple[int, int, int, int] | None = None
    vision_point: tuple[int, int] | None = None
    fallbacks_used: int = 0

    @property
    def found(self) -> bool:
        return self.center is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "method": self.method,
            "center": list(self.center) if self.center else None,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "element_text": self.element_text,
            "element_bounds": list(self.element_bounds)
            if self.element_bounds
            else None,
            "vision_point": list(self.vision_point) if self.vision_point else None,
            "fallbacks_used": self.fallbacks_used,
        }


# --------------------------------------------------------------------------- #
# candidate elements (a small structural view so tests need no device)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GroundCandidate:
    text: str
    content_desc: str
    resource_id: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    editable: bool = False

    @property
    def left(self) -> int:
        return self.bounds[0]

    @property
    def top(self) -> int:
        return self.bounds[1]

    @property
    def right(self) -> int:
        return self.bounds[2]

    @property
    def bottom(self) -> int:
        return self.bounds[3]

    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return ((l + r) // 2, (t + b) // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom

    def distance_to(self, x: int, y: int) -> float:
        cx, cy = self.center()
        return ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5

    def labels(self) -> tuple[str, ...]:
        out = []
        for value in (self.text, self.content_desc, self.resource_id):
            value = (value or "").strip()
            if value:
                out.append(value)
        return tuple(out)


# --------------------------------------------------------------------------- #
# the grounder
# --------------------------------------------------------------------------- #


class HybridGrounder:
    """Tree-first, vision-fallback tap targeting.

    Parameters control the safety rails around the vision model:

    * ``snap_threshold_px`` — if the model's point is within this distance of a
      clickable element, we snap to that element's centre instead.
    * ``max_snap_px`` — never snap further than this; a wildly wrong point is
      reported as ``none`` rather than "corrected" into meaninglessness.
    """

    def __init__(
        self,
        vision: VisionGrounder | None = None,
        *,
        snap_threshold_px: float = 120.0,
        max_snap_px: float = 400.0,
    ) -> None:
        self._vision = vision
        self.snap_threshold_px = max(0.0, snap_threshold_px)
        self.max_snap_px = max(self.snap_threshold_px, max_snap_px)
        self.stats: dict[str, int] = {
            "tree_hits": 0,
            "vision_hits": 0,
            "vision_snapped": 0,
            "misses": 0,
        }

    # -- exact tree matching ----------------------------------------------- #

    def match_tree(
        self,
        target: str,
        candidates: list[GroundCandidate],
    ) -> GroundCandidate | None:
        """Find an exact accessibility match for ``target``."""
        if not target.strip():
            return None

        lowered = target.strip().lower()
        exact: list[GroundCandidate] = []
        contains: list[GroundCandidate] = []

        for cand in candidates:
            labels = [label.lower() for label in cand.labels()]
            if not labels:
                continue
            if any(label == lowered for label in labels):
                exact.append(cand)
            elif any(lowered in label for label in labels):
                contains.append(cand)

        pool = exact or contains
        if not pool:
            return None

        # Prefer exact text, then clickable, then non-editable, then compact.
        def score(c: GroundCandidate) -> tuple[int, int, int, int]:
            exact_bonus = 0 if any(l == lowered for l in (c.text.lower(), c.content_desc.lower()) if l) else 1
            clickable_bonus = 0 if c.clickable else 1
            editable_penalty = 1 if c.editable else 0
            return (exact_bonus, clickable_bonus, editable_penalty, -_area(c))

        return sorted(pool, key=score)[0]

    # -- public entry ------------------------------------------------------- #

    def ground(
        self,
        target: str,
        candidates: list[GroundCandidate],
        *,
        screenshot_b64: str | None = None,
        width: int = 1080,
        height: int = 2340,
    ) -> GroundingResult:
        """Resolve ``target`` to a tap point, preferring the tree."""
        fallbacks = 0

        # 1) exact tree match — deterministic, no model
        hit = self.match_tree(target, candidates)
        if hit is not None:
            self.stats["tree_hits"] += 1
            return GroundingResult(
                target=target,
                method="tree",
                center=hit.center(),
                confidence=1.0,
                reason="matched accessibility element",
                element_text=hit.text or hit.content_desc or None,
                element_bounds=hit.bounds,
                fallbacks_used=fallbacks,
            )

        # 2) vision fallback — only for pixel-only targets
        if self._vision is None or not screenshot_b64:
            self.stats["misses"] += 1
            return GroundingResult(
                target=target,
                method="none",
                center=None,
                confidence=0.0,
                reason="no tree match and no vision available",
                fallbacks_used=fallbacks,
            )

        fallbacks += 1
        point = self._vision.ground(screenshot_b64, target, width=width, height=height)
        if point is None:
            self.stats["misses"] += 1
            return GroundingResult(
                target=target,
                method="none",
                center=None,
                confidence=0.0,
                reason="vision returned no point",
                vision_point=None,
                fallbacks_used=fallbacks,
            )

        gx, gy = point

        # validate against the tree: is the point already on a clickable node?
        clickables = [c for c in candidates if c.clickable]
        landed = next((c for c in clickables if c.contains(gx, gy)), None)
        if landed is not None:
            self.stats["vision_hits"] += 1
            return GroundingResult(
                target=target,
                method="vision",
                center=landed.center(),
                confidence=0.7,
                reason="vision point landed on a clickable node",
                element_text=landed.text or landed.content_desc or None,
                element_bounds=landed.bounds,
                vision_point=point,
                fallbacks_used=fallbacks,
            )

        # otherwise snap to the nearest clickable node if it is close enough
        fallbacks += 1
        nearest = min(clickables, key=lambda c: c.distance_to(gx, gy), default=None)
        if nearest is None:
            self.stats["misses"] += 1
            return GroundingResult(
                target=target,
                method="none",
                center=None,
                confidence=0.0,
                reason="vision point off-target and no clickable node to snap to",
                vision_point=point,
                fallbacks_used=fallbacks,
            )

        dist = nearest.distance_to(gx, gy)
        if dist <= self.snap_threshold_px:
            self.stats["vision_snapped"] += 1
            return GroundingResult(
                target=target,
                method="vision_snapped",
                center=nearest.center(),
                confidence=0.55,
                reason=f"vision point {dist:.0f}px from nearest clickable; snapped",
                element_text=nearest.text or nearest.content_desc or None,
                element_bounds=nearest.bounds,
                vision_point=point,
                fallbacks_used=fallbacks,
            )

        if dist <= self.max_snap_px:
            self.stats["vision_snapped"] += 1
            return GroundingResult(
                target=target,
                method="vision_snapped",
                center=nearest.center(),
                confidence=0.35,
                reason=f"vision point {dist:.0f}px off; snapped within max",
                element_text=nearest.text or nearest.content_desc or None,
                element_bounds=nearest.bounds,
                vision_point=point,
                fallbacks_used=fallbacks,
            )

        self.stats["misses"] += 1
        return GroundingResult(
            target=target,
            method="none",
            center=None,
            confidence=0.0,
            reason=f"vision point {dist:.0f}px off, beyond snap limit {self.max_snap_px:.0f}px",
            vision_point=point,
            fallbacks_used=fallbacks,
        )


def _area(c: GroundCandidate) -> int:
    return max(0, c.right - c.left) * max(0, c.bottom - c.top)
