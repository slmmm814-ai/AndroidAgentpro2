#!/usr/bin/env python3
"""طبقة المهام: أهداف عالية المستوى تُنفَّذ على الهاتف.

أمثلة:
  python3 python_core/mission.py search "الجزائر"
  python3 python_core/mission.py open YouTube
  python3 python_core/mission.py browse "wikipedia.org"
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_core.app_control import launch  # noqa: E402
from python_core.bridge_client import BridgeClient  # noqa: E402


def center_of(node):
    b = node.get("bounds") or {}
    left, top = int(b.get("left") or 0), int(b.get("top") or 0)
    right, bottom = int(b.get("right") or 0), int(b.get("bottom") or 0)
    if right > left and bottom > top:
        return ((left + right) // 2, (top + bottom) // 2)
    return None


def find_first(root, predicate):
    def walk(node):
        if predicate(node):
            return node
        for child in node.get("children") or ():
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(root)


def dump_root(client: BridgeClient):
    return (client.command("ui_dump").data or {}).get("root") or {}


def mission_search(client: BridgeClient, query: str) -> dict:
    """افتح Chrome وابحث عن استعلام."""
    launch(client, "Chrome")
    time.sleep(1.5)

    # انقر على شريط بحث Google أو شريط العنوان
    root = dump_root(client)

    def is_search_bar(node):
        desc = (node.get("content_description") or "")
        text = (node.get("text") or "")
        joined = desc + " " + text
        if not node.get("clickable"):
            return False
        if "بحث" in joined or "search" in joined.lower():
            return True
        # شريط العنوان في Chrome قد يكون حقل EditText يحوي عنواناً
        cls = (node.get("class_name") or "")
        if "EditText" in cls and text:
            return True
        return False

    node = find_first(root, is_search_bar)
    if not node:
        # تراجع: أي حقل نصي قابل للنقر
        node = find_first(
            root,
            lambda n: n.get("clickable")
            and "EditText" in (n.get("class_name") or ""),
        )
    if not node:
        return {"ok": False, "error": "search bar not found"}

    pos = center_of(node)
    if not pos:
        return {"ok": False, "error": "search bar has no bounds"}

    client.tap(*pos)
    time.sleep(1.2)
    # امسح أي نص سابق ثم اكتب الاستعلام الجديد
    try:
        client.clear_text()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.4)
    client.input_text(query)
    time.sleep(1.6)

    # انقر على أول اقتراح
    root = dump_root(client)
    suggestion = find_first(
        root,
        lambda n: (n.get("text") or "").strip() == query and n.get("clickable"),
    )
    if suggestion:
        sug_pos = center_of(suggestion)
        if sug_pos:
            client.tap(*sug_pos)
            time.sleep(2.5)

    # اجمع أول عناوين النتائج
    root = dump_root(client)
    results: list[str] = []
    for node in _walk(root):
        text = (node.get("text") or "").strip()
        if text and len(text) > 12 and text != query:
            results.append(text)
            if len(results) >= 8:
                break

    return {"ok": True, "query": query, "results": results}


def _walk(root):
    yield root
    for child in root.get("children") or ():
        yield from _walk(child)


def mission_browse(client: BridgeClient, url: str) -> dict:
    """افتح موقعاً مباشرة."""
    launch(client, "Chrome")
    time.sleep(1.5)
    if not url.startswith("http"):
        url = "https://" + url
    try:
        client.open_url(url)
        time.sleep(3.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    root = dump_root(client)
    texts = [
        (n.get("text") or "").strip()
        for n in _walk(root)
        if (n.get("text") or "").strip()
    ]
    return {"ok": True, "url": url, "page_title": texts[0] if texts else None}


def main() -> int:
    if len(sys.argv) < 3:
        print('usage: mission.py <search|browse> "<query or url>"')
        return 2

    action = sys.argv[1]
    target = sys.argv[2]
    client = BridgeClient()

    if action == "search":
        result = mission_search(client, target)
    elif action == "browse":
        result = mission_browse(client, target)
    else:
        print(f"unknown mission: {action}")
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
