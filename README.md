# OpenAI Status Page Tracker

Automatically tracks and logs service updates from the [OpenAI Status Page](https://status.openai.com/).

Two implementations included — a practical one and a truly event-based one.

---

## Solution 1: Simple Tracker (Efficient Feed Polling)

Uses the Atom feed with conditional HTTP requests (`ETag` + `Last-Modified`). The server returns `304 Not Modified` when nothing has changed, so data is only downloaded on actual updates.

```bash
pip install -r requirements.txt
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

### Local testing (built-in hub simulator):
```bash
python webhook_tracker.py --simulate-hub
```

### Production (with a real WebSub hub):
```bash
python webhook_tracker.py --callback-url https://your-server.com/callback --hub-url https://push.superfeedr.com/
```

**How it scales:** Register one subscription per feed with the hub. Our server does O(1) work per incoming event, regardless of how many feeds are monitored.

**Web UI:** Visit `http://<host>:8080/` to see the assignment overview, architecture diagram, and live incident feed via SSE.

---

## Output Format

```
[2025-11-03 14:32:00] Product: OpenAI API - Chat Completions
Status: Degraded performance due to upstream issue
```

## Docker

```bash
docker build -t status-tracker .
docker run -p 8080:8080 status-tracker
```

---

## Deploy to EC2

### 1. Launch EC2 Instance

- Go to **AWS Console > EC2 > Launch Instance**
- **AMI:** Ubuntu Server 22.04 LTS (free tier eligible)
- **Instance type:** `t2.micro` (free tier)
- **Key pair:** Create or select one (download the `.pem` file)
- **Security group:** Allow inbound:
  - SSH (port 22) — from your IP
  - Custom TCP (port **8080**) — from 0.0.0.0/0

### 2. SSH In

```bash
chmod 400 your-key.pem
ssh -i your-key.pem ubuntu@<EC2-PUBLIC-IP>
```

### 3. Install Python & Dependencies

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv
mkdir ~/tracker && cd ~/tracker
python3 -m venv venv
source venv/bin/activate
```

### 4. Copy Code (from your local machine)

```bash
scp -i your-key.pem \
  simple_tracker.py webhook_tracker.py requirements.txt \
  ubuntu@<EC2-PUBLIC-IP>:~/tracker/
```

Then back on EC2:

```bash
cd ~/tracker
source venv/bin/activate
pip install -r requirements.txt
```

### 5. Set Up systemd Service (runs forever, survives SSH disconnect)

```bash
sudo tee /etc/systemd/system/status-tracker.service << 'EOF'
[Unit]
Description=OpenAI Status Tracker
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/tracker
ExecStart=/home/ubuntu/tracker/venv/bin/python webhook_tracker.py --simulate-hub --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now status-tracker
```

### 6. Verify

```bash
# Check status:
sudo systemctl status status-tracker

# View live logs:
journalctl -u status-tracker -f

# Test health endpoint:
curl http://localhost:8080/health
```

### 7. Your Hosted Version

Open in browser: `http://<EC2-PUBLIC-IP>:8080/`

This shows the assignment description, architecture diagram, and live incident feed.

---

## Why This Approach?

The OpenAI status page (powered by incident.io) provides an Atom feed but no public push mechanism (no WebSocket, no SSE, no WebSub hub declared). At scale, services like Feedly and Superfeedr solve this by acting as WebSub hubs — they poll feeds efficiently and push updates to subscribers.

- **Solution 1** is how the data source is consumed efficiently (conditional HTTP)
- **Solution 2** is how production systems distribute updates (WebSub push to webhooks)
