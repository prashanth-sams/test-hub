"""The unit of work: one session, three repositories, one transaction.

Every write in Test Hub is several writes. Importing a JUnit file appends
events, creates a run, inserts suites, tests and attempts, rewrites the folded
counts, stores the original file as an artifact and records which adapter
parsed it. Half of that landing is worse than none of it landing: a run whose
counts say forty tests and whose detail page shows nineteen is a bug report
nobody can reproduce.

So the repositories do not own transactions. They share a session, they flush,
and they never commit; the caller opens a unit of work, does the whole job, and
commits once::

    with uow_factory() as uow:
        uow.events.apply_events(events, idempotency_key=key)
        uow.runs.annotate_run(run_id, source_adapter="junit-xml")
        uow.commit()

Leaving the block without committing rolls back — the safe default, because the
failure mode of forgetting to roll back is a half-written database and the
failure mode of forgetting to commit is a visibly missing import.
"""

from __future__ import annotations

from typing import Any, Protocol

from test_hub.config import Settings
from test_hub.storage.artifacts import FilesystemArtifactStore
from test_hub.storage.db import Database
from test_hub.storage.locks import WriteLocks
from test_hub.storage.repositories import SqlEventRepository, SqlRunRepository

__all__ = ["SqlUnitOfWork", "UnitOfWorkFactory", "create_unit_of_work_factory"]


class SqlUnitOfWork:
    """A transactional scope over the store — SQLite or PostgreSQL.

    Satisfies :class:`test_hub.storage.base.UnitOfWork`. The session is opened
    eagerly so that ``uow.runs`` is usable whether or not the caller went
    through ``with`` — but ``with`` is the intended form, because only
    :meth:`__exit__` guarantees the session is returned to the pool.
    """

    def __init__(self, database: Database, settings: Settings, *, read_only: bool = False) -> None:
        self._database = database
        self._settings = settings
        self.read_only = read_only
        self._session = database.new_session(read_only=read_only)
        self._closed = False
        # One per transaction scope. A no-op on SQLite, whose BEGIN IMMEDIATE
        # already serializes writers; per-run advisory locks on PostgreSQL.
        self._locks = WriteLocks(self._session)

        self.runs = SqlRunRepository(self._session, settings)
        self.events = SqlEventRepository(self._session, settings, self.runs, locks=self._locks)
        self.artifacts = FilesystemArtifactStore(
            settings.resolved_artifacts_dir, self._session, settings
        )

    def __enter__(self) -> SqlUnitOfWork:
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.rollback()
        finally:
            self.close()

    def commit(self) -> None:
        """Commit the transaction.

        Committing a unit of work opened ``read_only=True`` is a programming
        error rather than a silent no-op: the session would have taken the write
        lock after all, defeating the reason it was opened read-only.
        """
        if self.read_only:
            raise RuntimeError(
                "this unit of work was opened read-only; open a writable one to commit"
            )
        try:
            self._session.commit()
        finally:
            # Committed or not, the transaction is over and so are its locks.
            self._locks.released()

    def rollback(self) -> None:
        """Discard anything not yet committed. A no-op after a commit."""
        try:
            self._session.rollback()
        finally:
            self._locks.released()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._session.close()

    @property
    def session(self) -> Any:
        """The underlying SQLAlchemy session.

        Exposed for the demo seeder, which occasionally needs a
        query this interface does not have a method for. Not part of the
        storage contract — anything above :mod:`test_hub.service` that reaches
        for it is reaching through an abstraction on purpose.
        """
        return self._session


#: What the service layer is handed instead of a database.
class UnitOfWorkFactory(Protocol):
    """Creates a fresh unit of work.

    Callable with no arguments for the common (writing) case, and with
    ``read_only=True`` for a scope that only queries — see
    :meth:`test_hub.storage.db.Database.new_session` for why that matters.
    """

    def __call__(self, *, read_only: bool = False) -> SqlUnitOfWork: ...


def create_unit_of_work_factory(database: Database, settings: Settings) -> UnitOfWorkFactory:
    """Bind a database and its settings into a zero-argument factory.

    A factory rather than a shared instance: a unit of work is a transaction,
    and handing the same one to two requests would let a rolled-back import take
    an unrelated one down with it.
    """
    settings.resolved_artifacts_dir.mkdir(parents=True, exist_ok=True)

    def factory(*, read_only: bool = False) -> SqlUnitOfWork:
        return SqlUnitOfWork(database, settings, read_only=read_only)

    return factory
