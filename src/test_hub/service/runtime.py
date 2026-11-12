"""Application context — the one place where the layers are wired together.

Everything above this module (API, CLI) takes an :class:`AppContext`
and asks it for a unit of work. Nothing above this module knows that the store
is SQLite, that the bus is in-process, or where the data directory is. That is
what makes ADR-0001 D4's promise — swap the implementation, keep the callers —
something more than a comment.

Building a context is deliberately explicit rather than a module-level global:
tests build one per temporary directory, the CLI builds one per command, and the
API builds one in its lifespan handler. A process-wide singleton would make
those three fight over the same SQLite file.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from test_hub.config import Settings, get_settings
from test_hub.domain.identity import workspace_id as workspace_uuid
from test_hub.domain.models import OverviewSnapshot, Workspace
from test_hub.logging import configure_logging, get_logger
from test_hub.service.bus import InMemoryEventBus
from test_hub.service.coalesce import Coalescer
from test_hub.storage import (
    Database,
    UnitOfWorkFactory,
    create_database,
    create_unit_of_work_factory,
)

__all__ = ["AppContext", "create_context"]

logger = get_logger(__name__)


@dataclass(slots=True)
class AppContext:
    """A ready-to-use Test Hub: settings, storage, bus and the active workspace."""

    settings: Settings
    database: Database
    uow_factory: UnitOfWorkFactory
    bus: InMemoryEventBus
    workspace: Workspace
    #: Every open dashboard asks for the overview the moment a result lands;
    #: this computes it once for all of them. See test_hub.service.coalesce.
    overviews: Coalescer[OverviewSnapshot] = field(default_factory=Coalescer)

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.workspace.id

    def close(self) -> None:
        """Release the connection pool and detach every live subscriber."""
        self.bus.close()
        self.database.dispose()


def create_context(
    settings: Settings | None = None,
    *,
    migrate: bool = True,
    configure_logs: bool = True,
) -> AppContext:
    """Build an :class:`AppContext`.

    ``migrate=True`` brings the schema to head before returning, which is what
    every entry point wants: a user who upgrades Test Hub and runs ``serve``
    should not have to know that Alembic exists. Tests that want a bare schema
    pass ``migrate=False`` and call ``database.create_all()`` themselves.
    """
    settings = settings or get_settings()
    if configure_logs:
        configure_logging(settings)
    settings.ensure_directories()

    database = create_database(settings)
    if database.backend != "sqlite":
        # A server can be down, firewalled or still starting; say which one and
        # where, before Alembic turns it into a traceback. A SQLite file is
        # created on demand and has nothing to be unreachable.
        database.ping()
    if migrate:
        database.upgrade()

    uow_factory = create_unit_of_work_factory(database, settings)
    bus = InMemoryEventBus(
        replay_buffer=settings.sse_replay_buffer,
        max_clients=settings.sse_max_clients,
    )

    with uow_factory() as uow:
        workspace = uow.runs.ensure_workspace(settings.workspace_slug, settings.workspace_name)
        uow.commit()

    expected = workspace_uuid(settings.workspace_slug)
    if workspace.id != expected:
        # Not fatal — an existing database wins — but worth saying out loud,
        # because a publisher computing ids offline will disagree with us.
        logger.warning(
            "workspace %s has id %s, not the derived %s; offline publishers that compute "
            "workspace ids will need the real one",
            settings.workspace_slug,
            workspace.id,
            expected,
        )

    logger.debug(
        "context ready: db=%s artifacts=%s workspace=%s",
        # Masked: a PostgreSQL URL usually carries its password.
        database.display_url,
        settings.resolved_artifacts_dir,
        workspace.id,
    )
    return AppContext(
        settings=settings,
        database=database,
        uow_factory=uow_factory,
        bus=bus,
        workspace=workspace,
    )
