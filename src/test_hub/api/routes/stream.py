"""``GET /api/v1/events/stream`` — Server-Sent Events for the live dashboard.

SSE rather than WebSockets because the traffic is entirely one-way and SSE gives
us three things for free that we would otherwise have to build: automatic
reconnection in the browser, ``Last-Event-ID`` replay, and passage through any
HTTP proxy without an upgrade handshake (ADR-0001 D6).

The wire format:

.. code-block:: text

    event: hello
    id: 42
    data: {"workspace_id": "...", "replaying": false}

    event: test-hub
    id: 43
    data: {"event": {...CloudEvent...}, "received_at": "..."}

    : heartbeat                       <- a comment, every 15s by default

    event: resync
    id: 43
    data: {"reason": "cursor_expired"}

``id:`` is a bus cursor, not the CloudEvent's own id — a replayed event keeps its
identity, so using it as the resume token would make resumption ambiguous.

A client that has been away too long gets ``resync`` and refetches over REST.
Silently skipping the gap would leave a dashboard confidently displaying stale
numbers, which is worse than a reload.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from test_hub.api.deps import Context, require_read
from test_hub.logging import get_logger
from test_hub.service.bus import BusMessage

router = APIRouter(tags=["live"], dependencies=[Depends(require_read)])
logger = get_logger(__name__)


def _frame(message: BusMessage) -> bytes:
    """Render one bus message as an SSE frame."""
    payload = json.dumps(message.payload, separators=(",", ":"), default=str)
    return (f"event: {message.sse_event}\nid: {message.cursor}\ndata: {payload}\n\n").encode()


@router.get(
    "/events/stream",
    summary="Subscribe to live run and test events",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "An open SSE stream. Reconnect with `Last-Event-ID` to resume.",
        }
    },
)
async def event_stream(
    request: Request,
    context: Context,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Open the live stream.

    ``Last-Event-ID`` is sent by the browser automatically on reconnect; a
    hand-written client should send the last ``id:`` it saw.
    """
    settings = context.settings
    subscription = context.bus.subscribe(context.workspace_id, last_event_id=last_event_id)

    async def publisher() -> AsyncIterator[bytes]:
        # `retry:` tells the browser how long to wait before reconnecting. Sent
        # once, up front, so a server restart does not produce a reconnect storm.
        yield f"retry: {int(settings.sse_heartbeat_seconds * 1000)}\n\n".encode()
        heartbeat = settings.sse_heartbeat_seconds
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await subscription.get(heartbeat)
                except StopAsyncIteration:
                    break
                if message is None:
                    # A comment frame: keeps proxies from closing an idle
                    # connection, without moving the client's Last-Event-ID.
                    yield b": heartbeat\n\n"
                    continue
                yield _frame(message)
        except asyncio.CancelledError:  # pragma: no cover - client vanished
            raise
        finally:
            subscription.close()
            logger.debug("SSE client disconnected; %s remain", context.bus.subscriber_count)

    return StreamingResponse(
        publisher(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache, no-transform",
            "connection": "keep-alive",
            # Nginx buffers proxied responses by default, which turns a live
            # stream into a stream that arrives all at once, at the end.
            "x-accel-buffering": "no",
        },
    )
