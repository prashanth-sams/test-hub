"""The live-update bus behind Server-Sent Events.

One-way fan-out from "an event was applied to the database" to "every dashboard
currently watching this workspace" (ADR-0001 D6). It is in-process on purpose:
the first release runs one Uvicorn worker, and an in-process bus needs no Redis
to be correct. The :class:`~test_hub.storage.base.EventBus` protocol is where a
cross-process implementation lands later.

Two things here are worth understanding before changing anything.

**The replay buffer.** Every published event is kept in a bounded ring per
workspace, tagged with a monotonic cursor. A browser that reconnects sends
``Last-Event-ID`` (the browser does this by itself, as long as the server wrote
``id:`` lines), and we replay from just after that cursor. If the cursor has
already fallen out of the ring, we do *not* quietly skip ahead — we send a
``resync`` directive, which tells the dashboard to refetch from REST. A dashboard
silently missing 400 events is a worse failure than one that reloads.

**Slow consumers.** Each subscriber has its own bounded queue. A browser on a bad
connection that stops draining will fill it; rather than growing the queue until
the process dies, we drop that subscriber's backlog and hand it a ``resync``.
Correctness for everyone else is worth more than continuity for one tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from test_hub.domain.events import DomainEvent
from test_hub.domain.models import utcnow
from test_hub.logging import get_logger

__all__ = ["BusMessage", "InMemoryEventBus", "Subscription"]

logger = get_logger(__name__)

MessageKind = Literal["event", "heartbeat", "resync", "hello"]


@dataclass(slots=True)
class BusMessage:
    """One thing to write down the wire.

    ``cursor`` is what becomes the SSE ``id:`` line and what comes back as
    ``Last-Event-ID``. It is a bus cursor, not the event's own id: two different
    events can never share one, whereas a replayed CloudEvent legitimately keeps
    its original id.
    """

    kind: MessageKind
    cursor: int
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def sse_event(self) -> str:
        """The SSE ``event:`` name the dashboard switches on."""
        return {
            "event": "test-hub",
            "heartbeat": "heartbeat",
            "resync": "resync",
            "hello": "hello",
        }[self.kind]


class Subscription:
    """One dashboard's view of the stream.

    Async-iterable, so an SSE endpoint is ``async for message in subscription``.
    Closing it is idempotent and safe from either side.
    """

    def __init__(self, bus: InMemoryEventBus, workspace_id: uuid.UUID, maxsize: int) -> None:
        self._bus = bus
        self.workspace_id = workspace_id
        self._queue: asyncio.Queue[BusMessage | None] = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self.dropped = 0

    def offer(self, message: BusMessage) -> None:
        """Queue a message, or mark the subscriber for resync if it is not keeping up."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped += 1
            self._drain_and_resync(message.cursor)

    def _drain_and_resync(self, cursor: int) -> None:
        """Throw away the backlog and tell the client to start again from REST."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(
                BusMessage(kind="resync", cursor=cursor, payload={"reason": "slow_consumer"})
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        self._bus._remove(self)

    async def get(self, timeout: float) -> BusMessage | None:
        """Wait up to ``timeout`` seconds for the next message.

        Returns ``None`` on timeout — the caller sends a heartbeat and asks
        again — and raises :class:`StopAsyncIteration` once the subscription has
        been closed.

        This exists instead of ``asyncio.wait_for(aiterator.__anext__(), …)``:
        ``wait_for`` *cancels* what it is waiting on, and cancelling a pending
        ``__anext__`` on an async generator can leave the generator unusable, so
        a stream would go silent after its first heartbeat. Cancelling a
        ``Queue.get()`` is well defined and harmless, which is why the timeout
        lives here rather than around the iterator.
        """
        try:
            message = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None
        if message is None:
            raise StopAsyncIteration
        return message

    def __aiter__(self) -> AsyncIterator[BusMessage]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[BusMessage]:
        try:
            while True:
                message = await self._queue.get()
                if message is None:
                    return
                yield message
        finally:
            self.close()


class InMemoryEventBus:
    """The shipped :class:`~test_hub.storage.base.EventBus`."""

    def __init__(
        self, *, replay_buffer: int = 1000, max_clients: int = 200, queue_size: int = 256
    ) -> None:
        self._replay_size = replay_buffer
        self._max_clients = max_clients
        self._queue_size = queue_size
        self._cursor = itertools.count(1)
        self._buffers: dict[uuid.UUID, deque[BusMessage]] = {}
        self._subscribers: dict[uuid.UUID, list[Subscription]] = {}
        self._oldest: dict[uuid.UUID, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------- publish

    async def publish(self, workspace_id: uuid.UUID, events: Iterable[DomainEvent]) -> None:
        """Fan out applied events. Never raises into the caller's transaction."""
        for event in events:
            message = BusMessage(
                kind="event",
                cursor=next(self._cursor),
                payload={
                    "event": event.model_dump(mode="json", by_alias=True),
                    "received_at": utcnow().isoformat(),
                },
            )
            self._remember(workspace_id, message)
            for subscription in list(self._subscribers.get(workspace_id, ())):
                subscription.offer(message)

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Remember the server's event loop so worker threads can publish onto it.

        Called once from the application's lifespan handler. Without it,
        :meth:`publish_nowait` has nowhere to schedule: every route that writes
        is a synchronous ``def`` running in a threadpool (so a slow query cannot
        block the SSE streams), and a worker thread has no running loop of its
        own to find.
        """
        self._loop = loop or asyncio.get_running_loop()

    def publish_nowait(self, workspace_id: uuid.UUID, events: Iterable[DomainEvent]) -> None:
        """Publish from synchronous code, on or off the event loop.

        Three cases, in order: already on the loop (schedule a task); on a worker
        thread with a bound loop (hand it over thread-safely); no loop at all — a
        CLI import with no server attached — where there is nobody to notify and
        that must not be an error.
        """
        materialized = list(events)
        if not materialized:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # Hold a reference until it finishes: asyncio only keeps a weak one,
            # and a garbage-collected task silently drops the update.
            task = loop.create_task(self.publish(workspace_id, materialized))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        bound = self._loop
        if bound is not None and bound.is_running():
            asyncio.run_coroutine_threadsafe(self.publish(workspace_id, materialized), bound)

    def _remember(self, workspace_id: uuid.UUID, message: BusMessage) -> None:
        buffer = self._buffers.setdefault(workspace_id, deque(maxlen=self._replay_size))
        if len(buffer) == self._replay_size and buffer:
            self._oldest[workspace_id] = buffer[0].cursor
        buffer.append(message)
        self._oldest.setdefault(workspace_id, message.cursor)

    # ----------------------------------------------------------- subscribe

    def subscribe(
        self, workspace_id: uuid.UUID, *, last_event_id: str | None = None
    ) -> Subscription:
        """Attach a dashboard, replaying whatever it missed if we still have it."""
        subscribers = self._subscribers.setdefault(workspace_id, [])
        if len(subscribers) >= self._max_clients:
            raise RuntimeError("too many live subscribers")

        subscription = Subscription(self, workspace_id, self._queue_size)
        subscribers.append(subscription)

        cursor = self._parse_cursor(last_event_id)
        latest = self._latest_cursor(workspace_id)
        subscription.offer(
            BusMessage(
                kind="hello",
                cursor=latest,
                payload={"workspace_id": str(workspace_id), "replaying": cursor is not None},
            )
        )
        if cursor is not None:
            self._replay(subscription, workspace_id, cursor)
        return subscription

    def _replay(self, subscription: Subscription, workspace_id: uuid.UUID, cursor: int) -> None:
        buffer = self._buffers.get(workspace_id)
        oldest = self._oldest.get(workspace_id)
        if buffer is None or oldest is None:
            return
        if cursor < oldest - 1:
            subscription.offer(
                BusMessage(
                    kind="resync",
                    cursor=self._latest_cursor(workspace_id),
                    payload={"reason": "cursor_expired", "oldest_available": oldest},
                )
            )
            return
        for message in buffer:
            if message.cursor > cursor:
                subscription.offer(message)

    def latest_cursor(self, workspace_id: uuid.UUID) -> int:
        """The cursor of the newest event published for this workspace; 0 if none.

        It doubles as a data version: an event is published only after the
        write that produced it has committed, so anything that starts reading
        after this value was taken sees every change up to it. That is what
        lets concurrent overview requests share one computation safely — see
        :mod:`test_hub.service.coalesce`.
        """
        return self._latest_cursor(workspace_id)

    def _latest_cursor(self, workspace_id: uuid.UUID) -> int:
        buffer = self._buffers.get(workspace_id)
        return buffer[-1].cursor if buffer else 0

    @staticmethod
    def _parse_cursor(last_event_id: str | None) -> int | None:
        """Parse ``Last-Event-ID``, tolerating whatever a proxy did to it."""
        if not last_event_id:
            return None
        try:
            return int(last_event_id.strip())
        except (TypeError, ValueError):
            return None

    def _remove(self, subscription: Subscription) -> None:
        subscribers = self._subscribers.get(subscription.workspace_id)
        if not subscribers:
            return
        with contextlib.suppress(ValueError):
            subscribers.remove(subscription)

    # ------------------------------------------------------------ heartbeat

    def heartbeat(self, workspace_id: uuid.UUID) -> BusMessage:
        """A keep-alive.

        Not buffered and not given a fresh cursor: a heartbeat must never move a
        client's ``Last-Event-ID`` past an event it has not seen.
        """
        return BusMessage(
            kind="heartbeat",
            cursor=self._latest_cursor(workspace_id),
            payload={"time": utcnow().isoformat()},
        )

    @property
    def subscriber_count(self) -> int:
        return sum(len(subscribers) for subscribers in self._subscribers.values())

    def close(self) -> None:
        """Detach every subscriber — used on application shutdown."""
        self._loop = None
        for subscribers in list(self._subscribers.values()):
            for subscription in list(subscribers):
                subscription.close()
        self._subscribers.clear()
