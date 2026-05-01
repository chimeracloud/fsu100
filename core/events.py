"""In-process event bus used to fan out SSE updates and feed activity logs.

The bus is intentionally simple — there is no persistence and no cross-
instance distribution. Cloud Run is configured to run a single instance of
this service which is sufficient for an engine with a single Betfair
session. Events flow from many sources (the streaming thread, the order
poller, the API request handlers) into the bus, then out to any number of
SSE subscribers and into a bounded ring buffer used by the activity feed.

Sync code that needs to publish from a non-async context (the streaming
thread, settlement worker, etc.) should use :meth:`EventBus.publish_threadsafe`
which schedules the publish on the bus's bound asyncio loop.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from core.logging import get_logger
from models.schemas import ActivityEvent

logger = get_logger(__name__)


class EventBus:
    """Pub/sub primitive backed by per-subscriber asyncio queues."""

    def __init__(self, activity_log_size: int) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._activity: deque[ActivityEvent] = deque(maxlen=activity_log_size)
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the bus to the running asyncio loop.

        Required before any thread-side code calls
        :meth:`publish_threadsafe`. Called once during application
        startup from the lifespan handler.
        """

        self._loop = loop

    async def publish(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        market_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Publish an event to all current subscribers and the activity log.

        Args:
            event: Short event identifier (e.g. ``bet_placed``).
            payload: Optional structured data emitted to SSE subscribers.
            market_id: Optional market the event relates to.
            detail: Optional human-readable description for the activity log.
        """

        record = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": market_id,
            "data": payload or {},
        }

        async with self._lock:
            self._activity.appendleft(
                ActivityEvent(
                    timestamp=datetime.now(timezone.utc),
                    event=event,
                    market_id=market_id,
                    detail=detail,
                )
            )
            dead: list[asyncio.Queue[dict[str, Any]]] = []
            for queue in self._subscribers:
                try:
                    queue.put_nowait(record)
                except asyncio.QueueFull:
                    dead.append(queue)
            for queue in dead:
                self._subscribers.remove(queue)

    def publish_threadsafe(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        market_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Schedule a publish from a non-async thread.

        No-op if the bus has not been bound to a loop yet.
        """

        loop = self._loop
        if loop is None:
            logger.debug(
                "publish_threadsafe called before bus is bound to a loop",
                extra={"event": event},
            )
            return
        coro = self.publish(
            event, payload, market_id=market_id, detail=detail
        )
        asyncio.run_coroutine_threadsafe(coro, loop)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Async iterator yielding events for the lifetime of the connection."""

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                yield item
        finally:
            async with self._lock:
                if queue in self._subscribers:
                    self._subscribers.remove(queue)

    async def recent_activity(self) -> list[ActivityEvent]:
        """Return a snapshot of the activity ring buffer (newest first)."""

        async with self._lock:
            return list(self._activity)

    async def clear_activity(self) -> None:
        """Drop every entry from the activity ring buffer."""

        async with self._lock:
            self._activity.clear()


_BUS: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the lazily-initialised process-wide :class:`EventBus`."""

    global _BUS
    if _BUS is None:
        _BUS = EventBus(activity_log_size=get_settings().activity_log_size)
    return _BUS
