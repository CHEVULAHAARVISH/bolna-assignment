"""Incident tracking logic shared by both tracker implementations."""

from typing import Any, Callable

import feedparser

from .parsing import format_ts, parse_components, parse_status


def format_incident(provider: str, entry: feedparser.FeedParserDict) -> dict[str, Any]:
    """Parse a feed entry into a structured incident dict."""
    title = entry.get("title", "Unknown Incident")
    updated = entry.get("updated", entry.get("published", ""))
    summary_html = entry.get("summary", "")

    status = parse_status(summary_html)
    components = parse_components(summary_html)
    ts = format_ts(updated)

    product = f"{provider} API"
    if components:
        product += f" - {', '.join(components)}"

    return {
        "timestamp": ts,
        "provider": provider,
        "incident": title,
        "product": product,
        "status": status,
        "affected": components,
    }


def print_incident(incident: dict[str, Any]) -> None:
    """Print an incident to stdout in the assignment-required format."""
    print(f"[{incident['timestamp']}] Product: {incident['product']}")
    print(f"Status: {incident['status']}")
    print()


class IncidentTracker:
    """Tracks seen incidents to only report new/updated ones.

    Args:
        on_incident: Called with (provider, entry, incident_dict) for each
                     new or updated incident. Defaults to printing to stdout.
    """

    def __init__(
        self,
        on_incident: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._seen: dict[str, str] = {}  # incident_id -> last updated timestamp
        self._initialized = False
        self._on_incident = on_incident or print_incident

    def process_feed(self, provider: str, feed: str | bytes | feedparser.FeedParserDict) -> None:
        """Process a feed payload (raw or pre-parsed), reporting new/updated incidents."""
        parsed = feed if hasattr(feed, "entries") else feedparser.parse(feed)
        if not parsed.entries:
            return

        if not self._initialized:
            for e in parsed.entries:
                eid = e.get("id", e.get("link", ""))
                self._seen[eid] = e.get("updated", e.get("published", ""))
            print(
                f"[*] {provider}: baseline loaded ({len(parsed.entries)} incidents). "
                f"Watching for new updates...\n"
            )
            self._initialized = True
            return

        for e in parsed.entries:
            eid = e.get("id", e.get("link", ""))
            updated = e.get("updated", e.get("published", ""))
            prev = self._seen.get(eid)

            if prev is None or updated != prev:
                self._seen[eid] = updated
                incident = format_incident(provider, e)
                self._on_incident(incident)
