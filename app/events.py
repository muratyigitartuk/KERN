from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.metrics import metrics

logger = logging.getLogger(__name__)


class EventHub:
    def __init__(self, max_queue_size: int = 8) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue_size = max_queue_size
        self._events_dropped_total: int = 0

    @property
    def events_dropped_total(self) -> int:
        return self._events_dropped_total

    async def publish(self, event: dict[str, Any]) -> None:
        for subscriber in list(self._subscribers):
            if subscriber.full():
                self._drop_stale_item(subscriber, event)
            try:
                subscriber.put_nowait(event)
            except asyncio.QueueFull:
                self._events_dropped_total += 1
                metrics.inc("kern_events_dropped_total")
                if self._events_dropped_total % 100 == 1:
                    logger.warning(
                        "Event dropped (type=%s, subscribers=%d, total_dropped=%d)",
                        event.get("type", "unknown"),
                        len(self._subscribers),
                        self._events_dropped_total,
                    )
                continue

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    def _drop_stale_item(self, queue: asyncio.Queue[dict[str, Any]], incoming: dict[str, Any]) -> None:
        if queue.empty():
            return
        items: list[dict[str, Any]] = []
        dropped = False
        while not queue.empty():
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not dropped and item.get("type") == "snapshot" and incoming.get("type") == "snapshot":
                dropped = True
                continue
            items.append(item)
        for item in items:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                break
