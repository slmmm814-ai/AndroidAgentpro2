#!/usr/bin/env python3
"""خريطة التطبيقات: حصد كل التطبيقات المرئية في درج التطبيقات،
مع فتح المجلدات تلقائياً وجمع أسماء التطبيقات."""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_core.bridge_client import BridgeClient  # noqa: E402


def harvest(root) -> set[str]:
    apps: set[str] = set()

    def walk(node) -> None:
        text = (node.get("text") or "").strip()
        desc = (node.get("content_description") or "").strip()
        clickable = node.get("clickable")
        value = text or desc
        if (
            clickable
            and value
            and "مجلد" not in value
            and "إشعار" not in value
            and len(value) < 24
        ):
            apps.add(value)
        for child in node.get("children") or ():
            walk(child)

    walk(root)
    return apps


def get_center(node) -> tuple[int, int] | None:
    b = node.get("bounds") or {}
    left, top = b.get("left"), b.get("top")
    right, bottom = b.get("right"), b.get("bottom")
    if left is None or top is None or right is None or bottom is None:
        return None
    return ((left + right) // 2, (top + bottom) // 2)


def find_node(root, needle: str):
    def walk(node):
        text = (node.get("text") or "")
        desc = (node.get("content_description") or "")
        if needle in (text + " " + desc) and node.get("clickable"):
            return node
        for child in node.get("children") or ():
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(root)


def open_app_drawer(client: BridgeClient) -> None:
    client.key_event("home")
    time.sleep(1.0)
    w, h = client.screen_size()
    client.swipe(w // 2, int(h * 0.85), w // 2, int(h * 0.15), 400)
    time.sleep(1.5)


def main() -> int:
    client = BridgeClient()
    open_app_drawer(client)

    all_apps: set[str] = set()
    folders: set[str] = set()

    # صفحة 1: حصد + رصد المجلدات
    for _ in range(2):
        root = (client.command("ui_dump").data or {}).get("root") or {}
        all_apps |= harvest(root)
        w, h = client.screen_size()
        client.swipe(int(w * 0.85), h // 2, int(w * 0.10), h // 2, 400)
        time.sleep(1.3)

    # ارجع للصفحة الأولى وافتح كل مجلد
    root = (client.command("ui_dump").data or {}).get("root") or {}
    for needle in ("Samsung", "Google", "Microsoft"):
        node = find_node(root, needle)
        if node is None:
            continue
        center = get_center(node)
        if center is None:
            continue
        client.tap(*center)
        time.sleep(1.6)
        folder_root = (client.command("ui_dump").data or {}).get("root") or {}
        found = harvest(folder_root)
        all_apps |= found
        folders.add(needle)
        # أغلق المجلد بالضغط للخلف
        client.back()
        time.sleep(1.0)

    print(json.dumps(
        {
            "total_apps": len(all_apps),
            "folders_opened": sorted(folders),
            "apps": sorted(all_apps),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
