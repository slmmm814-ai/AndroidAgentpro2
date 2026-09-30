"""Bridge-backed ``ApexDriver`` — the live-device implementation.

This connects the APEX loop to the real bridge: snapshots come from
``FastDriver`` (UI tree), taps and scrolls go straight to the bridge, and the
vision grounder is only constructed when a vision model is configured.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from ..fast_driver import FastDriver
from .bridge_physics import BridgePhysicsDriver
from .grounder import GroundCandidate, HybridGrounder
from .physics import Physics

_LOGGER = logging.getLogger("agentpro.apex.bridge_driver")


class BridgeApexDriver:
    """Implements ``agentpro.apex.apex_agent.ApexDriver`` over the bridge."""

    def __init__(
        self,
        driver: FastDriver | None = None,
        *,
        vision_grounder: Any | None = None,
        snap_threshold_px: float = 120.0,
        max_snap_px: float = 400.0,
    ) -> None:
        self._driver = driver or FastDriver()
        self._physics_driver = BridgePhysicsDriver(self._driver)
        self._physics = Physics(self._physics_driver)
        self._grounder: HybridGrounder | None = None
        if vision_grounder is not None:
            self._grounder = HybridGrounder(
                vision_grounder,
                snap_threshold_px=snap_threshold_px,
                max_snap_px=max_snap_px,
            )

    # -- ApexDriver protocol ---------------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        snap = self._driver.observe(force=True)

        candidates: list[GroundCandidate] = []
        for el in snap.elements:
            candidates.append(
                GroundCandidate(
                    text=el.text or "",
                    content_desc=el.content_desc or "",
                    resource_id=el.resource_id or "",
                    bounds=(int(el.left), int(el.top), int(el.right), int(el.bottom)),
                    clickable=bool(el.clickable),
                    editable=bool(el.editable),
                )
            )

        return {
            "package": snap.package_name,
            "activity": snap.activity_name,
            "fingerprint": snap.fingerprint or "empty",
            "texts": [el.text or el.content_desc or "" for el in snap.elements
                      if (el.text or el.content_desc)],
            "candidates": candidates,
        }

    def screenshot_b64(self) -> str | None:
        try:
            response = self._driver.client.screenshot()
        except Exception as exc:  # noqa: BLE001
            # a missing screenshot degrades grounding, it must not be silent
            _LOGGER.warning("screenshot failed: %s", exc)
            return None
        return (response.data or {}).get("base64")

    def screen_size(self) -> tuple[int, int]:
        return self._physics_driver.display_size()

    def tap(self, x: int, y: int) -> dict[str, Any]:
        self._driver.client.tap(x, y)
        self._driver.invalidate()
        _LOGGER.info("tap (%s, %s)", x, y)
        return {"x": x, "y": y}

    def physics(self) -> Physics:
        return self._physics

    def back(self) -> dict[str, Any]:
        self._driver.client.key_event("back")
        self._driver.invalidate()
        return {}

    def input_text(self, text: str) -> dict[str, Any]:
        self._driver.client.input_text(text)
        self._driver.invalidate()
        return {"len": len(text)}

    # -- optional vision-assisted grounding ------------------------------- #

    def screenshot_png(self) -> bytes | None:
        b64 = self.screenshot_b64()
        return base64.b64decode(b64) if b64 else None

    def ground(self, target: str) -> dict[str, Any] | None:
        """Vision-assisted grounding, when a vision model is configured."""
        if self._grounder is None:
            return None
        screenshot = self.screenshot_b64()
        width, height = self.screen_size()
        result = self._grounder.ground(
            target,
            self.snapshot()["candidates"],
            screenshot_b64=screenshot,
            width=int(width),
            height=int(height),
        )
        return result.to_dict()
