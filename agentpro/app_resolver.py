"""Deterministic app resolution from launcher labels.

The device only accepts a package name for ``launch_app``, and package names
are vendor-specific (``com.sec.android.app.popupcalculator`` on this device,
``com.android.calculator2`` elsewhere). Guessing them from the goal reliably
fails, so this module resolves an app the way a human does: read the launcher,
match the visible label, and tap the icon.

No package guessing and no LLM involvement: the label comes from the real UI
tree, the tap lands on the real icon, and a folder is opened before its
contents are searched.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# Launcher chrome that must never be treated as an app label.
_CHROME = {
    "بحث",
    "ابحث عن التطبيقات",
    "البحث الصوتي",
    "بحث صوتي",
    "search",
    "more options",
    "المزيد من الخيارات",
    "مزيد من الخيارات",
    "apps",
    "التطبيقات",
}

_MAX_FOLDER_DEPTH = 2


def normalize_label(text: str) -> str:
    """Fold a label for tolerant matching (case, diacritics, Arabic shapes)."""
    if not isinstance(text, str):
        return ""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.casefold()
    for src, dst in (
        ("\u0623", "\u0627"),
        ("\u0625", "\u0627"),
        ("\u0622", "\u0627"),
        ("\u0649", "\u064a"),
        ("\u0629", "\u0647"),
        ("\u064f", ""),
        ("\u064e", ""),
        ("\u0650", ""),
        ("\u0652", ""),
    ):
        folded = folded.replace(src, dst)
    # Trailing launcher notification noise: "Termux، إشعار واحد".
    for sep in ("،", ",", "·"):
        if sep in folded:
            folded = folded.split(sep, 1)[0]
    return " ".join(folded.split())


def _is_chrome(label: str) -> bool:
    folded = normalize_label(label)
    if not folded:
        return True
    return folded in _CHROME or folded.startswith("مزيد") or folded.startswith("more")


@dataclass(frozen=True)
class LauncherEntry:
    label: str
    package: str | None
    resource_id: str
    left: int
    top: int
    right: int
    bottom: int
    is_folder: bool
    clickable: bool

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)


def _walk(node: Mapping[str, Any]):
    if not isinstance(node, Mapping):
        return
    yield node
    children = node.get("children")
    if isinstance(children, Sequence):
        for child in children:
            yield from _walk(child)


def collect_launcher_entries(root: Mapping[str, Any]) -> list[LauncherEntry]:
    """Extract tappable app/folder icons from a launcher UI tree."""
    entries: list[LauncherEntry] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    for node in _walk(root):
        bounds = node.get("bounds")
        if not isinstance(bounds, Mapping):
            continue
        resource_id = node.get("view_id_resource_name") or ""
        short_id = str(resource_id).split("/")[-1]
        if "icon" not in short_id.lower():
            continue
        label = (node.get("text") or "").strip() or (
            node.get("content_description") or ""
        ).strip()
        if not label or _is_chrome(label):
            continue
        try:
            left = int(bounds.get("left", 0))
            top = int(bounds.get("top", 0))
            right = int(bounds.get("right", 0))
            bottom = int(bounds.get("bottom", 0))
        except (TypeError, ValueError):
            continue
        if right <= left or bottom <= top:
            continue
        key = (normalize_label(label), left, top, right, bottom)
        if key in seen:
            continue
        seen.add(key)
        package = node.get("package_name") or None
        if package == "com.sec.android.app.launcher":
            package = None
        entries.append(
            LauncherEntry(
                label=label,
                package=package,
                resource_id=str(resource_id),
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                is_folder="folder" in short_id.lower(),
                clickable=bool(node.get("clickable", True)),
            )
        )
    return entries


def _match_score(entry_label: str, query: str) -> int:
    """0 means no match; 3 exact, 2 prefix, 1 substring."""
    label = normalize_label(entry_label)
    needle = normalize_label(query)
    if not label or not needle:
        return 0
    if label == needle:
        return 3
    if label.startswith(needle) or needle.startswith(label):
        return 2
    if needle in label or label in needle:
        return 1
    return 0


def pick_entry(
    entries: Sequence[LauncherEntry],
    query: str,
) -> LauncherEntry | None:
    best: LauncherEntry | None = None
    best_score = 0
    for entry in entries:
        score = _match_score(entry.label, query)
        if score > best_score:
            best, best_score = entry, score
    return best


class AppResolutionError(RuntimeError):
    """Raised when an app label cannot be resolved on the launcher."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LauncherAppResolver:
    """Resolve an app by its on-screen label and open it."""

    LAUNCHER_PACKAGES = ("com.sec.android.app.launcher", "com.android.launcher3")

    def __init__(
        self,
        client: Any,
        *,
        settle_seconds: float = 1.2,
        max_folder_depth: int = _MAX_FOLDER_DEPTH,
        max_folders: int = 6,
        max_pages: int = 3,
        sleep: Any = time.sleep,
    ) -> None:
        self._client = client
        self._settle = settle_seconds
        self._max_folder_depth = max(0, int(max_folder_depth))
        self._max_folders = max(0, int(max_folders))
        self._max_pages = max(1, int(max_pages))
        self._sleep = sleep

    def list_entries(self) -> list[LauncherEntry]:
        """Dump the launcher (home screen + app drawer) and list every icon.

        The drawer is paged: One UI hides icons on later pages, so the list is
        incomplete (and would report a missing app that is merely on page 2)
        unless the pages are scrolled.
        """
        self._go_home()
        self._open_drawer()
        collected: list[LauncherEntry] = []
        seen: set[tuple[str, int, int, int, int]] = set()
        labels_seen: set[str] = set()
        for page in range(self._max_pages):
            entries = self._entries_now()
            page_labels = {normalize_label(e.label) for e in entries}
            for entry in entries:
                key = (
                    normalize_label(entry.label),
                    entry.left,
                    entry.top,
                    entry.right,
                    entry.bottom,
                )
                if key in seen:
                    continue
                seen.add(key)
                collected.append(entry)
            if page and not (page_labels - labels_seen):
                break
            labels_seen |= page_labels
            if page + 1 >= self._max_pages:
                break
            self._scroll_drawer()
            self._sleep(self._settle)
        return collected

    def _go_home(self) -> None:
        self._sleep(self._settle)
        self._client.key_event("home")
        self._sleep(self._settle)

    def _foreground_package(self) -> str | None:
        response = self._client.ui_dump()
        data = getattr(response, "data", None)
        if not isinstance(data, Mapping):
            return None
        root = data.get("root")
        if not isinstance(root, Mapping):
            return None
        return root.get("package_name")

    def _is_launcher(self) -> bool:
        package = self._foreground_package()
        if package is None:
            return False
        return any(package.startswith(p) for p in self.LAUNCHER_PACKAGES)

    def _open_drawer(self) -> None:
        """Open the app drawer; recover once if the shade swallowed the swipe."""
        for attempt in range(2):
            width, height = self._dims()
            self._client.swipe(
                width // 2,
                int(height * 0.95),
                width // 2,
                int(height * 0.55),
                220,
            )
            self._sleep(self._settle)
            if self._is_launcher() or attempt:
                return
            # A quick-settings panel was pulled down instead of the drawer.
            back = getattr(self._client, "back", None)
            if callable(back):
                back()
                self._sleep(self._settle)

    def _scroll_drawer(self) -> None:
        width, height = self._dims()
        self._client.swipe(
            width // 2,
            int(height * 0.70),
            width // 2,
            int(height * 0.30),
            250,
        )

    def _dims(self) -> tuple[int, int]:
        getter = getattr(self._client, "last_screenshot_dims", None)
        if callable(getter):
            dims = getter()
            if isinstance(dims, tuple) and len(dims) == 2:
                return int(dims[0]), int(dims[1])
        return (1080, 2340)

    def _entries_now(self) -> list[LauncherEntry]:
        response = self._client.ui_dump()
        data = getattr(response, "data", None)
        if not isinstance(data, Mapping):
            return []
        root = data.get("root")
        if not isinstance(root, Mapping):
            return []
        return collect_launcher_entries(root)

    def launch(self, query: str) -> dict[str, Any]:
        """Open the app whose launcher label matches ``query``."""
        if not isinstance(query, str) or not query.strip():
            raise AppResolutionError("INVALID_ARGS", "app name must be a non-empty string")

        entries = self.list_entries()
        if not entries:
            raise AppResolutionError(
                "LAUNCHER_UNREADABLE",
                "the launcher exposed no app icons; the home screen could not be read",
            )

        return self._open_from_entries(entries, query, depth=0, trail=[])

    def _open_from_entries(
        self,
        entries: Sequence[LauncherEntry],
        query: str,
        *,
        depth: int,
        trail: list[str],
    ) -> dict[str, Any]:
        entry = pick_entry(entries, query)
        if entry is None:
            return self._search_folders(entries, query, depth=depth, trail=trail)

        x, y = entry.center
        self._client.tap(x, y)
        path = trail + [entry.label]

        if not entry.is_folder:
            return {
                "resolved_by": "launcher_label",
                "label": entry.label,
                "package": entry.package,
                "path": path,
                "x": x,
                "y": y,
            }

        if depth >= self._max_folder_depth:
            return {
                "resolved_by": "launcher_folder",
                "label": entry.label,
                "package": entry.package,
                "path": path,
                "x": x,
                "y": y,
            }

        self._sleep(self._settle)
        inner = self._entries_now()
        if not inner:
            return {
                "resolved_by": "launcher_folder",
                "label": entry.label,
                "package": entry.package,
                "path": path,
                "x": x,
                "y": y,
            }
        opened = self._open_from_entries(inner, query, depth=depth + 1, trail=path)
        opened["folder"] = entry.label
        return opened

    def _search_folders(
        self,
        entries: Sequence[LauncherEntry],
        query: str,
        *,
        depth: int,
        trail: list[str],
    ) -> dict[str, Any]:
        """Open launcher folders one by one until the app is found inside."""
        folders = [e for e in entries if e.is_folder]
        if depth < self._max_folder_depth and folders:
            for folder in folders[: self._max_folders]:
                fx, fy = folder.center
                self._client.tap(fx, fy)
                self._sleep(self._settle)
                inner = self._entries_now()
                if inner:
                    try:
                        opened = self._open_from_entries(
                            inner,
                            query,
                            depth=depth + 1,
                            trail=trail + [folder.label],
                        )
                    except AppResolutionError:
                        opened = None
                    if opened is not None:
                        opened["folder"] = folder.label
                        return opened
                self._leave_folder()

        raise AppResolutionError(
            "APP_LABEL_NOT_FOUND",
            f"no launcher icon matches {query!r}; visible: "
            + ", ".join(sorted({e.label for e in entries})[:20]),
        )

    def _leave_folder(self) -> None:
        back = getattr(self._client, "back", None)
        if callable(back):
            back()
            self._sleep(self._settle)
