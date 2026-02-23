"""Server-Sent Events pub/sub bus for pushing incidents to browser clients."""

import asyncio
from typing import Any


class SSEBus:
    """Pub/sub bus for pushing incident events to SSE clients."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._recent: list[dict[str, Any]] = []

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
