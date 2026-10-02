"""Run a read-only task on a background window without leaving the foreground.

Usage::

    python3 -m agentpro.background.run_feed "com.instagram.android" \\
        --max-scrolls 6

This drives the target package entirely through node actions: the feed
container is located by class (RecyclerView) and scrolled with
``ACTION_SCROLL_FORWARD``, and the visible captions/usernames are read from the
package's own UI tree. The foreground app is never touched, so the phone stays
where the user left it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_core.bridge_client import BridgeClient  # noqa: E402
from python_core.bridge_client import BridgeClientError  # noqa: E402

from .node_driver import NodeActionDriver  # noqa: E402


def _collect_posts(driver: NodeActionDriver, package: str) -> list[dict]:
    """Extract visible post summaries from the package's UI tree."""
    response = driver._client.ui_dump_for_package(package)
    data = response.data or {}
    root = data.get("root")

    if not isinstance(root, dict):
        return []

    posts: list[dict] = []

    def walk(node: dict) -> None:
        text = (node.get("text") or "").strip()
        desc = (node.get("content_description") or "").strip()

        # Instagram exposes the primary caption and the username as text nodes;
        # the a11y content description carries the spoken summary.
        label = text or desc
        if isinstance(label, str) and label:
            posts.append(
                {
                    "text": text,
                    "content_description": desc,
                    "resource_id": node.get("view_id_resource_name") or "",
                    "class_name": node.get("class_name") or "",
                }
            )

        for child in node.get("children") or ():
            if isinstance(child, dict):
                walk(child)

    walk(root)
    return posts


def _dedupe(posts: list[dict], seen: set[str]) -> list[dict]:
    unique: list[dict] = []

    for post in posts:
        key = (post.get("text") or "") + "|" + (post.get("content_description") or "")
        if not key or key in seen:
            continue

        seen.add(key)
        unique.append(post)

    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentpro.background.run_feed",
        description="Scroll a background app feed via node actions.",
    )
    parser.add_argument("package", help="target package, e.g. com.instagram.android")
    parser.add_argument("--max-scrolls", type=int, default=6)
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument("--kill-file", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not os.environ.get("ANDROID_AGENT_PRO_TOKEN"):
        print("error: ANDROID_AGENT_PRO_TOKEN is not set", file=sys.stderr)
        return 2

    package = args.package.strip()
    client = BridgeClient()

    started = time.monotonic()

    # The window must already be visible (launched but not necessarily
    # foreground). Confirm before driving it, otherwise node actions have
    # nothing to target.
    try:
        packages = client.window_packages()
    except BridgeClientError as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1

    if package not in packages:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": f"no visible window for {package}",
                    "visible_packages": packages,
                }
            )
        )
        return 1

    driver = NodeActionDriver(client, package=package)

    seen: set[str] = set()
    all_posts: list[dict] = []
    scrolls = 0

    # Reading never requires scrolling: the tree already holds everything
    # visible. Collect first, scroll only to see what is further down.
    all_posts.extend(_dedupe(_collect_posts(driver, package), seen))

    while scrolls < args.max_scrolls:
        if args.kill_file and os.path.exists(args.kill_file):
            break

        before_count = len(seen)
        driver.physics().scroll_once("up")
        scrolls += 1
        time.sleep(max(0.5, args.settle_seconds))

        all_posts.extend(_dedupe(_collect_posts(driver, package), seen))

        # Progress is "new unique labels appeared", not a fingerprint change:
        # a scroll that lands on an already-seen screen is a real dead end.
        if len(seen) == before_count:
            break

    elapsed = time.monotonic() - started

    report = {
        "package": package,
        "success": len(all_posts) > 0,
        "scrolls": scrolls,
        "posts_seen": len(all_posts),
        "duration_seconds": round(elapsed, 2),
        "posts": all_posts[:60],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"posts_seen={report['posts_seen']} scrolls={scrolls}")
        for post in all_posts[:20]:
            label = post.get("text") or post.get("content_description")
            print(f"  - {label}")

    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
