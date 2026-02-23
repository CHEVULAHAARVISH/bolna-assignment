# OpenAI Status Page Tracker

Automatically tracks and logs service updates from the [OpenAI Status Page](https://status.openai.com/).

Two implementations included — a practical one and a truly event-based one.

## Setup

```bash
pip install -r requirements.txt
```

---

## Solution 1: Simple Tracker (Efficient Feed Polling)

Uses the Atom feed with conditional HTTP requests (`ETag` + `Last-Modified`). The server returns `304 Not Modified` when nothing has changed, so data is only downloaded on actual updates.

```bash
python simple_tracker.py
```

**How it scales:** Each feed runs as its own `asyncio` task. Add 100 feeds — they all run concurrently in a single process with minimal overhead.

---

## Solution 2: Webhook Tracker (Truly Event-Based — WebSub)

Our code **never polls**. A WebSub hub monitors the feed and POSTs new content to our webhook when incidents change.

```
┌──────────────┐  subscribe  ┌──────────────┐  polls   ┌──────────────┐
│ Our Webhook  │ ──────────→ │  WebSub Hub  │ ───────→ │  Atom Feed   │
│   Server     │             │ (Superfeedr) │          │ (OpenAI)     │
│              │ ←────────── │              │ ←─────── │              │
│  /callback   │  POST push  │              │  200/304 │              │
└──────────────┘             └──────────────┘          └──────────────┘
       │
       ├──→ Console output (stdout)
       └──→ SSE stream (/events) ──→ Web UI (live updates)
```

```bash
# Local testing (built-in hub simulator):
python webhook_tracker.py --simulate-hub

# Production (with a real WebSub hub):
python webhook_tracker.py --callback-url https://your-server.com/callback
```

**How it scales:** Register one subscription per feed with the hub. Our server does O(1) work per incoming event, regardless of how many feeds are monitored.

**Web UI:** Visit `http://localhost:8080/` for the architecture diagram and live incident feed via SSE.

---

## Output Format

```
[2025-11-03 14:32:00] Product: OpenAI API - Chat Completions
Status: Degraded performance due to upstream issue
```

---

## Why This Approach?

The OpenAI status page (powered by incident.io) provides an Atom feed but no public push mechanism (no WebSocket, no SSE, no WebSub hub declared). At scale, services like Feedly and Superfeedr solve this by acting as WebSub hubs — they poll feeds efficiently and push updates to subscribers.

- **Solution 1** is how the data source is consumed efficiently (conditional HTTP)
- **Solution 2** is how production systems distribute updates (WebSub push to webhooks)
