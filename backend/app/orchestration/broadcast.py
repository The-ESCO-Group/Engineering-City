"""In-process event fan-out to connected WebSocket clients.

Deliberately not Redis. The product is a local, single-process deployment, and
the durable record of what happened is the ``events`` table — this broker only
carries live notifications to whoever is currently watching. A subscriber that
falls behind or disconnects loses nothing it cannot recover by replaying from
``?since_seq=``.

That is also why a slow client is dropped rather than allowed to apply
back-pressure: one stalled browser tab must not be able to block the workflow
engine.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Frames buffered per subscriber before the slowest ones are discarded.
SUBSCRIBER_QUEUE_SIZE = 256


class EventBroker:
    """Fan-out of event envelopes to per-project subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()
        #: Set once the app is running, so publishers on worker threads know
        #: which loop to hand work to.
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @asynccontextmanager
    async def subscribe(
        self, project_id: uuid.UUID
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Register a queue for one project, cleaned up on exit."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        async with self._lock:
            self._subscribers[project_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers[project_id].discard(queue)
                if not self._subscribers[project_id]:
                    del self._subscribers[project_id]

    def subscriber_count(self, project_id: uuid.UUID) -> int:
        return len(self._subscribers.get(project_id, ()))

    async def publish(self, project_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        """Deliver one envelope to every current subscriber of a project."""
        for queue in list(self._subscribers.get(project_id, ())):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # The client is not keeping up. Drop the frame; it can recover
                # the full ordering from the events table by sequence number.
                logger.warning(
                    "dropping event for a slow websocket subscriber",
                    extra={
                        "project_id": str(project_id),
                        "event_type": envelope.get("event_type"),
                        "seq": envelope.get("seq"),
                    },
                )

    def publish_threadsafe(self, project_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        """Publish from a thread that is not running the event loop.

        The worker loop and FastAPI's sync dependencies both run off-loop, so
        this is the path most events actually take. A no-op when no loop is
        bound (tests, CLI scripts) — persistence has already happened.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.publish(project_id, envelope), loop)
        except RuntimeError:  # pragma: no cover - loop shutting down
            pass


_broker = EventBroker()


def get_broker() -> EventBroker:
    """Process-wide broker."""
    return _broker
