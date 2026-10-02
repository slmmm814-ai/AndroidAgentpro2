#!/usr/bin/env python3
"""طبقة التحكم عالية المستوى: فتح أي تطبيق بالاسم من درج التطبيقات.

تتعامل مع: فتح الدرج، البحث (إن لزم)، النقر على الأيقونة،
والتحقق من أن التطبيق أصبح في المقدمة."""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_core.bridge_client import BridgeClient  # noqa: E402


# أسماء العرض -> اسم حزمة أندرويد (للتحقق)
PACKAGE_OF = {
    "Chrome": "com.android.chrome",
    "Browser": "com.sec.android.app.sbrowser",
    "YouTube": "com.google.android.youtube",
    "Instagram": "com.instagram.android",
    "الرسائل": "com.samsung.android.messaging",
    "صور": "com.sec.android.gallery3d",
    "خرائط": "com.google.android.apps.maps",
    "متجر Play": "com.android.vending",
    "Claude": "com.anthropic.claude",
    "Spotify": "com.spotify.music",
    "YT Music": "com.google.android.apps.youtube.music",
    "Drive": "com.google.android.apps.docs",
    "Gmail": "com.google.android.gm",
    "Outlook": "com.microsoft.office.outlook",
    "OneDrive": "com.microsoft.skydrive",
    "LinkedIn": "com.linkedin.android",
    "Meet": "com.google.android.meetings",
    "Wallet": "com.samsung.android.samsungpay",
    "SmartThings": "com.samsung.android.oneconnect",
    "متصفح": "com.sec.android.app.sbrowser",
}


def current_package(client: BridgeClient) -> str:
    data = client.command("get_window").data or {}
    return (
        data.get("package")
        or data.get("package_name")
        or ""
    )


def find_icon(root, name: str):
    """ابحث عن أيقونة تطبيق بالاسم (نص أو وصف)."""
    def walk(node):
        text = (node.get("text") or "").strip()
        desc = (node.get("content_description") or "").strip()
        value = text or desc
        if node.get("clickable") and value:
            if value == name or (name in value and "مجلد" not in value):
                b = node.get("bounds") or {}
                left, top = int(b.get("left") or 0), int(b.get("top") or 0)
                right, bottom = int(b.get("right") or 0), int(b.get("bottom") or 0)
                if right > left and bottom > top:
                    return ((left + right) // 2, (top + bottom) // 2)
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


def launch(client: BridgeClient, name: str, max_pages: int = 3) -> dict:
    """افتح تطبيقاً بالاسم. يعيد dict بالنتيجة."""
    result = {"app": name, "opened": False, "package": None}

    open_app_drawer(client)

    # جرّب launch_app مباشرة بالحزمة إن عرفناها
    pkg = PACKAGE_OF.get(name)
    if pkg:
        try:
            client.launch_app(pkg)
            time.sleep(1.8)
            now = current_package(client)
            if now == pkg:
                result.update(opened=True, package=now)
                return result
        except Exception:
            pass

    # ابحث في صفحات الدرج
    w, h = client.screen_size()
    for page in range(max_pages):
        root = (client.command("ui_dump").data or {}).get("root") or {}
        pos = find_icon(root, name)
        if pos:
            client.tap(*pos)
            time.sleep(2.0)
            now = current_package(client)
            result.update(opened=True, package=now)
            return result
        # انتقل للصفحة التالية
        client.swipe(int(w * 0.85), h // 2, int(w * 0.10), h // 2, 400)
        time.sleep(1.3)

    result["error"] = "app icon not found in drawer"
    return result


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: app_control.py <app-name>")
        return 2
    client = BridgeClient()
    result = launch(client, sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["opened"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
