"""Strong screen observation layer.

Turns a raw bridge ``ui_dump`` (plus optional screenshot and ``get_window``)
into a filtered, cheap, LLM-ready view of the device:

* interactive elements with bounds and labels (``InteractiveElement``)
* a stable screen ``fingerprint`` for change detection
* a compact text summary for the model (context budgeting)
* a smart ``wait_for_*`` helper built on polling instead of fixed sleeps

This layer never talks to the model; it only inspects the device.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .models import Observation


BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True)
class InteractiveElement:
    """One affordance on screen with everything the planner needs."""

    node_index: int
    class_name: str
    package: str
    resource_id: str
    text: str
    content_desc: str
    bounds: tuple[int, int, int, int]
    clickable: bool = False
    scrollable: bool = False
    editable: bool = False
    checked: bool = False
    selected: bool = False
    focused: bool = False
    enabled: bool = True
    visible_to_user: bool = True

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

    def area(self) -> int:
        l, t, r, b = self.bounds
        return max(0, r - l) * max(0, b - t)

    @property
    def label(self) -> str:
        return self.text or self.content_desc or self.resource_id

    def to_line(self) -> str:
        kind = str(self.class_name).split(".")[-1] or "node"
        flags = []
        if self.clickable:
            flags.append("clickable")
        if self.editable:
            flags.append("editable")
        if self.scrollable:
            flags.append("scrollable")
        if self.checked:
            flags.append("checked")
        if not self.enabled:
            flags.append("disabled")
        parts = [f"[{self.node_index}] {kind}"]
        if self.text:
            parts.append(f'text="{self.text}"')
        if self.content_desc:
            parts.append(f'desc="{self.content_desc}"')
        if self.resource_id:
            parts.append(f'id="{self.resource_id}"')
        parts.append(
            f"bounds=({self.left},{self.top},{self.right},{self.bottom})"
        )
        if flags:
            parts.append("|".join(flags))
        return " ".join(parts)


@dataclass(frozen=True)
class ScreenSnapshot:
    """A filtered, ready-to-use view of one observation."""

    success: bool
    observation: Observation
    package_name: str | None = None
    activity_name: str | None = None
    window_title: str | None = None
    elements: tuple[InteractiveElement, ...] = ()
    fingerprint: str | None = None
    tree_truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None

    def __bool__(self) -> bool:
        return self.success

    def visible_text(self, limit: int = 120) -> list[str]:
        out = []
        for el in self.elements:
            label = (el.text or "").strip()
            if label:
                out.append(label)
            if len(out) >= limit:
                break
        return out

    def find(self, **flags: Any) -> list[InteractiveElement]:
        matches: list[InteractiveElement] = []
        for el in self.elements:
            ok = True
            for key, value in flags.items():
                if not hasattr(el, key):
                    ok = False
                    break
                if getattr(el, key) != value:
                    ok = False
                    break
            if ok:
                matches.append(el)
        return matches


class ScreenReadError(RuntimeError):
    """Raised when the screen cannot be read at all."""


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        return default
    return default


def _parse_bounds(value: Any) -> tuple[int, int, int, int] | None:
    if isinstance(value, str):
        match = BOUNDS_RE.search(value)
        if match:
            try:
                return tuple(int(g) for g in match.groups())  # type: ignore[return-value]
            except ValueError:
                return None
        return None
    if isinstance(value, Mapping):
        try:
            return (
                int(value.get("left", 0)),
                int(value.get("top", 0)),
                int(value.get("right", 0)),
                int(value.get("bottom", 0)),
            )
        except (TypeError, ValueError):
            return None
    return None


def _walk(node: Any):
    """Depth-first traversal yielding every node mapping."""
    if isinstance(node, Mapping):
        yield node
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                yield from _walk(child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def extract_elements(ui_root: Mapping[str, Any] | None) -> list[InteractiveElement]:
    """Flatten a bridge UI tree into interactive/text elements."""
    elements: list[InteractiveElement] = []
    if not isinstance(ui_root, Mapping):
        return elements

    index = 0
    for node in _walk(ui_root):
        cls = (
            node.get("class")
            or node.get("className")
            or node.get("class_name")
            or ""
        )
        text = node.get("text") or node.get("text_value") or ""
        if not isinstance(text, str):
            text = str(text)
        desc = (
            node.get("content_desc")
            or node.get("contentDescription")
            or node.get("content_description")
            or ""
        )
        if not isinstance(desc, str):
            desc = str(desc)
        resource_id = (
            node.get("resource_id")
            or node.get("resourceId")
            or node.get("view_id_resource_name")
            or ""
        )
        package = node.get("package") or node.get("package_name") or ""
        bounds = _parse_bounds(node.get("bounds"))
        if bounds is None:
            continue

        visible_to_user = _coerce_bool(node.get("visible_to_user"), default=True)
        if not visible_to_user:
            continue
        enabled = _coerce_bool(node.get("enabled"), default=True)

        clickable = _coerce_bool(node.get("clickable"))
        scrollable = _coerce_bool(node.get("scrollable"))
        editable = _coerce_bool(
            node.get("editable")
            or node.get("is_editable")
            or cls.endswith("EditText")
        )
        checked = _coerce_bool(node.get("checked"))
        selected = _coerce_bool(node.get("selected"))
        focused = _coerce_bool(node.get("focused") or node.get("is_focused"))

        lower_cls = str(cls).lower()
        is_container = (
            "layout" in lower_cls
            or "viewpager" in lower_cls
            or "scrollview" in lower_cls
            or "webserver" in lower_cls
        )
        has_label = bool(text.strip()) or bool(desc.strip())

        if not (clickable or scrollable or editable or has_label or checked):
            continue

        l, t, r, b = bounds
        if r <= l or b <= t:
            continue

        element = InteractiveElement(
            node_index=index,
            class_name=str(cls),
            package=str(package),
            resource_id=str(resource_id),
            text=text.strip(),
            content_desc=desc.strip(),
            bounds=bounds,
            clickable=clickable,
            scrollable=scrollable,
            editable=editable,
            checked=checked,
            selected=selected,
            focused=focused,
            enabled=enabled,
            visible_to_user=True,
        )

        if is_container and not has_label and not clickable:
            continue

        elements.append(element)
        index += 1

    elements.sort(
        key=lambda el: (
            el.top,
            el.left,
            not el.clickable,
            not (el.editable or bool(el.label)),
        )
    )
    for i, el in enumerate(elements):
        elements[i] = InteractiveElement(
            node_index=i,
            class_name=el.class_name,
            package=el.package,
            resource_id=el.resource_id,
            text=el.text,
            content_desc=el.content_desc,
            bounds=el.bounds,
            clickable=el.clickable,
            scrollable=el.scrollable,
            editable=el.editable,
            checked=el.checked,
            selected=el.selected,
            focused=el.focused,
            enabled=el.enabled,
            visible_to_user=el.visible_to_user,
        )
    return elements


def compute_fingerprint(elements: Sequence[InteractiveElement]) -> str:
    """Stable, screen-position-tolerant fingerprint for change detection.

    NOTE: this signature folds ``bucket_x``/``bucket_y`` (centre // 85) into a
    single hash, so it answers "did *something* change", not "did the screen
    move". A scroll that keeps every element inside the same 85px bucket is
    invisible here. For "did the screen actually move", use
    :func:`visual_hash` instead, which is an independent channel.
    """
    signature: list[str] = []
    for el in elements:
        cx = (el.left + el.right) // 2
        cy = (el.top + el.bottom) // 2
        bucket_x = cx // 85
        bucket_y = cy // 85
        signature.append(
            "|".join(
                [
                    str(el.class_name.split(".")[-1]),
                    (el.text or "")[:80],
                    (el.content_desc or "")[:80],
                    str(bucket_x),
                    str(bucket_y),
                    "1" if el.clickable else "0",
                    "1" if el.editable else "0",
                ]
            )
        )
    raw = "\n".join(signature)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class ScreenChange:
    """Result of comparing two observations across independent channels.

    ``moved`` answers "did the screen visibly change" from pixels, which is a
    measurement rather than an inference from the element tree. It is
    ``None`` when no frame could be hashed, which is deliberately distinct
    from ``False``: a missing measurement must never be reported as "nothing
    moved". The pixel channel cannot say *what* changed, only that something did.
    """

    moved: bool | None
    distance: int | None
    new_content_ratio: float
    container_shrank: bool
    before_container_bottom: int | None
    after_container_bottom: int | None

    @property
    def settled(self) -> bool:
        """True only when pixels were compared and nothing moved or appeared.

        Returns ``False`` while the pixel channel is unknown, so an
        uncapturable screen can never look settled.
        """
        return self.moved is False and self.new_content_ratio == 0.0

    @property
    def moved_without_new_content(self) -> bool:
        """Pixels changed while the element tree showed no new content.

        Consistent with a scroll that re-showed known content. Not proof of a
        scroll: any other visual change without new text also lands here.
        """
        return self.moved is True and self.new_content_ratio == 0.0

    @property
    def content_changed(self) -> bool:
        """True when new content appeared (a page load, a new message)."""
        return self.new_content_ratio > 0.0


def element_signature(element: InteractiveElement) -> tuple[str, str, str]:
    """Content identity of an element, with no positional information.

    Two screens showing the same text compare equal regardless of where the
    text sits, which is what makes "did new content arrive" separable from
    "did the screen move".
    """
    return (
        str(element.class_name).split(".")[-1],
        (element.text or "")[:80],
        (element.content_desc or "")[:80],
    )


def content_set(elements: Sequence[InteractiveElement]) -> set[tuple[str, str, str]]:
    """Set of :func:`element_signature` values for the screen's *content*.

    Nodes carrying neither text nor a content description are structural
    (layout containers, dividers) and are excluded. They never change while
    scrolling, so leaving them in the denominator would deflate every ratio
    permanently and cap it below 1.0, making any threshold unreliable.

    Limitation: identity here is text-based, so repeated identical rows (a table
    of equal values) collapse into one entry and their count is not preserved.
    The ratio answers "did text I had not seen arrive", not "how many rows are
    new".
    """
    return {
        element_signature(el)
        for el in elements
        if (el.text or "").strip() or (el.content_desc or "").strip()
    }


def new_content_ratio(
    before: Sequence[InteractiveElement],
    after: Sequence[InteractiveElement],
) -> float:
    """Share of ``after`` elements whose content was absent from ``before``.

    Scrolling re-positions existing elements and therefore scores ``0.0``;
    content that newly entered the viewport scores above ``0.0``. This is a
    different question from "did the screen move", and the two are reported
    separately on purpose.
    """
    seen = content_set(before)
    after_set = content_set(after)
    if not after_set:
        return 0.0
    return len(after_set - seen) / len(after_set)


def scroll_container_bounds(
    elements: Sequence[InteractiveElement],
) -> tuple[int, int, int, int] | None:
    """Bounds of the largest scrollable node, or ``None`` if there is none."""
    scrollables = [el for el in elements if el.scrollable]
    if not scrollables:
        return None
    return max(scrollables, key=lambda el: el.area()).bounds


def difference_hash(image: Any, size: int = 16) -> int:
    """256-bit dHash of a PIL image: robust to JPEG compression noise.

    Measured on device, ``size=16`` with a distance threshold of 6 separated an
    idle screen (0), a swipe that never reached the list (14) and a real scroll
    (32). A naive per-pixel threshold scored the same idle screen at 0.65
    "changed" purely from JPEG artefacts, so a compression-tolerant hash has to
    come before any pixel comparison can be trusted.
    """
    small = image.convert("L").resize((size + 1, size))
    pixels = small.load()
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (1 if pixels[x, y] > pixels[x + 1, y] else 0)
    return bits


def hamming_distance(left: int, right: int) -> int:
    """Number of differing bits between two hashes."""
    return bin(left ^ right).count("1")


class ScreenReader:
    """Reads the device screen through the bridge and enriches it."""

    def __init__(
        self,
        client: Any,
        *,
        include_screenshot: bool = True,
        max_elements: int = 200,
        visual_interval_s: float = 0.35,
        visual_threshold: int = 6,
    ) -> None:
        self._client = client
        self._include_screenshot = include_screenshot
        self._max_elements = max_elements
        self._visual_interval_s = visual_interval_s
        self._visual_threshold = visual_threshold
        self._last_visual_at: float | None = None
        self._last_visual_hash: int | None = None

    def get_window_info(self) -> tuple[str | None, str | None, str | None]:
        package: str | None = None
        activity: str | None = None
        title: str | None = None
        try:
            resp = self._client.get_window()
        except Exception:
            return (None, None, None)
        data = getattr(resp, "data", None)
        if not isinstance(data, Mapping):
            return (None, None, None)
        package = data.get("package_name") or data.get("package")
        activity = data.get("activity_name") or data.get("activity")
        title = data.get("window_title") or data.get("title")
        if package is not None and not isinstance(package, str):
            package = None
        if activity is not None and not isinstance(activity, str):
            activity = None
        if title is not None and not isinstance(title, str):
            title = None
        return (package, activity, title)

    def observe(self) -> ScreenSnapshot:
        package, activity, title = self.get_window_info()

        observation: Observation
        ui_root: Mapping[str, Any] | None = None
        screenshot_b64: str | None = None
        error_code: str | None = None
        error_message: str | None = None

        try:
            resp = self._client.ui_dump()
            data = getattr(resp, "data", None)
            if isinstance(data, Mapping):
                root = data.get("root")
                if isinstance(root, Mapping):
                    ui_root = root
        except Exception as exc:
            observation = Observation(
                success=False,
                error_code="OBSERVE_UI_DUMP_FAILED",
                error_message=str(exc),
            )
            return ScreenSnapshot(
                success=False,
                observation=observation,
                error_code="OBSERVE_UI_DUMP_FAILED",
                error_message=str(exc),
            )

        tree_truncated = False
        if isinstance(data, Mapping):
            tree_truncated = bool(data.get("truncated", False))

        if self._include_screenshot:
            try:
                shot = self._client.screenshot()
                shot_data = getattr(shot, "data", None)
                if isinstance(shot_data, Mapping):
                    b64 = shot_data.get("base64")
                    if isinstance(b64, str) and b64:
                        screenshot_b64 = b64
            except Exception:
                screenshot_b64 = None

        if ui_root is not None:
            root_pkg = ui_root.get("package") or ui_root.get("package_name")
            if isinstance(root_pkg, str) and package is None:
                package = root_pkg

        elements = extract_elements(ui_root)
        if self._max_elements and len(elements) > self._max_elements:
            elements = elements[: self._max_elements]

        observation = Observation(
            success=True,
            package_name=package,
            activity_name=activity,
            ui_root=ui_root,
            screenshot_base64=screenshot_b64,
            fingerprint=None,
            tree_truncated=tree_truncated,
        )

        fingerprint = compute_fingerprint(elements) if elements else None

        return ScreenSnapshot(
            success=True,
            observation=observation,
            package_name=package,
            activity_name=activity,
            window_title=title,
            elements=tuple(elements),
            fingerprint=fingerprint,
            tree_truncated=tree_truncated,
            error_code=error_code,
            error_message=error_message,
        )

    def capture_visual(self, *, force: bool = False) -> int | None:
        """Return a dHash of the current screen, or ``None`` if it failed.

        The bridge hashes the frame on device and returns 64 hex characters, so
        no image library is needed and the 300-400 KB JPEG transfer, its
        compression artefacts and its capture rate limit are all avoided. Raw
        pixels are never compared: measured on device, JPEG re-encoding made an
        idle screen differ by 0.65 under a naive per-pixel metric.

        Within ``visual_interval_s`` a cached hash is returned so callers can
        poll cheaply; pass ``force`` after performing an action to wait out the
        interval and get a genuine fresh frame. The default sits just above
        ``ScreenshotEngine.MIN_CAPTURE_INTERVAL_MS`` (250 ms) on the Android
        side, so a forced re-capture costs a fraction of a second per action
        instead of the multi-second wait a screenshot round trip would need.

        A failed capture returns ``None``, never the previous hash. Returning a
        stale hash would make a broken capture look identical to a static
        screen, which is the exact false "nothing happened" this channel exists
        to rule out.
        """
        now = time.monotonic()
        if (
            not force
            and self._last_visual_hash is not None
            and self._last_visual_at is not None
            and now - self._last_visual_at < self._visual_interval_s
        ):
            return self._last_visual_hash

        if force and self._last_visual_at is not None:
            remaining = self._visual_interval_s - (now - self._last_visual_at)
            if remaining > 0:
                time.sleep(remaining)

        try:
            response = self._client.command("visual_hash", {})
        except Exception:
            return None
        data = getattr(response, "data", None)
        if not isinstance(data, Mapping):
            return None
        encoded = data.get("hash")
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            digest = int(encoded, 16)
        except ValueError:
            return None
        self._last_visual_hash = digest
        self._last_visual_at = time.monotonic()
        return digest

    def measure_change(
        self,
        before: ScreenSnapshot | None,
        after: ScreenSnapshot | None,
        *,
        previous_visual: int | None = None,
    ) -> ScreenChange:
        """Compare two snapshots on both channels and report the result.

        Pixel distance answers whether anything moved; the content ratio
        answers whether new content arrived. Reporting them separately is what
        lets "the swipe did nothing" be told apart from "the screen moved but
        showed nothing new" — a distinction the element fingerprint alone
        cannot make.

        ``previous_visual`` must be the hash captured *before* the action. It is
        not guessed from the last cached frame: comparing against an unrelated
        frame would produce a confident verdict about the wrong baseline, so
        without it the pixel channel reports ``moved=None``.
        """
        after_hash = self.capture_visual(force=True)

        distance: int | None = None
        moved: bool | None = None
        if previous_visual is not None and after_hash is not None:
            distance = hamming_distance(previous_visual, after_hash)
            moved = distance > self._visual_threshold

        ratio = 0.0
        if before is not None and after is not None:
            ratio = new_content_ratio(before.elements, after.elements)

        before_bounds = (
            scroll_container_bounds(before.elements) if before is not None else None
        )
        after_bounds = (
            scroll_container_bounds(after.elements) if after is not None else None
        )
        before_bottom = before_bounds[3] if before_bounds is not None else None
        after_bottom = after_bounds[3] if after_bounds is not None else None
        shrank = (
            before_bottom is not None
            and after_bottom is not None
            and after_bottom < before_bottom
        )
        return ScreenChange(
            moved=moved,
            distance=distance,
            new_content_ratio=ratio,
            container_shrank=shrank,
            before_container_bottom=before_bottom,
            after_container_bottom=after_bottom,
        )

    def wait_for(
        self,
        predicate: Callable[[ScreenSnapshot], bool],
        *,
        timeout_s: float = 15.0,
        interval_s: float = 0.5,
        description: str = "condition",
        observed: Callable[[ScreenSnapshot], None] | None = None,
    ) -> tuple[bool, ScreenSnapshot]:
        """Poll until ``predicate`` is satisfied or the timeout elapses."""
        if not callable(predicate):
            raise TypeError("predicate must be callable")
        if timeout_s < 0:
            raise ValueError("timeout_s must not be negative")
        if interval_s <= 0:
            raise ValueError("interval_s must be greater than zero")

        deadline = time.monotonic() + timeout_s
        last: ScreenSnapshot | None = None

        while True:
            snapshot = self.observe()
            last = snapshot
            if snapshot.success and predicate(snapshot):
                return (True, snapshot)
            if observed is not None:
                observed(snapshot)
            if time.monotonic() >= deadline:
                break
            time.sleep(interval_s)

        if last is None:
            last = self.observe()
        return (False, last)

    def wait_for_text(
        self,
        text: str,
        *,
        timeout_s: float = 15.0,
        interval_s: float = 0.5,
    ) -> tuple[bool, ScreenSnapshot]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        needle = text.strip().lower()

        def _has_text(snapshot: ScreenSnapshot) -> bool:
            return any(needle in label.lower() for label in snapshot.visible_text())

        return self.wait_for(
            _has_text,
            timeout_s=timeout_s,
            interval_s=interval_s,
            description=f"text {text!r}",
        )

    def wait_for_stable(
        self,
        *,
        timeout_s: float = 15.0,
        stable_count: int = 2,
        interval_s: float = 0.4,
    ) -> tuple[bool, ScreenSnapshot]:
        if stable_count < 1:
            raise ValueError("stable_count must be >= 1")
        history: list[str] = []

        def _predicate(snapshot: ScreenSnapshot) -> bool:
            if snapshot.fingerprint is None:
                return False
            history.append(snapshot.fingerprint)
            if len(history) < stable_count:
                return False
            tail = history[-stable_count:]
            return len(set(tail)) == 1

        return self.wait_for(
            _predicate,
            timeout_s=timeout_s,
            interval_s=interval_s,
            description=f"stable screen x{stable_count}",
        )

    @property
    def includes_screenshot(self) -> bool:
        return self._include_screenshot


def summarize_screen(
    snapshot: ScreenSnapshot,
    *,
    max_chars: int = 3000,
    include_screenshot: bool = False,
) -> str:
    """Compact, prioritized summary of a snapshot (context budgeting).

    Priority order: editable fields first, then clickable/scrollable elements
    with a label, then any remaining labeled nodes. The output is trimmed to
    ``max_chars`` so the model never sees an unbounded dump.
    """
    if not snapshot.success:
        return f"(screen unreadable: {snapshot.error_code or 'unknown'})"

    header_parts = []
    if snapshot.package_name:
        header_parts.append(f"package={snapshot.package_name}")
    if snapshot.activity_name:
        header_parts.append(f"activity={snapshot.activity_name}")
    if snapshot.window_title:
        header_parts.append(f'title="{snapshot.window_title}"')
    if snapshot.fingerprint:
        header_parts.append(f"fp={snapshot.fingerprint}")

    header = "# " + (" ".join(header_parts) if header_parts else "screen") + "\n"
    if snapshot.tree_truncated:
        header = header.rstrip("\n") + " [tree truncated]\n"

    editable = [el for el in snapshot.elements if el.editable]
    clickable = [
        el for el in snapshot.elements if el.clickable and not el.editable
    ]
    scrollable = [
        el for el in snapshot.elements if el.scrollable and not el.editable
    ]
    rest = [
        el
        for el in snapshot.elements
        if not el.editable and not el.clickable and not el.scrollable
    ]

    lines: list[str] = [header]
    budget = max_chars - len(header)

    def _emit(group: Sequence[InteractiveElement], limit: int) -> int:
        nonlocal budget
        emitted = 0
        for el in group:
            line = el.to_line()
            if len(line) > budget:
                break
            lines.append(line)
            budget -= len(line) + 1
            emitted += 1
            if emitted >= limit:
                break
        return emitted

    _emit(editable, 12)
    _emit(clickable, 40)
    _emit(scrollable, 15)
    _emit(rest, 20)

    if len(lines) == 1:
        lines.append("(no interactable or labeled elements found)")

    return "\n".join(lines)


def fingerprint_json(*, package: str | None = None, activity: str | None = None, elements: list[dict[str, Any]] | None = None) -> str:
    """Stable fingerprint built from caller-supplied data (testable)."""
    parts: list[Any] = [package, activity]
    for el in elements or []:
        cx = (el["bounds"][0] + el["bounds"][2]) // 2
        cy = (el["bounds"][1] + el["bounds"][3]) // 2
        parts.append(
            (
                el["class_name"].split(".")[-1],
                (el["text"] or "")[:80],
                (el["content_desc"] or "")[:80],
                cx // 85,
                cy // 85,
            )
        )
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]