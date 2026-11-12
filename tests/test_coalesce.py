"""The overview's shared computation: one for everybody, never a stale one.

Ten dashboards asking at once must cost one computation — that is the fix for a
server that stalled under a burst of results. What must never happen is a
caller being handed an answer older than the newest change it has heard about.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from test_hub.service.coalesce import Coalescer


class Gate:
    """A computation that runs only when the test says so, and counts itself."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            self.calls += 1
            number = self.calls
        self.started.set()
        assert self.release.wait(5), "the test never released the computation"
        return number


def test_callers_asking_at_once_share_one_computation() -> None:
    shared: Coalescer[int] = Coalescer()
    gate = Gate()
    with ThreadPoolExecutor(max_workers=10) as pool:
        first = pool.submit(
            shared.get, "overview", need=7, current=7, compute=gate, reuse_finished=True
        )
        assert gate.started.wait(5)
        others = [
            pool.submit(
                shared.get, "overview", need=need, current=7, compute=gate, reuse_finished=False
            )
            for need in (3, 5, 7, 7, 7, 7, 7, 7, 7)
        ]
        gate.release.set()
        results = [first.result(5)] + [future.result(5) for future in others]
    assert gate.calls == 1
    assert set(results) == {1}


def test_a_caller_that_heard_of_a_newer_change_gets_a_newer_answer() -> None:
    """The computation in flight started before change 8; it cannot include it."""
    shared: Coalescer[int] = Coalescer()
    gate = Gate()
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(
            shared.get, "overview", need=7, current=7, compute=gate, reuse_finished=True
        )
        assert gate.started.wait(5)
        newer = pool.submit(
            shared.get, "overview", need=8, current=8, compute=gate, reuse_finished=True
        )
        gate.release.set()
        assert {older.result(5), newer.result(5)} == {1, 2}
    assert gate.calls == 2


def test_a_finished_answer_is_reused_only_by_callers_on_the_event_stream() -> None:
    """A dashboard is told when something changes and asks again; anyone else
    may have written out of band, so they get a fresh computation."""
    shared: Coalescer[int] = Coalescer(ttl=60)
    calls: list[int] = []

    def compute() -> int:
        calls.append(1)
        return len(calls)

    assert shared.get("overview", need=4, current=4, compute=compute, reuse_finished=True) == 1
    assert shared.get("overview", need=4, current=4, compute=compute, reuse_finished=True) == 1
    assert shared.get("overview", need=4, current=4, compute=compute, reuse_finished=False) == 2
    # A change reached the bus: version 5 is beyond what any answer so far holds.
    assert shared.get("overview", need=5, current=5, compute=compute, reuse_finished=True) == 3


def test_a_finished_answer_expires() -> None:
    shared: Coalescer[int] = Coalescer(ttl=0)
    calls: list[int] = []

    def compute() -> int:
        calls.append(1)
        return len(calls)

    shared.get("overview", need=1, current=1, compute=compute, reuse_finished=True)
    assert shared.get("overview", need=1, current=1, compute=compute, reuse_finished=True) == 2


def test_keys_do_not_share() -> None:
    shared: Coalescer[str] = Coalescer(ttl=60)
    assert (
        shared.get(30, need=1, current=1, compute=lambda: "30 days", reuse_finished=True)
        == "30 days"
    )
    assert (
        shared.get(7, need=1, current=1, compute=lambda: "7 days", reuse_finished=True) == "7 days"
    )


def test_a_failure_is_reported_once_and_never_shared() -> None:
    """The caller whose computation failed sees the error; a caller that only
    joined it gets an attempt of its own; nothing failed is reused."""
    shared: Coalescer[int] = Coalescer(ttl=60)
    started = threading.Event()
    release = threading.Event()

    def failing() -> int:
        started.set()
        assert release.wait(5)
        raise RuntimeError("database went away")

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            shared.get, "overview", need=1, current=1, compute=failing, reuse_finished=True
        )
        assert started.wait(5)
        joiner = pool.submit(
            shared.get, "overview", need=1, current=1, compute=lambda: 42, reuse_finished=True
        )
        release.set()
        with pytest.raises(RuntimeError, match="database went away"):
            owner.result(5)
        assert joiner.result(5) == 42
    assert shared.get("overview", need=1, current=1, compute=lambda: 7, reuse_finished=True) == 7


def test_a_version_from_the_future_does_not_wait_for_it() -> None:
    """A dashboard that survived a server restart still holds the old cursors."""
    shared: Coalescer[int] = Coalescer(ttl=60)
    assert (
        shared.get("overview", need=10_000, current=3, compute=lambda: 1, reuse_finished=True) == 1
    )
    assert (
        shared.get("overview", need=10_000, current=3, compute=lambda: 2, reuse_finished=True) == 1
    )


def test_the_overview_accepts_the_cursor_it_has_seen(client: Any) -> None:
    assert client.get("/api/v1/overview", params={"after": 0}).status_code == 200
    assert client.get("/api/v1/overview", params={"after": 10**9}).status_code == 200
    assert client.get("/api/v1/overview", params={"after": -1}).status_code == 422
