"""One computation for everybody asking the same question at the same moment.

Every open dashboard refetches the overview when results arrive, and they all
hear about a result at the same instant. Computed once per request, ten open
dashboards are ten overview computations racing each other for the same
database connections and the same interpreter — and while a burst of results
lasts, the next round of refetches arrives before the last one has finished.
That pile-up is what stalled a server under load: every worker thread busy,
every pooled connection held, `/healthz` unanswered.

So concurrent requests for the same thing share one computation, and a finished
result is reused for a moment. What makes that safe is a *data version*: the
event bus's cursor, which moves on every write that reaches a dashboard. A
request says how new an answer it needs — the cursor of the newest change it
has heard about, or, if it says nothing, whatever is current when it arrives —
and it is only ever handed a computation that *started* at or after that
version. A computation that started then read a database that already held
every change up to it, because a change reaches the bus only after it commits.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

__all__ = ["Coalescer"]

T = TypeVar("T")


@dataclass(slots=True, eq=False)
class _Computation(Generic[T]):
    version: int
    done: threading.Event = field(default_factory=threading.Event)
    finished_at: float | None = None
    result: T | None = None
    failed: bool = False


class Coalescer(Generic[T]):
    """Shares computations of ``key`` between callers that need the same freshness.

    ``ttl`` bounds how long a finished result is reused when nothing has
    changed: some of what an overview says depends on the clock too (a run's
    "24 hours ago" moves on its own), so even an unchanged database gets a new
    answer every few seconds.
    """

    def __init__(self, *, ttl: float = 2.0) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._latest: dict[Hashable, _Computation[T]] = {}

    def get(
        self,
        key: Hashable,
        *,
        need: int,
        current: int,
        compute: Callable[[], T],
        reuse_finished: bool,
    ) -> T:
        """The result of ``compute`` for ``key``, including every change up to ``need``.

        ``current`` is the data version right now, read before calling: a
        computation started here reads a database holding every change up to
        it, so that is the version it is filed under — and later requests that
        need no more than that can share it. ``need`` above ``current`` is
        treated as ``current``: a version from before a restart, say, can make
        a request wait for nothing that is coming.

        ``reuse_finished`` lets a *finished* result answer this request, which
        is only right for a caller that hears about every change and asks again
        when one lands — a dashboard on the event stream. Not every write
        reaches the bus (an import from another process, a prune), so anyone
        else is only ever handed a computation still running when they asked.
        """
        need = min(need, current)
        now = time.monotonic()
        with self._lock:
            self._forget_stale(now)
            computation = self._latest.get(key)
            if computation is not None and self._serves(computation, need, now, reuse_finished):
                shared: _Computation[T] | None = computation
            else:
                shared = None
                computation = _Computation(version=current)
                self._latest[key] = computation
        if shared is not None:
            shared.done.wait()
            if not shared.failed:
                return shared.result  # type: ignore[return-value]  # set before done
            # The computation we joined failed. Its caller has the exception to
            # report; this request deserves an attempt of its own.
            return compute()
        try:
            computation.result = compute()
        except BaseException:
            computation.failed = True
            raise
        finally:
            computation.finished_at = time.monotonic()
            computation.done.set()
        return computation.result

    def _forget_stale(self, now: float) -> None:
        """Drop results too old to serve anyone. Called with the lock held.

        A key is whatever the caller asks about — for the overview, its window,
        and a custom range is a new key every time somebody picks one. A result
        past ``ttl`` can never be handed out again, so keeping it would only be
        a slow leak of every range anyone ever asked for.
        """
        stale = [
            key
            for key, computation in self._latest.items()
            if computation.finished_at is not None
            and (computation.failed or now - computation.finished_at > self._ttl)
        ]
        for key in stale:
            del self._latest[key]

    def _serves(
        self, computation: _Computation[T], need: int, now: float, reuse_finished: bool
    ) -> bool:
        if computation.failed or computation.version < need:
            return False
        if computation.finished_at is None:
            return True  # still running, and started late enough to be fresh
        return reuse_finished and now - computation.finished_at <= self._ttl
