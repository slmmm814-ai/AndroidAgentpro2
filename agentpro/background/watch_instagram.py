#!/usr/bin/env python3
"""Watch the Instagram home feed for new posts from accounts you follow.

Runs in the foreground of the phone's own UI logic: Instagram stays in a
floating window while your app stays in front. On every tick it scans the
visible feed, scrolls a few times, and records any post it has not seen
before. New posts are printed and (optionally) summarized with Gemini.

Usage:
  # scan once and report
  python3 -m agentpro.background.watch_instagram --once

  # watch forever, checking every 5 minutes
  python3 -m agentpro.background.watch_instagram --interval 300

  # summarize new posts as they appear
  python3 -m agentpro.background.watch_instagram --interval 300 --summarize
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_core.bridge_client import BridgeClient  # noqa: E402
from python_core.bridge_client import BridgeClientError  # noqa: E402

from .node_driver import NodeActionDriver  # noqa: E402
from .run_feed import _collect_posts, _dedupe  # noqa: E402

PACKAGE = "com.instagram.android"
DATA_DIR = os.path.expanduser("~/.agentpro/instagram")
STATE_FILE = os.path.join(DATA_DIR, "feed_state.json")


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_seen() -> set[str]:
    _ensure_dirs()
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return set(json.load(handle).get("seen", []))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_seen(seen: set[str]) -> None:
    _ensure_dirs()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"seen": sorted(seen)}, handle, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _feed_posts(client: BridgeClient, max_scrolls: int, settle: float) -> list[dict]:
    """Read the visible feed plus a few scrolled pages."""
    driver = NodeActionDriver(client, package=PACKAGE)
    seen: set[str] = set()
    found: list[dict] = []

    found.extend(_dedupe(_collect_posts(driver, PACKAGE), seen))

    for _ in range(max_scrolls):
        before = len(seen)
        driver.physics().scroll_once("up")
        time.sleep(max(0.5, settle))
        found.extend(_dedupe(_collect_posts(driver, PACKAGE), seen))
        if len(seen) == before:
            break
    return found


def _post_key(post: dict) -> str:
    return (post.get("text") or "") + "|" + (post.get("content_description") or "")


def _is_post(label: str) -> bool:
    """Heuristic: ignore UI chrome, stories, and action buttons."""
    if not label or len(label) < 6:
        return False
    noise = (
        "الصفحة الرئيسية",
        "بحث واستكشاف",
        "ريلز",
        "موجز الصفحة الرئيسية",
        "إنشاء",
        "العناصر المحفوظة",
        "ملفك الشخصي",
        "قصة ",
        "لم تتم مشاهدتها",
        "صورة ملف",
        "الإجراءات التي يمكن اتخاذها",
        "تسجيلات الإعجاب",
        "عدد التعليقات",
        "إعادة نشر",
        "اضغط ضغطًا مزدوجًا",
        "شريط الضبط",
        "مهووس بـ",
        "استكشاف الأشخاص",
    )
    return not any(marker in label for marker in noise)


def _summarize_new(posts: list[dict], api_key: str) -> None:
    import urllib.error
    import urllib.request

    for post in posts[:3]:
        label = post.get("text") or post.get("content_description") or ""
        prompt = (
            "صف منشور إنستغرام التالي في جملة عربية واحدة مفيدة، "
            "واذكر اسم الناشر إن وُجد:\n\n" + label
        )
        body = json.dumps(
            {"contents": [{"parts": [{"text": prompt}]}]}
        ).encode()
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models"
            "/gemini-flash-latest:generateContent?key=" + api_key,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
            summary = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"    ملخص: {summary}")
        except (urllib.error.URLError, KeyError, OSError):
            pass


def scan_once(args: argparse.Namespace) -> tuple[int, list[dict]]:
    """One scan. Returns (new_count, new_posts)."""
    client = BridgeClient()
    try:
        packages = client.window_packages()
    except BridgeClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1, []

    if PACKAGE not in packages:
        print(
            f"Instagram window not visible. Open it in pop-up view first.",
            file=sys.stderr,
        )
        return 1, []

    seen = _load_seen()
    posts = _feed_posts(client, args.max_scrolls, args.settle_seconds)

    new_posts: list[dict] = []
    for post in posts:
        key = _post_key(post)
        if not key or key in seen:
            continue
        seen.add(key)
        if _is_post(post.get("text") or "") or _is_post(
            post.get("content_description") or ""
        ):
            new_posts.append(post)

    _save_seen(seen)

    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] scanned {len(posts)} items, {len(new_posts)} new")
    for post in new_posts[:10]:
        label = post.get("text") or post.get("content_description") or "(no text)"
        flat = " ".join(str(label).split())
        print(f"  + NEW: {flat[:90]}")
    return len(new_posts), new_posts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument(
        "--interval", type=int, default=300, help="seconds between scans"
    )
    parser.add_argument("--max-scrolls", type=int, default=4)
    parser.add_argument("--settle-seconds", type=float, default=2.5)
    parser.add_argument(
        "--summarize", action="store_true", help="summarize new posts with Gemini"
    )
    parser.add_argument("--kill-file", default=None)
    args = parser.parse_args(argv)

    if not os.environ.get("ANDROID_AGENT_PRO_TOKEN"):
        print("error: ANDROID_AGENT_PRO_TOKEN is not set", file=sys.stderr)
        return 2

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if args.summarize and not api_key:
        print("error: GEMINI_API_KEY is not set", file=sys.stderr)
        return 2

    if args.once:
        count, _ = scan_once(args)
        return 0 if count >= 0 else 1

    print(f"watching Instagram feed every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            if args.kill_file and os.path.exists(args.kill_file):
                break
            count, new_posts = scan_once(args)
            if args.summarize and new_posts:
                _summarize_new(new_posts, api_key)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
