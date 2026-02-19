"""
OpenAI Status Tracker — Event-Based (WebSub / Webhook)
=======================================================
Truly event-driven: our code never polls. A WebSub hub monitors the feed
and POSTs new content to our webhook callback when incidents change.

Architecture:
  ┌──────────────┐  subscribe  ┌──────────────┐  polls   ┌──────────────┐
  │ Our Webhook  │ ──────────→ │  WebSub Hub  │ ───────→ │  Atom Feed   │
  │   Server     │             │              │          │ (OpenAI)     │
  │              │ ←────────── │              │ ←─────── │              │
  │  /callback   │  POST push  │              │  200/304 │              │
  └──────────────┘             └──────────────┘          └──────────────┘
       │
       ▼
    Console output

Scales to 100+ feeds: register one subscription per feed with the hub.
Our server just listens — O(1) work per incoming event regardless of feed count.

Usage:
    pip install aiohttp feedparser

    # With an external WebSub hub (e.g. Superfeedr):
    python webhook_tracker.py --callback-url https://your-server.com/callback

    # For local testing (built-in hub simulator):
    python webhook_tracker.py --simulate-hub
"""

import argparse
import asyncio
import hashlib
import hmac
import html
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web
import feedparser


# ── Configuration ────────────────────────────────────────────────────────────

FEED_TOPICS: list[dict[str, str]] = [
    {
        "name": "OpenAI",
        "topic": "https://status.openai.com/feed.atom",
    },
    # Add more feeds — each gets a separate hub subscription:
    # {"name": "GitHub", "topic": "https://www.githubstatus.com/history.atom"},
]

DEFAULT_HUB = "https://push.superfeedr.com/"
WEBHOOK_PORT = 8080
HUB_SIMULATE_INTERVAL = 30  # seconds, only used with --simulate-hub


# ── SSE Event Bus ────────────────────────────────────────────────────────────

class SSEBus:
    """Pub/sub bus for pushing incident events to SSE clients."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._recent: list[dict[str, Any]] = []  # last 50 events for new clients

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, event: dict[str, Any]) -> None:
        self._recent.append(event)
        self._recent = self._recent[-50:]
        for q in self._subscribers:
            await q.put(event)

    @property
    def recent(self) -> list[dict[str, Any]]:
        return list(self._recent)


sse_bus = SSEBus()


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


# ── Incident State ───────────────────────────────────────────────────────────

class IncidentTracker:
    """Tracks seen incidents to only print new/updated ones."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}  # incident_id -> last updated timestamp
        self._initialized = False

    def process_feed(self, provider: str, feed_body: str) -> None:
        parsed = feedparser.parse(feed_body)
        if not parsed.entries:
            return

        if not self._initialized:
            # First time: silently record all existing incidents
            for e in parsed.entries:
                eid = e.get("id", e.get("link", ""))
                self._seen[eid] = e.get("updated", e.get("published", ""))
            print(f"[*] {provider}: baseline loaded ({len(parsed.entries)} incidents). Watching for new updates...\n")
            self._initialized = True
            return

        for e in parsed.entries:
            eid = e.get("id", e.get("link", ""))
            updated = e.get("updated", e.get("published", ""))
            prev = self._seen.get(eid)

            if prev is None or updated != prev:
                self._seen[eid] = updated
                self._log(provider, e)

    def _log(self, provider: str, entry: feedparser.FeedParserDict) -> None:
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

        # Push to SSE clients
        event = {
            "timestamp": ts,
            "provider": provider,
            "incident": title,
            "product": product,
            "status": status,
            "affected": components,
        }
        asyncio.create_task(sse_bus.publish(event))


# Per-topic trackers
trackers: dict[str, IncidentTracker] = {}


def get_tracker(topic_url: str) -> tuple[IncidentTracker, str]:
    """Get or create a tracker for a topic URL. Returns (tracker, provider_name)."""
    for feed in FEED_TOPICS:
        if feed["topic"] == topic_url:
            name = feed["name"]
            if topic_url not in trackers:
                trackers[topic_url] = IncidentTracker()
            return trackers[topic_url], name
    # Unknown topic
    if topic_url not in trackers:
        trackers[topic_url] = IncidentTracker()
    return trackers[topic_url], topic_url


