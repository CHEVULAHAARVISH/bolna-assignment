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
import html
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field

import aiohttp
import feedparser


# ── Configuration ────────────────────────────────────────────────────────────

STATUS_FEEDS: list[dict[str, str]] = [
    {
        "name": "OpenAI",
        "url": "https://status.openai.com/feed.atom",
    },
    # Add more feeds to monitor concurrently:
    # {"name": "GitHub", "url": "https://www.githubstatus.com/history.atom"},
]

POLL_INTERVAL_SECONDS = 30


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class FeedState:
    """Per-feed HTTP cache headers and seen incidents."""
    etag: str | None = None
    last_modified: str | None = None
    seen_incidents: dict[str, str] = field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    clean = re.sub(r"<br\s*/?>", "\n", text)
    clean = re.sub(r"<li>", "  - ", clean)
    clean = re.sub(r"<[^>]+>", "", clean)
    return html.unescape(clean).strip()


def parse_components(summary_html: str) -> list[str]:
    items = re.findall(r"<li>\s*(.+?)\s*</li>", summary_html, re.DOTALL)
    return [strip_html(c) for c in items]


def parse_status(summary_html: str) -> str:
    # Extract the detailed status message (everything between Status: and Affected components)
    match = re.search(r"<b>Status:\s*(.+?)</b>(.*?)(?:<b>Affected|$)", summary_html, re.DOTALL)
    if match:
        status = strip_html(match.group(1))
        detail = strip_html(match.group(2))
        if detail:
            return f"{status} — {detail}"
        return status
    return strip_html(summary_html)


def format_ts(ts_str: str) -> str:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return ts_str


def log_incident(provider: str, entry: feedparser.FeedParserDict) -> None:
    title = entry.get("title", "Unknown Incident")
    updated = entry.get("updated", entry.get("published", ""))
    summary_html = entry.get("summary", "")

    status = parse_status(summary_html)
    components = parse_components(summary_html)
    ts = format_ts(updated)

    product = f"{provider} API"
    if components:
        product += f" - {', '.join(components)}"

    print(f"[{ts}] Product: {product}")
    print(f"Status: {status}")
    print()


# ── Core ─────────────────────────────────────────────────────────────────────

async def fetch_feed(
    session: aiohttp.ClientSession, url: str, state: FeedState
) -> feedparser.FeedParserDict | None:
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


async def watch_feed(config: dict[str, str]) -> None:
    name, url = config["name"], config["url"]
    state = FeedState()
    first_run = True

    print(f"[*] Watching {name}: {url}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                parsed = await fetch_feed(session, url, state)
                if parsed is not None:
                    if first_run:
                        for e in parsed.entries:
                            eid = e.get("id", e.get("link", ""))
                            state.seen_incidents[eid] = e.get("updated", e.get("published", ""))
                        print(f"[*] {name}: {len(parsed.entries)} existing incidents loaded. Watching for new updates...\n")
                        first_run = False
                    else:
                        for e in parsed.entries:
                            eid = e.get("id", e.get("link", ""))
                            updated = e.get("updated", e.get("published", ""))
                            prev = state.seen_incidents.get(eid)
                            if prev is None or updated != prev:
                                state.seen_incidents[eid] = updated
                                log_incident(name, e)
            except aiohttp.ClientError as err:
                print(f"[error] {name}: {err}")
            except Exception as err:
                print(f"[error] {name}: {err}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def main() -> None:
    print("OpenAI Status Tracker (Simple — Conditional HTTP Polling)")
    print(f"Checking {len(STATUS_FEEDS)} feed(s) every {POLL_INTERVAL_SECONDS}s\n")

    tasks = [asyncio.create_task(watch_feed(cfg)) for cfg in STATUS_FEEDS]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Stopped.")
