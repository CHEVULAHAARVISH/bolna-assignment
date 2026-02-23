"""
OpenAI Status Tracker — Simple (Efficient Feed Polling)
========================================================
Tracks incidents from the OpenAI Status Page using Atom feed
with conditional HTTP requests (ETag + Last-Modified).

The server returns 304 Not Modified when nothing has changed,
so data is only downloaded on actual updates — bandwidth efficient.

Scales to 100+ feeds via asyncio concurrent tasks.

Usage:
    pip install aiohttp feedparser
    python simple_tracker.py
"""

import asyncio
from dataclasses import dataclass, field

import aiohttp
import feedparser

from status_tracker import FEED_TOPICS, POLL_INTERVAL
from status_tracker.tracker import IncidentTracker, print_incident


@dataclass
class FeedState:
    """Per-feed HTTP cache headers for conditional requests."""
    etag: str | None = None
    last_modified: str | None = None


async def fetch_feed(
    session: aiohttp.ClientSession, url: str, state: FeedState
) -> feedparser.FeedParserDict | None:
    """Fetch feed with conditional HTTP; returns None on 304 Not Modified."""
    headers: dict[str, str] = {}
    if state.etag:
        headers["If-None-Match"] = state.etag
    if state.last_modified:
        headers["If-Modified-Since"] = state.last_modified

    async with session.get(url, headers=headers) as resp:
        if resp.status == 304:
            return None
        if resp.status != 200:
            print(f"[warn] {url} returned HTTP {resp.status}")
            return None
        state.etag = resp.headers.get("ETag")
        state.last_modified = resp.headers.get("Last-Modified")
        return feedparser.parse(await resp.text())


async def watch_feed(name: str, url: str) -> None:
    """Poll a single feed forever, printing new/updated incidents."""
    state = FeedState()
    tracker = IncidentTracker(on_incident=print_incident)

    print(f"[*] Watching {name}: {url}")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                parsed = await fetch_feed(session, url, state)
                if parsed is not None:
                    tracker.process_feed(name, parsed)
            except aiohttp.ClientError as err:
                print(f"[error] {name}: {err}")
            except Exception as err:
                print(f"[error] {name}: {err}")

            await asyncio.sleep(POLL_INTERVAL)


async def main() -> None:
    print("OpenAI Status Tracker (Simple — Conditional HTTP Polling)")
    print(f"Checking {len(FEED_TOPICS)} feed(s) every {POLL_INTERVAL}s\n")

    tasks = [
        asyncio.create_task(watch_feed(cfg["name"], cfg["url"]))
        for cfg in FEED_TOPICS
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Stopped.")
