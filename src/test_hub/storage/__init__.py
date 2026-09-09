"""Storage: the SQLite implementation of the protocols in :mod:`test_hub.storage.base`.

Everything above this package talks to a :class:`~test_hub.storage.uow.SqlUnitOfWork`
and never to a table, a session or an engine. That is the seam ADR-0001 D4 keeps
open for PostgreSQL and object storage; the names re-exported here are the whole
of what the rest of the application is allowed to know.
"""

from __future__ import annotations

from test_hub.storage.base import (
    ArtifactStore,
    EventBus,
    EventRepository,
    RunFilter,
    RunRepository,
    StoredArtifact,
    UnitOfWork,
    WriteResult,
)
from test_hub.storage.db import Database, create_database, migrations_path
from test_hub.storage.uow import SqlUnitOfWork, UnitOfWorkFactory, create_unit_of_work_factory

__all__ = [
    "ArtifactStore",
    "Database",
    "EventBus",
    "EventRepository",
    "RunFilter",
    "RunRepository",
    "SqlUnitOfWork",
    "StoredArtifact",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "WriteResult",
    "create_database",
    "create_unit_of_work_factory",
    "migrations_path",
]
