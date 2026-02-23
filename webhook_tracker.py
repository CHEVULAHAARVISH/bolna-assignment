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
import json
import pathlib
import secrets
from typing import Any

import aiohttp
from aiohttp import web

from status_tracker.config import (
    DEFAULT_HUB,
    FEED_TOPICS,
    HUB_SIMULATE_INTERVAL,
    WEBHOOK_PORT,
)
from status_tracker.sse import SSEBus
from status_tracker.tracker import IncidentTracker, print_incident

# ── Global State ────────────────────────────────────────────────────────────

sse_bus = SSEBus()
trackers: dict[str, IncidentTracker] = {}
subscription_secrets: dict[str, str] = {}

TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"


# ── Incident Handling ───────────────────────────────────────────────────────

def handle_incident(incident: dict[str, Any]) -> None:
    """Print incident and push to SSE clients."""
    print_incident(incident)
    task = asyncio.create_task(sse_bus.publish(incident))
    task.add_done_callback(
        lambda t: t.exception() if not t.cancelled() and t.exception() else None
    )


def get_tracker(topic_url: str) -> tuple[IncidentTracker, str]:
    """Get or create a tracker for a topic URL. Returns (tracker, provider_name)."""
    for feed in FEED_TOPICS:
        if feed["url"] == topic_url:
            name = feed["name"]
            if topic_url not in trackers:
                trackers[topic_url] = IncidentTracker(on_incident=handle_incident)
            return trackers[topic_url], name
    # Unknown topic
    if topic_url not in trackers:
        trackers[topic_url] = IncidentTracker(on_incident=handle_incident)
    return trackers[topic_url], topic_url


# ── WebSub Webhook Handlers ────────────────────────────────────────────────

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

    # Determine which topic this is for (from Link header or body content)
    topic = None
    link_header = request.headers.get("Link", "")
    for feed in FEED_TOPICS:
        if feed["url"] in link_header or feed["url"] in body.decode("utf-8", errors="ignore"):
            topic = feed["url"]
            break

    if topic is None and FEED_TOPICS:
        topic = FEED_TOPICS[0]["url"]
        print(f"[warn] Could not match webhook POST to a known topic — defaulting to {topic}")

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


# ── HTTP Handlers ───────────────────────────────────────────────────────────

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
    """Serve the web UI from templates/index.html."""
    page = (TEMPLATES_DIR / "index.html").read_text()
    return web.Response(text=page, content_type="text/html")


# ── WebSub Subscription ────────────────────────────────────────────────────

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


# ── Hub Simulator (for local testing) ──────────────────────────────────────

async def simulate_hub(callback_url: str) -> None:
    """
    Built-in hub simulator for local testing.
    Polls the feed and POSTs to the callback when changes are detected,
    mimicking what a real WebSub hub (Superfeedr etc.) does.
    """
    print(f"[hub-sim] Simulating WebSub hub — polling feeds and pushing to {callback_url}")

    etags: dict[str, str | None] = {}
    last_mods: dict[str, str | None] = {}

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            for feed in FEED_TOPICS:
                topic = feed["url"]
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


# ── Main ────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    port = args.port

    print("OpenAI Status Tracker (Event-Based — WebSub Webhook)")
    print()

    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/events", sse_handler)
    app.router.add_get("/callback", callback_get)
    app.router.add_post("/callback", callback_post)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[*] Webhook server listening on port {port}")

    callback_url = args.callback_url or f"http://localhost:{port}/callback"

    if args.simulate_hub:
        print("[*] Mode: simulated hub (local testing)")
        print("[*] The hub simulator polls the feed and POSTs to our webhook,")
        print("    mimicking what a real WebSub hub does in production.\n")

        for feed in FEED_TOPICS:
            subscription_secrets[feed["url"]] = secrets.token_hex(20)

        asyncio.create_task(simulate_hub(callback_url))
    else:
        hub_url = args.hub_url or DEFAULT_HUB
        print(f"[*] Mode: WebSub hub subscription")
        print(f"[*] Hub: {hub_url}")
        print(f"[*] Callback: {callback_url}\n")

        async with aiohttp.ClientSession() as session:
            for feed in FEED_TOPICS:
                await subscribe_to_hub(session, hub_url, callback_url, feed["url"])

    print("[*] Waiting for events (incoming webhook POSTs)...\n")
    await asyncio.Event().wait()


def main() -> None:
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

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[*] Stopped.")


if __name__ == "__main__":
    main()
