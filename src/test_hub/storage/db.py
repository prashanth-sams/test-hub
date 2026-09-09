"""The SQLite engine, configured the way SQLite actually has to be configured.

A default ``create_engine("sqlite:///...")`` gives you a database that loses
writes under concurrency, ignores foreign keys, deadlocks two publishers against
each other, and cannot be touched from a second thread. None of that is
SQLAlchemy's fault and all of it is fixable; this module is the fix, in one
place, so that no caller has to remember any of it.

What is set up here, and why:

``PRAGMA journal_mode=WAL``
    Readers stop blocking the writer. Without it the dashboard's polling
    freezes every time a CI job posts results.

``PRAGMA foreign_keys=ON``
    SQLite ships with foreign keys *disabled*. ``ON DELETE CASCADE`` is silently
    a no-op until you say this on every single connection.

``PRAGMA synchronous=NORMAL``
    The right pairing with WAL: durable across a process crash, and only at risk
    from a power cut, in exchange for not fsyncing every commit.

``PRAGMA busy_timeout``
    A second writer waits instead of failing instantly with "database is locked".

``BEGIN IMMEDIATE``
    The important one. SQLite's default deferred transaction takes a read lock
    first and asks for the write lock only at the first write — so two
    publishers that both read and then write deadlock on the *upgrade*, and one
    of them dies with SQLITE_BUSY no matter how long the busy timeout is,
    because neither can make progress. Taking the write lock up front turns that
    deadlock into a queue. SQLAlchemy cannot be told to do this directly, so
    pysqlite's own implicit ``BEGIN`` is disabled (``isolation_level = None``)
    and the statement is issued on SQLAlchemy's ``begin`` event instead. The
    cost is that read-only transactions also queue behind a writer; that is the
    single-writer ceiling ADR-0001 D4 documents rather than hides — except that
    a transaction which only ever reads has no business holding the write lock
    at all. ``new_session(read_only=True)`` marks the connection so this
    listener issues a plain deferred ``BEGIN``, which is what a long export or a
    large overview query should use so that publishers keep flowing underneath
    it.

In-memory URLs (``:memory:``, ``file::memory:?cache=shared``) get a
``StaticPool``, because every new connection to ``:memory:`` is a *different*
empty database and the schema would vanish between two sessions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from test_hub.config import Settings
from test_hub.logging import get_logger
from test_hub.storage.models import Base

__all__ = ["READ_ONLY_OPTION", "Database", "create_database", "migrations_path"]

#: Execution-option key marking a connection whose transactions may begin
#: deferred. Read :func:`Database.new_session` before using it directly.
READ_ONLY_OPTION = "test_hub_read_only"

logger = get_logger(__name__)

#: Where the Alembic environment lives inside the installed package. Resolved
#: from this file rather than from ``alembic.ini`` so that ``upgrade()`` works
#: from a wheel, where the repo's ini file does not exist.
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def migrations_path() -> Path:
    """The packaged Alembic script location."""
    return _MIGRATIONS_DIR


def _is_memory_url(url: str) -> bool:
    """True for the URL forms that address a transient, per-connection database."""
    lowered = url.lower()
    return ":memory:" in lowered or "mode=memory" in lowered


class Database:
    """Owns the engine and hands out sessions. One per process, normally.

    Building this is cheap and side-effect free: no file is created and no
    connection is opened until something asks for a session. That matters for
    the CLI, which builds a context for commands that never touch the database.

    One caveat, and it only applies to in-memory URLs: a ``StaticPool`` hands
    every session the *same* connection, because a second connection to
    ``:memory:`` would be a second, empty database. Two threads writing through
    one connection would interleave each other's transactions, so an in-memory
    Test Hub is single-threaded by construction. File-backed databases get a
    connection per session and are safe to share.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._url = settings.resolved_database_url
        self._is_memory = _is_memory_url(self._url)
        self.engine = self._create_engine()
        # A shallow copy sharing the same pool — no extra connections — whose
        # transactions the `begin` listener starts deferred.
        self._read_engine = self.engine.execution_options(**{READ_ONLY_OPTION: True})
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=True,
            future=True,
        )

    # ----------------------------------------------------------- engine

    def _create_engine(self) -> Engine:
        url = make_url(self._url)
        connect_args: dict[str, Any] = {}
        engine_kwargs: dict[str, Any] = {"future": True, "echo": False}

        if url.get_backend_name() == "sqlite":
            # The API serves requests from a thread pool and the CLI hands
            # sessions between threads; pysqlite's same-thread assertion is a
            # 2006-era guard that WAL plus our own transaction discipline make
            # unnecessary.
            connect_args["check_same_thread"] = False
            connect_args["timeout"] = self._settings.sqlite_busy_timeout_ms / 1000.0
            if self._is_memory:
                # Every fresh connection to :memory: is a fresh empty database.
                # One connection, shared, is the only way the schema survives.
                engine_kwargs["poolclass"] = StaticPool

        engine = create_engine(self._url, connect_args=connect_args, **engine_kwargs)
        if url.get_backend_name() == "sqlite":
            self._install_sqlite_listeners(engine)
        return engine

    @property
    def read_engine(self) -> Engine:
        """The engine variant whose transactions begin deferred.

        ``Engine.execution_options`` returns a shallow copy that shares the same
        connection pool, so this costs no extra connections.
        """
        return self._read_engine

    def _install_sqlite_listeners(self, engine: Engine) -> None:
        busy_timeout = int(self._settings.sqlite_busy_timeout_ms)
        want_wal = not self._is_memory

        @event.listens_for(engine, "connect")
        def _configure(dbapi_connection: Any, _record: Any) -> None:
            if isinstance(dbapi_connection, sqlite3.Connection):
                # Hand transaction control to us. Without this, pysqlite emits
                # its own BEGIN before the first DML and the BEGIN IMMEDIATE
                # below would be a nested-transaction error.
                dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            try:
                if want_wal:
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout}")
            finally:
                cursor.close()

        @event.listens_for(engine, "begin")
        def _begin_immediate(connection: Connection) -> None:
            # A transaction that will not write must not take the write lock:
            # holding it for the length of a full snapshot export would stall
            # every publisher for the duration. SQLite's deferred BEGIN takes a
            # read lock only, and WAL lets that coexist with a writer.
            if connection.get_execution_options().get(READ_ONLY_OPTION):
                connection.exec_driver_sql("BEGIN DEFERRED")
                return

            # Only an in-memory URL can already be inside a transaction here:
            # StaticPool hands the same DBAPI connection to two overlapping
            # SQLAlchemy connections — a session held open while `upgrade()` or
            # `current_revision()` runs, say — and a second BEGIN on a
            # connection that already has one is an error, not a nested
            # transaction. Joining the open transaction is the only thing a
            # single shared connection can honestly do, and it costs nothing:
            # with one connection there is no second writer to serialise
            # against. A file-backed database gets a connection per session and
            # never takes this branch.
            raw: Any = connection.connection.dbapi_connection
            if isinstance(raw, sqlite3.Connection) and raw.in_transaction:
                return
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    # ---------------------------------------------------------- schema

    def create_all(self) -> None:
        """Create every table directly from the metadata.

        The fast path for tests and the demo seeder, which want a schema and do
        not care how it got there. It also stamps the Alembic version table at
        head, so a database built this way can still be upgraded later instead
        of failing with "table already exists" on the first migration.
        """
        Base.metadata.create_all(self.engine)
        self._stamp_head()

    def upgrade(self) -> None:
        """Bring the schema to head, programmatically.

        A user who upgrades Test Hub and runs ``serve`` should never have to
        learn that Alembic exists, so every entry point calls this. The engine
        is handed to ``env.py`` through ``config.attributes`` rather than being
        rebuilt from a URL string, so the migration runs on a connection with
        this class's PRAGMAs already applied.
        """
        from alembic import command

        config = self.alembic_config()
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def current_revision(self) -> str | None:
        """The migration revision this database is stamped with, if any."""
        from alembic.migration import MigrationContext

        with self.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return context.get_current_revision()

    def alembic_config(self) -> Any:
        """An Alembic ``Config`` pointing at the packaged migrations.

        Built in code rather than read from ``alembic.ini`` because the ini file
        is a repository convenience for ``alembic revision``; it is not shipped
        in the wheel, and the URL has to come from :class:`Settings` anyway.
        """
        from alembic.config import Config

        config = Config()
        config.set_main_option("script_location", str(_MIGRATIONS_DIR))
        config.set_main_option("sqlalchemy.url", self._url.replace("%", "%%"))
        config.attributes["settings"] = self._settings
        return config

    def _stamp_head(self) -> None:
        from alembic import command

        config = self.alembic_config()
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.stamp(config, "head")

    # --------------------------------------------------------- sessions

    def session(self) -> AbstractContextManager[Session]:
        """A session scope. Closes on exit; committing is the caller's business."""
        return self._session_scope()

    @contextmanager
    def _session_scope(self) -> Iterator[Session]:
        session = self.new_session()
        try:
            yield session
        finally:
            session.close()

    def new_session(self, *, read_only: bool = False) -> Session:
        """An unmanaged session. The unit of work owns its lifetime instead.

        ``read_only=True`` binds the session to an engine variant carrying the
        read-only execution option, so its transactions begin deferred and never
        block a writer. It is a promise, not an enforcement: nothing stops a
        caller writing through such a session, and if it does, SQLite will
        upgrade the lock exactly as it would for any deferred transaction.
        """
        if read_only:
            return Session(bind=self._read_engine, expire_on_commit=False)
        return self._session_factory()

    @property
    def is_memory(self) -> bool:
        return self._is_memory

    @property
    def url(self) -> str:
        return self._url

    def dispose(self) -> None:
        """Close every pooled connection. Safe to call more than once."""
        self.engine.dispose()


def create_database(settings: Settings) -> Database:
    """Build the :class:`Database` for these settings, creating its directory."""
    if not _is_memory_url(settings.resolved_database_url):
        url = make_url(settings.resolved_database_url)
        if url.get_backend_name() == "sqlite" and url.database:
            Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return Database(settings)
