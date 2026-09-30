"""Bridge + Gemini wiring for the APEX grounder.

This is the integration glue: it converts a live ``FastDriver`` snapshot into
``GroundCandidate`` list, calls the hybrid grounder, and — when the tree cannot
name the target — asks the Gemini vision model for a point, then validates /
snaps it against the real tree. This is the exact path that turns the ~200px
miss we measured into a usable tap.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..fast_driver import FastDriver
from .grounder import GroundCandidate, GroundingResult, HybridGrounder

_LOGGER = logging.getLogger("agentpro.apex.vision")


@dataclass
class GeminiConfig:
    """Where and how to reach the vision model."""

    api_key: str
    model: str = "gemini-3.6-flash"
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    timeout: float = 90.0
    thinking_budget: int = 0


class GeminiGrounder:
    """``VisionGrounder`` backed by Google Gemini."""

    def __init__(self, config: GeminiConfig) -> None:
        self._config = config
        self.call_count = 0

    def ground(
        self,
        screenshot_b64: str,
        goal: str,
        *,
        width: int,
        height: int,
    ) -> tuple[int, int] | None:
        """Ask Gemini for the tap point. Returns None on any failure."""
        self.call_count += 1

        prompt = (
            f"You are a phone GUI grounding model. The screenshot is "
            f"{width}x{height} portrait, origin top-left. "
            f"Goal: '{goal}'. "
            "Reply with ONLY a JSON object: {\"x\": int, \"y\": int, \"element\": str} "
            "for the single best tap point. No other text."
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "image/png", "data": screenshot_b64}},
                    ]
                }
            ],
            "generationConfig": {
                "maxOutputTokens": 1024,
                "temperature": 0.1,
                "thinkingConfig": {"thinkingBudget": self._config.thinking_budget},
            },
        }

        url = (
            f"{self._config.endpoint}/models/{self._config.model}"
            f":generateContent?key={self._config.api_key}"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout) as response:
                body = response.read().decode()
        except Exception as exception:  # noqa: BLE001 - any network/model failure
            _LOGGER.warning("gemini grounding failed: %s", exception)
            return None

        try:
            payload_out = json.loads(body)
            parts = payload_out["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
        except Exception:  # noqa: BLE001
            return None

        return _parse_point(text)


def _parse_point(text: str) -> tuple[int, int] | None:
    """Pull the first (x, y) pair out of a model response."""
    numbers: list[int] = []
    current = ""
    for char in text:
        if char.isdigit() or (char == "-" and not current):
            current += char
        else:
            if current:
                numbers.append(int(current))
                current = ""
                if len(numbers) >= 2:
                    break
    if current:
        numbers.append(int(current))

    if len(numbers) < 2:
        return None

    x, y = numbers[0], numbers[1]
    if x < 0 or y < 0:
        return None
    return (x, y)


def candidates_from_snapshot(snapshot: Any) -> list[GroundCandidate]:
    """Convert a ``ScreenSnapshot`` into grounding candidates."""
    out: list[GroundCandidate] = []
    try:
        elements = list(snapshot.elements)
    except Exception:
        return out

    for el in elements:
        out.append(
            GroundCandidate(
                text=getattr(el, "text", "") or "",
                content_desc=getattr(el, "content_desc", "") or "",
                resource_id=getattr(el, "resource_id", "") or "",
                bounds=(
                    int(el.left),
                    int(el.top),
                    int(el.right),
                    int(el.bottom),
                ),
                clickable=bool(getattr(el, "clickable", False)),
                editable=bool(getattr(el, "editable", False)),
            )
        )
    return out


class BridgeGrounder:
    """One-call grounder over the live bridge: snapshot -> ground -> ready to tap."""

    def __init__(
        self,
        driver: FastDriver | None = None,
        *,
        vision: GeminiGrounder | None = None,
        snap_threshold_px: float = 120.0,
        max_snap_px: float = 400.0,
    ) -> None:
        self._driver = driver or FastDriver()
        self._grounder = HybridGrounder(
            vision,
            snap_threshold_px=snap_threshold_px,
            max_snap_px=max_snap_px,
        )

    @property
    def grounder(self) -> HybridGrounder:
        return self._grounder

    def ground(self, target: str) -> GroundingResult:
        """Resolve ``target`` against the current screen."""
        snapshot = self._driver.observe(force=True)

        screenshot_b64: str | None = None
        try:
            response = self._driver.client.command("screenshot")
            screenshot_b64 = (response.data or {}).get("base64")
        except Exception:  # noqa: BLE001
            screenshot_b64 = None

        width = max((el.right for el in snapshot.elements), default=1080)
        height = max((el.bottom for el in snapshot.elements), default=2340)

        result = self._grounder.ground(
            target,
            candidates_from_snapshot(snapshot),
            screenshot_b64=screenshot_b64,
            width=int(width),
            height=int(height),
        )
        _LOGGER.info("ground(%s) -> %s", target, result.to_dict())
        return result

    def screenshot_png(self) -> bytes | None:
        """Return the current screen as PNG bytes, or None."""
        try:
            response = self._driver.client.command("screenshot")
            b64 = (response.data or {}).get("base64")
            return base64.b64decode(b64) if b64 else None
        except Exception:  # noqa: BLE001
            return None
