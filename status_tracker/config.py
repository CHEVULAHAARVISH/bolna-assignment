"""Shared configuration for both tracker implementations."""

FEED_TOPICS: list[dict[str, str]] = [
    {
        "name": "OpenAI",
        "url": "https://status.openai.com/feed.atom",
    },
    # Add more feeds to monitor concurrently:
    # {"name": "GitHub", "url": "https://www.githubstatus.com/history.atom"},
]

POLL_INTERVAL = 30  # seconds between feed checks
WEBHOOK_PORT = 8080
DEFAULT_HUB = "https://push.superfeedr.com/"
HUB_SIMULATE_INTERVAL = 30  # seconds, only used with --simulate-hub
