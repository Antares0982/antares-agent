"""Per-thread event sequence, with replay.

`GET /events?after=<id>` is served from here rather than by having the client
open a stream and then reconcile it against a history fetch. The simpler
protocol is also the safer one: the reconcile version drops an
`approval.required` that lands during the gap, and the thread then sits in
`awaiting_approval` forever with nobody able to answer it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from .events import Event

Sink = Callable[[Event], Any]


class EventLog:
    def __init__(self, thread_id: str, capacity: int = 512, sink: Sink | None = None) -> None:
        self.thread_id = thread_id
        self._buffer: deque[Event] = deque(maxlen=capacity)
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._next_id = 1
        #: Called for every event once it has an id -- used to persist.
        self._sink = sink
        self._closed = False

    @property
    def last_id(self) -> int:
        return self._next_id - 1

    def publish(self, event: Event) -> Event:
        """Assign an id, buffer, and fan out. Never blocks on a slow client."""
        stamped = event.with_id(self._next_id)
        self._next_id += 1
        self._buffer.append(stamped)
        if self._sink is not None:
            self._sink(stamped)
        for queue in self._subscribers:
            queue.put_nowait(stamped)
        return stamped

    def since(self, after: int | None) -> list[Event]:
        if after is None:
            return []
        return [e for e in self._buffer if e.id > after]

    def close(self) -> None:
        self._closed = True
        for queue in self._subscribers:
            queue.put_nowait(None)

    async def stream(self, after: int | None = None) -> AsyncIterator[Event]:
        """Replay the gap, then follow live.

        Subscribing *before* replaying is deliberate: an event published while
        we are draining the buffer lands in the queue rather than falling
        between the two.
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            replayed = self.since(after)
            for event in replayed:
                yield event
            seen = replayed[-1].id if replayed else (after or 0)

            if self._closed:
                return
            while True:
                event = await queue.get()
                if event is None:
                    return
                if event.id > seen:
                    seen = event.id
                    yield event
        finally:
            self._subscribers.discard(queue)
