"""Feed parsing helpers shared by both tracker implementations."""

import html
import re
from datetime import datetime, timezone


def strip_html(text: str) -> str:
    """Remove HTML tags, converting <br> to newlines and <li> to bullet points."""
    clean = re.sub(r"<br\s*/?>", "\n", text)
    clean = re.sub(r"<li>", "  - ", clean)
    clean = re.sub(r"<[^>]+>", "", clean)
    return html.unescape(clean).strip()


def parse_components(summary_html: str) -> list[str]:
    """Extract affected component names from an incident summary."""
    items = re.findall(r"<li>\s*(.+?)\s*</li>", summary_html, re.DOTALL)
    return [strip_html(c) for c in items]


def parse_status(summary_html: str) -> str:
    """Extract the status label and detail text from an incident summary."""
    match = re.search(
        r"<b>Status:\s*(.+?)</b>(.*?)(?:<b>Affected|$)",
        summary_html,
        re.DOTALL,
    )
    if match:
        status = strip_html(match.group(1))
        detail = strip_html(match.group(2))
        if detail:
            return f"{status} — {detail}"
        return status
    return strip_html(summary_html)


def format_ts(ts_str: str) -> str:
    """Convert an ISO 8601 timestamp to local 'YYYY-MM-DD HH:MM:SS' format."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return ts_str
