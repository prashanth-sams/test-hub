"""The live bus: fan-out, replay, resync and slow consumers."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from test_hub.domain.events import EventType, RunStartedData, make_event
from test_hub.domain.models import utcnow
from test_hub.service.bus import InMemoryEventBus

pytestmark = pytest.mark.asyncio


def event(workspace: uuid.UUID, run: uuid.UUID, sequence: int) -> object:
    return make_event(
        EventType.RUN_STARTED,
        run_id=run,
        workspace_id=workspace,
        sequence=sequence,
        data=RunStartedData(started_at=utcnow()),
    )


async def drain(subscription: object) -> list[tuple[str, int]]:
    """Everything currently queued, without waiting for more."""
    out: list[tuple[str, int]] = []
    queue = subscription._queue  # type: ignore[attr-defined]
    while not queue.empty():
        message = queue.get_nowait()
        if message is None:
            break
        out.append((message.kind, message.cursor))
    return out


async def test_a_subscriber_receives_published_events() -> None:
    bus = InMemoryEventBus()
    workspace, run = uuid.uuid4(), uuid.uuid4()
    subscription = bus.subscribe(workspace)
    await bus.publish(workspace, [event(workspace, run, 1)])

    messages = await drain(subscription)
    assert [kind for kind, _ in messages] == ["hello", "event"]


async def test_events_do_not_cross_workspaces() -> None:
    bus = InMemoryEventBus()
    first, second = uuid.uuid4(), uuid.uuid4()
    watcher = bus.subscribe(first)
    await bus.publish(second, [event(second, uuid.uuid4(), 1)])
    assert [kind for kind, _ in await drain(watcher)] == ["hello"]


async def test_reconnect_replays_only_what_was_missed() -> None:
    bus = InMemoryEventBus(replay_buffer=50)
    workspace, run = uuid.uuid4(), uuid.uuid4()
    for sequence in range(5):
        await bus.publish(workspace, [event(workspace, run, sequence)])

    resumed = bus.subscribe(workspace, last_event_id="3")
    messages = await drain(resumed)
    assert [kind for kind, _ in messages] == ["hello", "event", "event"]
    assert [cursor for kind, cursor in messages if kind == "event"] == [4, 5]


async def test_a_cursor_that_has_fallen_out_of_the_buffer_gets_a_resync() -> None:
    """Silently skipping the gap would leave a dashboard confidently stale."""
    bus = InMemoryEventBus(replay_buffer=3)
    workspace, run = uuid.uuid4(), uuid.uuid4()
    for sequence in range(10):
        await bus.publish(workspace, [event(workspace, run, sequence)])

    resumed = bus.subscribe(workspace, last_event_id="1")
    kinds = [kind for kind, _ in await drain(resumed)]
    assert kinds == ["hello", "resync"]


async def test_a_malformed_last_event_id_is_treated_as_a_fresh_connection() -> None:
    bus = InMemoryEventBus()
    workspace = uuid.uuid4()
    for header in ("", "not-a-number", "  "):
        subscription = bus.subscribe(workspace, last_event_id=header)
        assert [kind for kind, _ in await drain(subscription)] == ["hello"]


async def test_a_slow_consumer_is_resynced_rather_than_growing_without_limit() -> None:
    bus = InMemoryEventBus(queue_size=4)
    workspace, run = uuid.uuid4(), uuid.uuid4()
    subscription = bus.subscribe(workspace)

    for sequence in range(50):
        await bus.publish(workspace, [event(workspace, run, sequence)])

    messages = await drain(subscription)
    # The queue is bounded, the backlog was thrown away, and the client was told
    # to refetch. What it must never do is grow to hold all fifty.
    assert len(messages) <= 5
    assert any(kind == "resync" for kind, _ in messages)
    assert subscription.dropped > 0


async def test_heartbeats_do_not_advance_the_client_cursor() -> None:
    """A heartbeat that moved Last-Event-ID would skip an unseen event on reconnect."""
    bus = InMemoryEventBus()
    workspace, run = uuid.uuid4(), uuid.uuid4()
    await bus.publish(workspace, [event(workspace, run, 1)])
    before = bus.heartbeat(workspace).cursor
    after = bus.heartbeat(workspace).cursor
    assert before == after


async def test_closing_a_subscription_is_idempotent_and_deregisters_it() -> None:
    bus = InMemoryEventBus()
    workspace = uuid.uuid4()
    subscription = bus.subscribe(workspace)
    assert bus.subscriber_count == 1
    subscription.close()
    subscription.close()
    assert bus.subscriber_count == 0


async def test_the_client_cap_is_enforced() -> None:
    bus = InMemoryEventBus(max_clients=2)
    workspace = uuid.uuid4()
    bus.subscribe(workspace)
    bus.subscribe(workspace)
    with pytest.raises(RuntimeError, match="too many"):
        bus.subscribe(workspace)


async def test_iterating_a_subscription_yields_until_closed() -> None:
    bus = InMemoryEventBus()
    workspace, run = uuid.uuid4(), uuid.uuid4()
    subscription = bus.subscribe(workspace)
    await bus.publish(workspace, [event(workspace, run, 1)])

    received = []

    async def consume() -> None:
        async for message in subscription:
            received.append(message.kind)
            if len(received) == 2:
                subscription.close()

    await asyncio.wait_for(consume(), timeout=2)
    assert received == ["hello", "event"]


async def test_publish_nowait_outside_a_loop_is_a_no_op_not_a_crash() -> None:
    """A CLI import has no server attached; that must not be an error."""
    bus = InMemoryEventBus()
    workspace = uuid.uuid4()

    def synchronous() -> None:
        bus.publish_nowait(workspace, [event(workspace, uuid.uuid4(), 1)])

    await asyncio.get_running_loop().run_in_executor(None, synchronous)


async def test_close_detaches_every_subscriber() -> None:
    bus = InMemoryEventBus()
    workspace = uuid.uuid4()
    bus.subscribe(workspace)
    bus.subscribe(workspace)
    bus.close()
    assert bus.subscriber_count == 0
