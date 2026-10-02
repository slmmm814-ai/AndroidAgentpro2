#!/usr/bin/env python3
"""Watch Instagram accounts through an RSS-Bridge feed — no phone needed.

This is the true background path: it asks a public RSS-Bridge instance to
turn any public Instagram profile into an Atom/MRSS feed, so new posts are
detected without opening the app at all.

Usage:
  python3 -m agentpro.youtube.instagram_rss --user claudeai --once
  python3 -m agentpro.youtube.instagram_rss --add claudeai --add google
  python3 -m agentpro.youtube.instagram_rss --watch --interval 600
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DATA_DIR = os.path.expanduser("~/.agentpro/instagram")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
STATE_FILE = os.path.join(DATA_DIR, "rss_state.json")
BRIDGE_URL = "https://rss-bridge.org/bridge01"
USER_AGENT = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_accounts() -> list[str]:
    _ensure_dirs()
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []


def _save_accounts(accounts: list[str]) -> None:
    _ensure_dirs()
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as handle:
        json.dump(sorted(set(accounts)), handle, ensure_ascii=False, indent=1)


def _load_state() -> dict:
    _ensure_dirs()
    if not os.path.exists(STATE_FILE):
        return {"posts": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"posts": {}}


def _save_state(state: dict) -> None:
    _ensure_dirs()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _fetch_feed(user: str) -> str:
    query = urllib.parse.urlencode(
        {
            "action": "display",
            "bridge": "Instagram",
            "u": user,
            "format": "Mrss",
        }
    )
    url = BRIDGE_URL + "?" + query
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="ignore")


def _parse_feed(xml: str, user: str) -> list[dict]:
    posts: list[dict] = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def field(tag: str) -> str:
            match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.S)
            return html.unescape(match.group(1)).strip() if match else ""

        link = field("link")
        post_id = link.rstrip("/").split("/")[-1] if link else ""

        # strip html from description for a clean caption preview
        raw_desc = field("description")
        caption = re.sub(r"<[^>]+>", " ", raw_desc)
        caption = " ".join(caption.split())

        media = re.search(r'<media:content url="([^"]+)"', item)
        media_url = media.group(1) if media else ""

        posts.append(
            {
                "user": user,
                "post_id": post_id,
                "title": field("title"),
                "caption": caption,
                "link": link,
                "published": field("pubDate"),
                "media_url": media_url,
            }
        )
    return posts


def scan_user(user: str) -> list[dict]:
    try:
        xml = _fetch_feed(user)
    except urllib.error.HTTPError as error:
        print(f"  ! {user}: HTTP {error.code}", file=sys.stderr)
        return []
    except urllib.error.URLError as error:
        print(f"  ! {user}: {error}", file=sys.stderr)
        return []
    return _parse_feed(xml, user)


def cmd_add(args: argparse.Namespace) -> int:
    accounts = _load_accounts()
    accounts.extend(args.add)
    _save_accounts(accounts)
    unique = sorted(set(accounts))
    print(f"watching {len(unique)} Instagram accounts:")
    for account in unique:
        print(f"  - @{account}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    users = [args.user] if args.user else _load_accounts()
    if not users:
        print("no accounts — add one with --add", file=sys.stderr)
        return 1

    state = _load_state()
    new_total = 0

    for user in users:
        posts = scan_user(user)
        for post in posts:
            key = post["post_id"]
            if not key or key in state["posts"]:
                continue
            state["posts"][key] = post
            new_total += 1
            print(f"  + NEW @{user}: {post['title'][:60] or post['caption'][:60]}")
            print(f"      {post['link']}")

    _save_state(state)
    print(f"\n{new_total} new post(s) across {len(users)} account(s)")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    state = _load_state()
    posts = sorted(state["posts"].values(), key=lambda p: p.get("published", ""))
    if args.limit:
        posts = posts[-args.limit :]
    if not posts:
        print("no posts collected yet")
        return 0
    for post in reversed(posts):
        label = post["title"] or post["caption"] or "(no caption)"
        print(f"[@{post['user']}] {label[:70]}")
        print(f"   {post['published'][:25]}  {post['link']}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    users = _load_accounts()
    if not users:
        print("no accounts — add one with --add", file=sys.stderr)
        return 1
    print(f"watching {len(users)} accounts every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] scanning...")
            cmd_scan(args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add", nargs="+", metavar="USER", help="accounts to watch")
    parser.add_argument("--user", metavar="USER", help="scan a single account")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument("--watch", action="store_true", help="run forever")
    parser.add_argument("--interval", type=int, default=600)
    parser.add_argument("--list", action="store_true", help="list collected posts")
    parser.add_argument("--limit", type=int, help="limit results")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.add:
        return cmd_add(args)
    if args.list:
        return cmd_list(args)
    if args.watch:
        return cmd_watch(args)
    if args.user or args.once:
        return cmd_scan(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
