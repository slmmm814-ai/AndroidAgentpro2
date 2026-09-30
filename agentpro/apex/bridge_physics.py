"""Bridge-backed ``PhysicsDriver``.

Adapts the existing ``FastDriver``/``ScreenReader`` stack to the small
``PhysicsDriver`` protocol the physics primitives consume, so the primitives
never see a bridge, a socket, or a model.
"""

from __future__ import annotations

from typing import Any

from ..fast_driver import FastDriver


class BridgePhysicsDriver:
    """Real-device implementation of ``agentpro.apex.physics.PhysicsDriver``."""

    def __init__(self, driver: FastDriver | None = None) -> None:
        self._driver = driver or FastDriver()
        self._size: tuple[int, int] | None = None

    def display_size(self) -> tuple[int, int]:
        """Real display size, cached; falls back to the UI-tree rectangle."""
        if self._size is not None:
            return self._size
        resolved: tuple[int, int] | None = None
        try:
            resolved = self._driver.client.screen_size()
        except Exception:  # noqa: BLE001 - offline/fake drivers
            resolved = None
        if resolved is None:
            left, top, right, bottom = snapshot_bounds(self._driver.observe())
            resolved = (max(1, right - left), max(1, bottom - top))
        self._size = resolved
        return resolved

    # -- PhysicsDriver protocol ------------------------------------------- #

    def swipe(self, direction: str, distance: int = 900) -> dict[str, Any]:
        # The bridge ``swipe`` moves content toward ``direction``; reuse the
        # same coordinate math the scroll tool uses.
        width, height = self.display_size()
        cx, cy = width // 2, height // 2

        if direction == "down":
            y1, y2 = cy + distance // 2, cy - distance // 2
            x1 = x2 = cx
        elif direction == "up":
            y1, y2 = cy - distance // 2, cy + distance // 2
            x1 = x2 = cx
        else:
            raise ValueError(f"unsupported direction {direction!r}")

        y1 = max(0, min(height - 1, y1))
        y2 = max(0, min(height - 1, y2))
        x1 = max(0, min(width - 1, x1))
        x2 = max(0, min(width - 1, x2))

        self._driver.client.swipe(x1, y1, x2, y2, 400)
        self._driver.invalidate()
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}

    def fingerprint(self) -> str:
        return self._driver.observe(force=True).fingerprint or ""

    def screen_height(self) -> int:
        return self.display_size()[1]

    def screen_width(self) -> int:
        return self.display_size()[0]

    def find_text(self, text: str) -> tuple[int, int] | None:
        lowered = text.strip().lower()
        for el in self._driver.observe(force=True).elements:
            hay = ((el.text or "") + " " + (el.content_desc or "")).strip().lower()
            if lowered in hay:
                return el.center()
        return None

    def visible_texts(self) -> list[str]:
        out: list[str] = []
        for el in self._driver.observe(force=True).elements:
            label = (el.text or el.content_desc or "").strip()
            if label:
                out.append(label)
        return out


# --------------------------------------------------------------------------- #
# helpers (kept dependency-free and unit-testable)
# --------------------------------------------------------------------------- #


def snapshot_bounds(value: Any) -> tuple[int, int, int, int]:
    """Best-effort (left, top, right, bottom) covering a snapshot's content.

    ``ScreenSnapshot`` does not expose the root window bounds directly, so we
    derive the usable rectangle from the union of the visible elements.
    """
    try:
        elements = list(value.elements)  # type: ignore[attr-defined]
    except Exception:
        return 0, 0, 1080, 2400

    if not elements:
        return 0, 0, 1080, 2400

    left = min(el.left for el in elements)
    top = min(el.top for el in elements)
    right = max(el.right for el in elements)
    bottom = max(el.bottom for el in elements)
    return int(left), int(top), int(right), int(bottom)


def snapshot_height(value: Any) -> int:
    _, top, _, bottom = snapshot_bounds(value)
    height = bottom - top
    return height if height > 0 else 2400


def snapshot_width(value: Any) -> int:
    left, _, right, _ = snapshot_bounds(value)
    width = right - left
    return width if width > 0 else 1080
