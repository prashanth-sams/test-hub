"""Writer coordination: what SQLite gives the write path for free, PostgreSQL asks for.

The fold in :mod:`test_hub.storage.repositories` reads before it writes. It
checks which events are already stored, looks up the suite, the test and the
attempt it is about to touch, and finally recounts the whole run from its
attempt rows. On SQLite that is safe by construction: every write transaction
opens with ``BEGIN IMMEDIATE`` and holds the single database write lock for its
whole life, so nothing can interleave between a read and the write that depends
on it (ADR-0001 D4).

PostgreSQL has no such lock, which is the point of using it — two CI jobs
publishing two different runs should not wait for each other. But two batches
for the *same* run must still not interleave, or the second recount overwrites
the first with a total that never existed (``tests/test_concurrency.py`` is the
proof). :class:`WriteLocks` restores exactly that much serialization and no
more, with transaction-scoped advisory locks:

``run``
    Every write to one run queues behind the others; writes to different runs
    proceed in parallel. Taken by ``apply_events`` and ``next_sequence``.

``idempotency key``
    A retried import arrives with a new run id but the same key. Without this
    lock both copies would miss each other's key and the second would fail on
    the primary key instead of being answered from the first. Taken before the
    run lock, so the usual order is always key → run.

The locks are released by PostgreSQL itself when the transaction ends — commit,
rollback, or a dropped connection — so there is no unlock path to get wrong.
On SQLite every method here is a no-op.
"""

from __future__ import annotations

import hashlib
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

__all__ = ["WriteLocks", "advisory_lock_id"]


def advisory_lock_id(namespace: str, value: str) -> int:
    """A stable signed 64-bit key for ``pg_advisory_xact_lock``.

    Hashed rather than derived from the UUID's own bits so that a run id and an
    idempotency key can never land on the same number by construction, only by
    a 1-in-2⁶⁴ collision — and a collision costs nothing but an unnecessary
    wait, never correctness.
    """
    digest = hashlib.blake2b(f"test-hub:{namespace}:{value}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


_LOCK = sa.text("SELECT pg_advisory_xact_lock(CAST(:key AS BIGINT))")


class WriteLocks:
    """Transaction-scoped write coordination for one unit of work.

    Owned by :class:`~test_hub.storage.uow.SqlUnitOfWork`, which calls
    :meth:`released` whenever its transaction ends so that the next transaction
    on the same session asks again.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._enabled = session.get_bind().dialect.name == "postgresql"
        self._held: set[int] = set()

    @property
    def enabled(self) -> bool:
        """True when this backend needs explicit locks (PostgreSQL)."""
        return self._enabled

    def run(self, run_id: uuid.UUID) -> None:
        """Serialize this transaction against every other writer of ``run_id``."""
        self._acquire("run", run_id.hex)

    def idempotency_key(self, key: str) -> None:
        """Serialize this transaction against every other import using ``key``."""
        self._acquire("idempotency", key)

    def released(self) -> None:
        """The transaction ended, and PostgreSQL released every lock with it."""
        self._held.clear()

    def _acquire(self, namespace: str, value: str) -> None:
        if not self._enabled:
            return
        key = advisory_lock_id(namespace, value)
        if key in self._held:
            return
        # Push anything pending first: expiring below would otherwise discard it.
        self._session.flush()
        self._session.execute(_LOCK, {"key": key})
        self._held.add(key)
        # Whatever this transaction read *before* it got the lock may since have
        # been rewritten by the writer it just waited for — the service layer
        # loads the run to check it exists before it publishes to it. Expiring
        # makes the next attribute access read the committed row instead of
        # trusting a stale copy in the identity map.
        self._session.expire_all()
