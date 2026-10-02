#!/usr/bin/env python3
"""Powerful YouTube channel monitor.

Watches any number of YouTube channels, detects new videos, and can
summarize them with Gemini. Designed to run forever as a daemon.

Usage:
  # one-off scan
  python3 -m agentpro.youtube.monitor --channel @codedigiptbiplab --once

  # add channels to watch, then run forever
  python3 -m agentpro.youtube.monitor --add @codedigiptbiplab
  python3 -m agentpro.youtube.monitor --add @AIPhyOx
  python3 -m agentpro.youtube.monitor --watch --interval 300

  # list / search what we have collected
  python3 -m agentpro.youtube.monitor --list
  python3 -m agentpro.youtube.monitor --search "gemini"

  # summarize the newest videos with Gemini
  python3 -m agentpro.youtube.monitor --summarize --limit 3
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

DATA_DIR = os.path.expanduser("~/.agentpro/youtube")
CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
YOUTUBEI_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
USER_AGENT = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"


@dataclass
class Video:
    channel: str
    video_id: str
    title: str
    url: str
    published: str
    views: int = 0
    likes: int = 0
    description: str = ""
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "video_id": self.video_id,
            "title": self.title,
            "url": self.url,
            "published": self.published,
            "views": self.views,
            "likes": self.likes,
            "description": self.description,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Video":
        fields = cls.__dataclass_fields__
        kwargs = {}
        for name, spec in fields.items():
            if name in data:
                value = data[name]
                if spec.type is int or spec.type == "int":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        value = 0
                kwargs[name] = value
        return cls(**kwargs)

    @property
    def age_hours(self) -> float:
        try:
            published = datetime.fromisoformat(self.published.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - published).total_seconds() / 3600
        except (ValueError, AttributeError):
            return 0.0


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_state() -> dict:
    _ensure_dirs()
    if not os.path.exists(STATE_FILE):
        return {"videos": {}, "last_scan": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"videos": {}, "last_scan": {}}


def _save_state(state: dict) -> None:
    _ensure_dirs()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _load_channels() -> list[str]:
    _ensure_dirs()
    if not os.path.exists(CHANNELS_FILE):
        return []
    try:
        with open(CHANNELS_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []


def _save_channels(channels: list[str]) -> None:
    _ensure_dirs()
    with open(CHANNELS_FILE, "w", encoding="utf-8") as handle:
        json.dump(sorted(set(channels)), handle, ensure_ascii=False, indent=1)


def _http_get(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _resolve_channel_id(handle: str) -> str | None:
    """Resolve @handle, channel URL, or raw id to a channel id."""
    handle = handle.strip()
    if handle.startswith("UC") and len(handle) == 24:
        return handle

    clean = handle.lstrip("@")
    clean = clean.split("/")[-1] if "/" in clean else clean
    if clean.startswith("watch?v=") or "youtube.com/watch" in handle:
        return None

    # try the RSS handle form first (works for vanity handles)
    for candidate in (clean, "@" + clean):
        try:
            feed_url = (
                "https://www.youtube.com/feeds/videos.xml"
                f"?user={urllib.parse.quote(candidate)}"
            )
            xml = _http_get(feed_url)
            match = re.search(r"<channelId>(.*?)</channelId>", xml)
            if match:
                return match.group(1)
        except urllib.error.HTTPError:
            continue

    # fall back to youtubei search
    try:
        url = f"https://www.youtube.com/youtubei/v1/search?key={YOUTUBEI_KEY}"
        body = json.dumps(
            {
                "context": {
                    "client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}
                },
                "query": clean,
            }
        ).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read())
        contents = data.get("contents", {})
        primary = contents.get("twoColumnSearchResultsRenderer", {}).get(
            "primaryContents", {}
        )
        for section in primary.get("sectionListRenderer", {}).get("contents", []):
            for item in section.get("itemSectionRenderer", {}).get("contents", []):
                channel = item.get("channelRenderer", {})
                if channel.get("channelId"):
                    return channel["channelId"]
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        return None

    return None


def _fetch_feed(channel_id: str) -> str:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    return _http_get(url)


def _parse_feed(xml: str, channel_name: str = "") -> list[Video]:
    videos: list[Video] = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def find(tag: str) -> str:
            match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", entry, re.S)
            return html.unescape(match.group(1)).strip() if match else ""

        video_id = find("yt:videoId")
        if not video_id:
            continue

        title = find("title")
        published = find("published")
        channel = channel_name or find("name")

        views = 0
        likes = 0
        stats = re.search(r"<media:statistics[^>]*views=\"(\d+)\"", entry)
        if stats:
            views = int(stats.group(1))
        stars = re.search(r'<media:starRating[^>]*count="(\d+)"', entry)
        if stars:
            likes = int(stars.group(1))

        videos.append(
            Video(
                channel=channel,
                video_id=video_id,
                title=title,
                url=f"https://youtube.com/watch?v={video_id}",
                published=published,
                views=views,
                likes=likes,
            )
        )
    return videos


def fetch_description(video_id: str) -> str:
    """Fetch the full description of a video."""
    try:
        page = _http_get(f"https://www.youtube.com/watch?v={video_id}")
        match = re.search(r'"shortDescription":"(.*?)","', page, re.S)
        if match:
            return html.unescape(match.group(1)).replace("\\n", "\n")
    except urllib.error.HTTPError:
        return ""
    return ""


def scan_channel(channel: str, fetch_descriptions: bool = False) -> list[Video]:
    """Scan one channel and return its recent videos."""
    channel_id = _resolve_channel_id(channel)
    if not channel_id:
        print(f"  ! could not resolve {channel}", file=sys.stderr)
        return []

    try:
        xml = _fetch_feed(channel_id)
    except urllib.error.URLError as error:
        print(f"  ! feed failed for {channel}: {error}", file=sys.stderr)
        return []

    videos = _parse_feed(xml)
    if fetch_descriptions:
        for video in videos[:5]:
            if not video.description:
                video.description = fetch_description(video.video_id)
    return videos


def gemini_summarize(text: str, api_key: str, model: str = "gemini-flash-latest") -> str:
    """Summarize text with Gemini, falling back to other models on outage."""
    models = [model, "gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-3.5-flash"]
    prompt = (
        "لخص هذا الفيديو في 3 جمل عربية واضحة، واذكر أهم أداة أو نموذج ذُكر:\n\n" + text
    )
    body = json.dumps(
        {"contents": [{"parts": [{"text": prompt}]}]}
    ).encode()

    last_error = ""
    for candidate in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"/{candidate}:generateContent?key={api_key}"
        )
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as error:
            last_error = f"{candidate}: HTTP {error.code}"
            continue
        except (urllib.error.URLError, KeyError, OSError) as error:
            last_error = f"{candidate}: {error}"
            continue
    raise RuntimeError(f"all models failed ({last_error})")


def cmd_add(args: argparse.Namespace) -> int:
    channels = _load_channels()
    for channel in args.add:
        channels.append(channel)
    _save_channels(channels)
    print(f"watching {len(set(channels))} channels:")
    for channel in sorted(set(channels)):
        print(f"  - {channel}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    state = _load_state()
    videos = sorted(
        (Video.from_dict(v) for v in state["videos"].values()),
        key=lambda v: v.published,
        reverse=True,
    )
    if args.limit:
        videos = videos[: args.limit]
    if not videos:
        print("no videos yet — run a scan first")
        return 0
    for video in videos:
        age = video.age_hours
        when = (
            f"{age:.0f}h ago" if age < 48 else f"{age / 24:.1f}d ago"
        )
        print(f"[{when:>7}] {video.channel}: {video.title[:65]}")
        print(f"           {video.views} views  {video.url}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    state = _load_state()
    query = args.search.lower()
    hits = [
        Video.from_dict(v)
        for v in state["videos"].values()
        if query in (v.get("title") or "").lower()
        or query in (v.get("description") or "").lower()
    ]
    hits.sort(key=lambda v: v.published, reverse=True)
    print(f"{len(hits)} matches for {args.search!r}")
    for video in hits[: args.limit or 20]:
        print(f"  - {video.title[:70]}")
        print(f"      {video.channel}  {video.url}")
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not set", file=sys.stderr)
        return 1
    state = _load_state()
    videos = sorted(
        (Video.from_dict(v) for v in state["videos"].values()),
        key=lambda v: v.published,
        reverse=True,
    )[: args.limit or 3]
    for video in videos:
        if not video.description:
            video.description = fetch_description(video.video_id)
        source = video.description or video.title
        try:
            video.summary = gemini_summarize(source, api_key)
            state["videos"][video.video_id] = video.to_dict()
        except (urllib.error.URLError, KeyError, OSError) as error:
            print(f"  ! summarize failed: {error}", file=sys.stderr)
        print(f"\n=== {video.title[:70]} ===")
        print(video.summary or "(no summary)")
    _save_state(state)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    channels = [args.channel] if args.channel else _load_channels()
    if not channels:
        print("no channels to scan — add one with --add", file=sys.stderr)
        return 1

    state = _load_state()
    new_count = 0

    for channel in channels:
        label = channel.lstrip("@")
        videos = scan_channel(channel, fetch_descriptions=args.descriptions)
        for video in videos:
            existing = state["videos"].get(video.video_id)
            if existing:
                continue
            state["videos"][video.video_id] = video.to_dict()
            new_count += 1
            age = video.age_hours
            when = f"{age:.0f}h" if age < 48 else f"{age / 24:.1f}d"
            print(f"  + NEW [{when}] {label}: {video.title[:60]}")
        state["last_scan"][channel] = datetime.now(timezone.utc).isoformat()
        print(f"  scanned {label}: {len(videos)} videos")

    _save_state(state)
    print(f"\n{new_count} new video(s) across {len(channels)} channel(s)")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    channels = _load_channels()
    if not channels:
        print("no channels to watch — add one with --add", file=sys.stderr)
        return 1

    print(f"watching {len(channels)} channels every {args.interval}s")
    print("press Ctrl+C to stop")
    try:
        while True:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] scanning...")
            cmd_scan(args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export collected videos to a markdown or json file."""
    state = _load_state()
    videos = sorted(
        (Video.from_dict(v) for v in state["videos"].values()),
        key=lambda v: v.published,
        reverse=True,
    )
    if not videos:
        print("no videos to export")
        return 0

    out_path = args.export
    if out_path.endswith(".json"):
        payload = [video.to_dict() for video in videos]
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
    else:
        lines = ["# YouTube feed digest\n"]
        current_channel = ""
        for video in videos:
            if video.channel != current_channel:
                current_channel = video.channel
                lines.append(f"\n## {current_channel}\n")
            age = video.age_hours
            when = f"{age:.0f}h ago" if age < 48 else f"{age / 24:.1f}d ago"
            lines.append(f"- **{video.title}** ({when}, {video.views} views)")
            lines.append(f"  {video.url}")
            if video.summary:
                flat = " ".join(video.summary.split())
                lines.append(f"  > {flat}")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    print(f"exported {len(videos)} videos to {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add", nargs="+", metavar="CHANNEL", help="channels to watch")
    parser.add_argument("--channel", metavar="CHANNEL", help="scan a single channel")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument("--watch", action="store_true", help="run forever")
    parser.add_argument("--interval", type=int, default=300, help="seconds between scans")
    parser.add_argument("--list", action="store_true", help="list collected videos")
    parser.add_argument("--search", metavar="QUERY", help="search collected videos")
    parser.add_argument("--summarize", action="store_true", help="summarize with Gemini")
    parser.add_argument("--limit", type=int, help="limit results")
    parser.add_argument("--export", metavar="PATH", help="export to a .md or .json file")
    parser.add_argument(
        "--descriptions", action="store_true", help="fetch full descriptions"
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.add:
        return cmd_add(args)
    if args.list:
        return cmd_list(args)
    if args.search:
        return cmd_search(args)
    if args.summarize:
        return cmd_summarize(args)
    if args.export:
        return cmd_export(args)
    if args.watch:
        return cmd_watch(args)
    if args.channel or args.once:
        return cmd_scan(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
