"""Node-action driver — drives a window without bringing it to the foreground.

``BridgeApexDriver`` (and the whole v2 gesture stack) is built on
``dispatchGesture``, which Android delivers to the *focused* window only. That
makes true background driving impossible: scrolling the feed while another app
is in the foreground is not a coordinate problem, it is a targeting problem.

This driver replaces gesture input with accessibility *node actions*
(``ACTION_CLICK``, ``ACTION_SCROLL_FORWARD``, ``ACTION_SET_TEXT``). A node
action is delivered to the node that owns it, in whatever window that node
lives in, so the target app never has to be foreground. The window is selected
by package and addressed either by a deterministic child-index ``node_path``
or by an attribute selector.

The ``ApexDriver`` protocol is honoured so the existing APEX loop, planner and
verification can run unchanged; only the execution layer is swapped.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from agentpro.apex.physics import Physics
from agentpro.apex.physics import PhysicsDriver

from python_core.bridge_client import BridgeClient
from python_core.bridge_client import BridgeClientError

_LOGGER = logging.getLogger("agentpro.background.node_driver")


class _NodePhysicsDriver(PhysicsDriver):
    """Scrolls a fixed container with ``ACTION_SCROLL_FORWARD/BACKWARD``.

    Implements the small surface ``physics.Physics`` actually calls: a swipe
    becomes a node scroll on the container resolved at construction (or, when
    the caller supplies a fresh path, on that path).
    """

    def __init__(
        self,
        client: BridgeClient,
        package: str,
        container_path: list[int] | None = None,
    ) -> None:
        self._client = client
        self._package = package
        self._container_path = container_path

    def swipe(
        self,
        direction: str,
        distance: int = 900,
    ) -> dict[str, Any]:
        del distance  # node scrolls move one viewport, not N pixels

        if direction not in ("up", "down"):
            return {"ok": False, "reason": f"unsupported direction {direction}"}

        node_action = "scroll_forward" if direction == "up" else "scroll_backward"
        path = self._container_path or self._resolve_container()

        if not path:
            return {"ok": False, "reason": "no scrollable container found"}

        try:
            response = self._client.node_action(
                node_action,
                package=self._package,
                node_path=path,
            )
        except BridgeClientError as exc:
            _LOGGER.warning("node scroll failed: %s", exc)
            return {"ok": False, "reason": str(exc)}

        return {
            "ok": bool(response.ok),
            "action": node_action,
            "container_path": path,
            "error": response.error_code,
        }

    def _resolve_container(self) -> list[int] | None:
        try:
            paths = self._client.window_feed_paths(self._package)
        except BridgeClientError as exc:
            _LOGGER.warning("feed path lookup failed: %s", exc)
            return None

        return paths[0] if paths else None

    def fingerprint(self) -> str:
        snapshot = self._client.ui_dump_for_package(self._package)
        data = snapshot.data or {}
        root = data.get("root")

        if not isinstance(root, Mapping):
            return "unavailable"

        labels: list[str] = []

        def collect(node: Mapping[str, Any]) -> None:
            label = (node.get("text") or node.get("content_description") or "").strip()
            if isinstance(label, str) and label:
                labels.append(label[:48])

            for child in node.get("children") or ():
                if isinstance(child, Mapping):
                    collect(child)

        collect(root)

        if not labels:
            return "empty"

        import hashlib

        digest = hashlib.sha256(
            "\n".join(labels[:40]).encode("utf-8")
        ).hexdigest()

        return f"n{len(labels)}:{digest[:12]}"

    def screen_height(self) -> int:
        width, height = self._client.screen_size()
        return int(height)

    def find_text(self, text: str) -> tuple[int, int] | None:
        snapshot = self._client.ui_dump_for_package(self._package)
        data = snapshot.data or {}
        root = data.get("root")

        if not isinstance(root, Mapping):
            return None

        def search(
            node: Mapping[str, Any],
        ) -> tuple[int, int] | None:
            label = (node.get("text") or node.get("content_description") or "")
            if isinstance(label, str) and text in label:
                bounds = node.get("bounds") or {}
                left = int(bounds.get("left") or 0)
                top = int(bounds.get("top") or 0)
                right = int(bounds.get("right") or 0)
                bottom = int(bounds.get("bottom") or 0)
                return ((left + right) // 2, (top + bottom) // 2)

            for child in node.get("children") or ():
                if isinstance(child, Mapping):
                    found = search(child)
                    if found is not None:
                        return found

            return None

        return search(root)

    def visible_texts(self) -> list[str]:
        snapshot = self._client.ui_dump_for_package(self._package)
        data = snapshot.data or {}
        root = data.get("root")

        if not isinstance(root, Mapping):
            return []

        labels: list[str] = []

        def collect(node: Mapping[str, Any]) -> None:
            label = (node.get("text") or node.get("content_description") or "").strip()
            if isinstance(label, str) and label:
                labels.append(label)

            for child in node.get("children") or ():
                if isinstance(child, Mapping):
                    collect(child)

        collect(root)
        return labels


class NodeActionDriver:
    """Implements ``ApexDriver`` over node actions on a background window."""

    def __init__(
        self,
        client: BridgeClient | None = None,
        *,
        package: str,
        container_path: list[int] | None = None,
    ) -> None:
        self._client = client or BridgeClient()
        self._package = package
        self._physics_driver = _NodePhysicsDriver(
            self._client,
            package,
            container_path,
        )
        self._physics = Physics(self._physics_driver)

    # -- ApexDriver protocol ------------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        response = self._client.ui_dump_for_package(self._package)
        data = response.data or {}
        root = data.get("root")

        if not isinstance(root, Mapping):
            return {
                "package": self._package,
                "activity": None,
                "fingerprint": "unavailable",
                "texts": [],
                "candidates": [],
            }

        texts: list[str] = []
        candidates: list[dict[str, Any]] = []

        def visit(node: Mapping[str, Any], path: list[int]) -> None:
            label = (node.get("text") or node.get("content_description") or "").strip()
            if isinstance(label, str) and label:
                texts.append(label)

            bounds = node.get("bounds") or {}
            candidates.append(
                {
                    "text": node.get("text") or "",
                    "content_desc": node.get("content_description") or "",
                    "resource_id": node.get("view_id_resource_name") or "",
                    "class_name": node.get("class_name") or "",
                    "node_path": list(path),
                    "bounds": (
                        int(bounds.get("left") or 0),
                        int(bounds.get("top") or 0),
                        int(bounds.get("right") or 0),
                        int(bounds.get("bottom") or 0),
                    ),
                    "clickable": bool(node.get("clickable")),
                    "editable": bool(node.get("editable")),
                    "scrollable": bool(node.get("scrollable")),
                }
            )

            for index, child in enumerate(node.get("children") or ()):
                if isinstance(child, Mapping):
                    visit(child, path + [index])

        visit(root, [])

        return {
            "package": self._package,
            "activity": None,
            "fingerprint": f"nodes:{len(candidates)}:texts:{len(texts)}",
            "texts": texts,
            "candidates": candidates,
        }

    def screenshot_b64(self) -> str | None:
        # Screenshots capture the whole display, which for a background task
        # shows the foreground app, not the window being driven. Returning
        # None keeps the vision grounder from grounding against the wrong app.
        return None

    def screen_size(self) -> tuple[int, int]:
        response = self._client.get_window()
        data = response.data or {}
        width = data.get("width") or data.get("w")
        height = data.get("height") or data.get("h")
        try:
            if width and height and int(width) > 0 and int(height) > 0:
                return (int(width), int(height))
        except (TypeError, ValueError):
            pass
        return (1080, 2400)

    def tap(self, x: int, y: int) -> dict[str, Any]:
        """Coordinate taps cannot reach a background window.

        Callers that resolve a node should use ``tap_node`` instead. Kept for
        protocol compatibility; it fails loudly rather than silently tapping
        the foreground app.
        """
        raise NotImplementedError(
            "coordinate taps cannot target a background window; use tap_node"
        )

    def tap_node(self, node_path: list[int]) -> dict[str, Any]:
        response = self._client.node_action(
            "click",
            package=self._package,
            node_path=node_path,
        )
        return {"ok": bool(response.ok), "node_path": node_path}

    def physics(self) -> Physics:
        return self._physics

    def back(self) -> dict[str, Any]:
        response = self._client.key_event("back")
        return {"ok": bool(response.ok)}

    def input_text(self, text: str) -> dict[str, Any]:
        response = self._client.node_action(
            "set_text",
            package=self._package,
            text_argument=text,
            class_name="android.widget.EditText",
            match_index=0,
        )
        return {"ok": bool(response.ok), "len": len(text)}

    def idle(self, seconds: float) -> dict[str, Any]:
        time.sleep(max(0.0, float(seconds)))
        return {"seconds": seconds}