# ── WebSub Webhook Handler ──────────────────────────────────────────────────

# Secret per subscription for HMAC verification
subscription_secrets: dict[str, str] = {}


async def callback_get(request: web.Request) -> web.Response:
    """
    WebSub verification callback (RFC 7574 §5.3).
    The hub sends a GET with hub.challenge to verify our subscription.
    We echo back the challenge to confirm.
    """
    mode = request.query.get("hub.mode", "")
    topic = request.query.get("hub.topic", "")
    challenge = request.query.get("hub.challenge", "")

    if mode in ("subscribe", "unsubscribe") and challenge:
        print(f"[*] WebSub: verified {mode} for {topic}")
        return web.Response(text=challenge, content_type="text/plain")

    return web.Response(status=404)


async def callback_post(request: web.Request) -> web.Response:
    """
    WebSub content distribution callback (RFC 7574 §7).
    The hub POSTs new feed content when incidents change.
    This is the event — our code reacts, never polls.
    """
    body = await request.read()

    # Determine which topic this is for (from Link header or stored mapping)
    topic = None
    link_header = request.headers.get("Link", "")
    for feed in FEED_TOPICS:
        if feed["topic"] in link_header or feed["topic"] in body.decode("utf-8", errors="ignore"):
            topic = feed["topic"]
            break

    if topic is None and FEED_TOPICS:
        topic = FEED_TOPICS[0]["topic"]

    # Optional: verify HMAC signature
    sig_header = request.headers.get("X-Hub-Signature", "")
    secret = subscription_secrets.get(topic, "")
    if sig_header and secret:
        expected = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            print("[warn] HMAC signature mismatch — ignoring payload")
            return web.Response(status=403)

    # Process the feed content — this is the event handler
    tracker, name = get_tracker(topic)
    tracker.process_feed(name, body.decode("utf-8", errors="ignore"))

    return web.Response(status=200, text="OK")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def sse_handler(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events endpoint — browsers subscribe here for live updates."""
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)

    await resp.write(b": connected\n\n")

    # Send recent events so new clients see history
    for event in sse_bus.recent:
        data = json.dumps(event)
        await resp.write(f"event: incident\ndata: {data}\n\n".encode())

    q = sse_bus.subscribe()
    try:
        while True:
            event = await q.get()
            data = json.dumps(event)
            await resp.write(f"event: incident\ndata: {data}\n\n".encode())
    except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
        pass
    finally:
        sse_bus.unsubscribe(q)
    return resp


async def index_handler(request: web.Request) -> web.Response:
    """Index page: assignment overview + architecture + live incident feed."""
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenAI Status Tracker</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; background: #0d1117; color: #c9d1d9; line-height: 1.6; padding: 2rem; max-width: 900px; margin: 0 auto; }
  h1 { color: #58a6ff; margin-bottom: 0.5rem; font-size: 1.5rem; }
  h2 { color: #58a6ff; margin: 2rem 0 0.75rem; font-size: 1.15rem; border-bottom: 1px solid #21262d; padding-bottom: 0.4rem; }
  p, li { color: #8b949e; font-size: 0.9rem; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .badge { display: inline-block; background: #238636; color: #fff; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; margin-left: 0.5rem; }
  pre { background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 1rem; overflow-x: auto; font-size: 0.82rem; color: #c9d1d9; margin: 0.75rem 0; }
  .arch { white-space: pre; font-size: 0.78rem; line-height: 1.4; }
  ul { padding-left: 1.5rem; margin: 0.5rem 0; }
  .feed { margin-top: 1rem; }
  .event { background: #161b22; border-left: 3px solid #58a6ff; padding: 0.75rem 1rem; margin: 0.5rem 0; border-radius: 4px; }
  .event .ts { color: #484f58; font-size: 0.78rem; }
  .event .title { color: #c9d1d9; font-weight: bold; }
  .event .status { color: #d29922; font-size: 0.85rem; }
  .event .affected { color: #8b949e; font-size: 0.8rem; }
  #waiting { color: #484f58; font-style: italic; }
  .dot { display: inline-block; width: 8px; height: 8px; background: #238636; border-radius: 50%; margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
</style>
</head>
<body>

<h1>OpenAI Status Tracker <span class="badge">LIVE</span></h1>
<p>Event-driven service tracking incidents from the <a href="https://status.openai.com/" target="_blank">OpenAI Status Page</a>.</p>

<h2>Assignment</h2>
<p>Build a Python script or lightweight app that automatically tracks and logs service updates from the OpenAI Status Page. Whenever there's a new incident, outage, or degradation update related to any OpenAI API product, the program should automatically detect the update and print the affected product/service and the latest status message.</p>
<p style="margin-top:0.5rem">The solution must use an <strong>event-based approach</strong> that scales efficiently to 100+ status pages.</p>

<h2>Architecture (WebSub / Webhook)</h2>
<pre class="arch">┌──────────────┐  subscribe  ┌──────────────┐  polls   ┌──────────────┐
│ Our Webhook  │ ──────────→ │  WebSub Hub  │ ───────→ │  Atom Feed   │
│   Server     │             │              │          │ (OpenAI)     │
│              │ ←────────── │              │ ←─────── │              │
│  /callback   │  POST push  │              │  200/304 │              │
└──────────────┘             └──────────────┘          └──────────────┘
       │
       ├──→ Console output (stdout)
       └──→ SSE stream (/events) ──→ This page (live updates below)</pre>

<h2>How It Works</h2>
<ul>
  <li>A <strong>WebSub hub</strong> monitors the OpenAI Atom feed and POSTs new content to our <code>/callback</code> webhook when incidents change.</li>
  <li>Our server <strong>never polls</strong> — it only reacts to incoming webhook POSTs (events).</li>
  <li>Updates are pushed to this page via <strong>Server-Sent Events</strong> (SSE) — your browser never polls either.</li>
  <li>Scales to 100+ feeds: one subscription per feed, O(1) work per event.</li>
</ul>

<h2>Endpoints</h2>
<ul>
  <li><code>GET /</code> — This page</li>
  <li><code>GET /events</code> — SSE stream (try: <code>curl -N http://&lt;host&gt;:8080/events</code>)</li>
  <li><code>POST /callback</code> — WebSub webhook receiver</li>
  <li><code>GET /health</code> — Health check</li>
</ul>

<h2>Live Incident Feed <span class="dot"></span></h2>
<div class="feed">
  <p id="waiting">Listening for new incidents via SSE...</p>
  <div id="events"></div>
</div>

<script>
const es = new EventSource("/events");
const container = document.getElementById("events");
const waiting = document.getElementById("waiting");

es.addEventListener("incident", function(e) {
  waiting.style.display = "none";
  const d = JSON.parse(e.data);
  const div = document.createElement("div");
  div.className = "event";
  div.innerHTML =
    '<div class="ts">' + d.timestamp + ' — ' + d.provider + '</div>' +
    '<div class="title">' + d.incident + '</div>' +
    '<div class="status">Status: ' + d.status + '</div>' +
    (d.affected && d.affected.length ? '<div class="affected">Affected: ' + d.affected.join(", ") + '</div>' : '');
  container.prepend(div);
});

es.onerror = function() {
  waiting.textContent = "SSE connection lost. Reconnecting...";
  waiting.style.display = "block";
};
</script>

</body>
</html>"""
    return web.Response(text=page, content_type="text/html")


# ── WebSub Subscription ─────────────────────────────────────────────────────

async def subscribe_to_hub(
    session: aiohttp.ClientSession,
    hub_url: str,
    callback_url: str,
    topic_url: str,
) -> bool:
    """
    Send a WebSub subscription request to the hub (RFC 7574 §5.1).
    The hub will verify by GETting our callback, then start pushing updates.
    """
    secret = secrets.token_hex(20)
    subscription_secrets[topic_url] = secret

    data = {
        "hub.callback": callback_url,
        "hub.mode": "subscribe",
        "hub.topic": topic_url,
        "hub.secret": secret,
    }

    try:
        async with session.post(hub_url, data=data) as resp:
            if resp.status in (202, 204):
                print(f"[*] WebSub: subscription accepted by hub for {topic_url}")
                return True
            else:
                body = await resp.text()
                print(f"[warn] WebSub: hub returned {resp.status}: {body[:200]}")
                return False
    except aiohttp.ClientError as e:
        print(f"[error] WebSub: failed to contact hub: {e}")
        return False


# ── Hub Simulator (for local testing) ────────────────────────────────────────

async def simulate_hub(callback_url: str) -> None:
    """
    Built-in hub simulator for local testing.
    Polls the feed and POSTs to the callback when changes are detected,
    mimicking what a real WebSub hub (Superfeedr etc.) does.
    """
    print(f"[hub-sim] Simulating WebSub hub — polling feeds and pushing to {callback_url}")

    etags: dict[str, str | None] = {}
    last_mods: dict[str, str | None] = {}

    async with aiohttp.ClientSession() as session:
        while True:
            for feed in FEED_TOPICS:
                topic = feed["topic"]
                headers: dict[str, str] = {}
                if etags.get(topic):
                    headers["If-None-Match"] = etags[topic]
                if last_mods.get(topic):
                    headers["If-Modified-Since"] = last_mods[topic]

                try:
                    async with session.get(topic, headers=headers) as resp:
                        if resp.status == 304:
                            continue
                        if resp.status != 200:
                            continue

                        etags[topic] = resp.headers.get("ETag")
                        last_mods[topic] = resp.headers.get("Last-Modified")
                        body_bytes = await resp.read()

                        # Push to our webhook callback, just like a real hub would
                        push_headers = {
                            "Content-Type": "application/atom+xml",
                            "Link": f'<{topic}>; rel="self"',
                        }
                        secret = subscription_secrets.get(topic, "")
                        if secret:
                            sig = "sha1=" + hmac.new(
                                secret.encode(), body_bytes, hashlib.sha1
                            ).hexdigest()
                            push_headers["X-Hub-Signature"] = sig

                        async with session.post(
                            callback_url, data=body_bytes, headers=push_headers
                        ) as push_resp:
                            if push_resp.status != 200:
                                print(f"[hub-sim] callback returned {push_resp.status}")

                except Exception as e:
                    print(f"[hub-sim] error: {e}")

            await asyncio.sleep(HUB_SIMULATE_INTERVAL)


# ── Main ─────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    print("OpenAI Status Tracker (Event-Based — WebSub Webhook)")
    print()

    # Start the webhook server
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/events", sse_handler)
    app.router.add_get("/callback", callback_get)
    app.router.add_post("/callback", callback_post)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"[*] Webhook server listening on port {WEBHOOK_PORT}")

    callback_url = args.callback_url or f"http://localhost:{WEBHOOK_PORT}/callback"

    if args.simulate_hub:
        # Local testing mode: built-in hub simulator
        print(f"[*] Mode: simulated hub (local testing)")
        print(f"[*] The hub simulator polls the feed and POSTs to our webhook,")
        print(f"    mimicking what a real WebSub hub does in production.\n")

        # Pre-initialize trackers
        for feed in FEED_TOPICS:
            subscription_secrets[feed["topic"]] = secrets.token_hex(20)

        asyncio.create_task(simulate_hub(callback_url))
    else:
        # Production mode: subscribe to a real WebSub hub
        hub_url = args.hub_url or DEFAULT_HUB
        print(f"[*] Mode: WebSub hub subscription")
        print(f"[*] Hub: {hub_url}")
        print(f"[*] Callback: {callback_url}\n")

        async with aiohttp.ClientSession() as session:
            for feed in FEED_TOPICS:
                await subscribe_to_hub(session, hub_url, callback_url, feed["topic"])

    # Run forever — we just wait for incoming webhook POSTs (events)
    print("[*] Waiting for events (incoming webhook POSTs)...\n")
    await asyncio.Event().wait()


def main() -> None:
    global WEBHOOK_PORT

    parser = argparse.ArgumentParser(
        description="OpenAI Status Tracker — Event-Based (WebSub Webhook)"
    )
    parser.add_argument(
        "--simulate-hub",
        action="store_true",
        help="Run with a built-in hub simulator for local testing",
    )
    parser.add_argument(
        "--callback-url",
        help="Public URL for the webhook callback (required for real hub)",
    )
    parser.add_argument(
        "--hub-url",
        help=f"WebSub hub URL (default: {DEFAULT_HUB})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=WEBHOOK_PORT,
        help=f"Port for the webhook server (default: {WEBHOOK_PORT})",
    )
    args = parser.parse_args()
    WEBHOOK_PORT = args.port

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[*] Stopped.")


if __name__ == "__main__":
    main()
