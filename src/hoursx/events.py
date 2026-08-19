"""Typed event bus.

Producers (runtime, executor, conductor) publish :class:`Event` envelopes; consumers
(WebSocket gateway, SSE streams, audit log) subscribe by workspace. The in-process
bus serves a single process; when Redis is configured, :class:`RedisEventBus`
bridges publishes over pub/sub so any API replica can serve any client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_DELTA = "run.delta"  # streamed model text
    RUN_STEP = "run.step"  # a completed model/tool step
    RUN_AWAITING_APPROVAL = "run.awaiting_approval"
    RUN_FINISHED = "run.finished"
    APPROVAL_DECIDED = "approval.decided"
    CHANGE_APPLIED = "change.applied"
    CHANGE_VERIFIED = "change.verified"
    CHANGE_REVERTED = "change.reverted"
    DOCUMENT_INGESTED = "document.ingested"
    SCHEDULE_FIRED = "schedule.fired"


class Event(BaseModel):
    """The envelope every subscriber receives. ``payload`` shape is owned by the
    event type; workspace scoping is mandatory so fanout can never cross tenants."""

    type: EventType
    workspace_id: str
    session_id: str | None = None
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    at: float = Field(default_factory=time.time)


class EventBus:
    """In-process pub/sub with per-subscriber bounded queues.

    A slow subscriber drops its own oldest events rather than blocking producers —
    streaming UI cares about liveness, and durable state is in the database anyway.
    """

    _QUEUE_LIMIT = 512

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(event.workspace_id, ()))
        for queue in queues:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)

    async def subscribe(self, workspace_id: str) -> AsyncIterator[Event]:
        """Yield events for one workspace until the consumer disconnects."""
        queue: asyncio.Queue[Event] = asyncio.Queue(self._QUEUE_LIMIT)
        async with self._lock:
            self._subscribers.setdefault(workspace_id, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.get(workspace_id, set()).discard(queue)


class RedisEventBus(EventBus):
    """Extends the in-process bus with Redis pub/sub fanout across processes."""

    _CHANNEL_PREFIX = "hoursx:events:"

    def __init__(self, redis_url: str) -> None:
        super().__init__()
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def publish(self, event: Event) -> None:
        await super().publish(event)  # local subscribers see it immediately
        await self._redis.publish(
            self._CHANNEL_PREFIX + event.workspace_id, event.model_dump_json()
        )

    async def subscribe(self, workspace_id: str) -> AsyncIterator[Event]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._CHANNEL_PREFIX + workspace_id)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                yield Event.model_validate(json.loads(message["data"]))
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()


def build_event_bus(redis_url: str | None) -> EventBus:
    """Select the bus implementation from configuration."""
    return RedisEventBus(redis_url) if redis_url else EventBus()
