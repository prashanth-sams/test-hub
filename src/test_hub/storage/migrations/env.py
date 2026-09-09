"""Alembic environment, driven from :class:`~test_hub.config.Settings`.

This file has to work in three situations that usually get three different
env.py files:

1. ``alembic revision --autogenerate`` from the repository, where ``alembic.ini``
   is present and a developer expects the URL from their environment.
2. ``alembic upgrade head`` from a shell, against an installed package.
3. :meth:`test_hub.storage.db.Database.upgrade`, called in-process, which has
   already built an engine with the WAL, foreign-key and ``BEGIN IMMEDIATE``
   behaviour the rest of the application depends on.

Case 3 is the one that matters and the one most env.py files get wrong. When a
connection is handed in through ``config.attributes``, it is used as-is —
building a second engine here would run the migration on a connection with
foreign keys switched off and the default deferred transaction, which is a
different database from the one the application uses.

``render_as_batch=True`` is not optional on SQLite. SQLite cannot drop or alter
a column; Alembic emulates it by creating a new table, copying the rows and
renaming. Without batch mode the first migration that changes a column type
fails at runtime, in production, on someone else's machine.
"""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from test_hub.storage.models import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    """Settings first, then whatever ``alembic.ini`` or ``-x`` supplied.

    The URL belongs to the application, not to the migration tool: a user who
    set ``TEST_HUB_DATA_DIR`` and then ran ``alembic upgrade head`` must not
    migrate a different database from the one ``test-hub serve`` opens.
    """
    settings = config.attributes.get("settings")
    if settings is not None:
        url: str = settings.resolved_database_url
        return url

    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return str(override)

    from_ini = config.get_main_option("sqlalchemy.url")
    if from_ini:
        return from_ini

    from test_hub.config import get_settings

    return get_settings().resolved_database_url


def _configure(connection: Any) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — ``alembic upgrade head --sql``."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        # In-process call from Database.upgrade(): the caller owns the
        # transaction and the PRAGMAs. Do not open a second engine.
        _configure(connection)
        context.run_migrations()
        return

    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as new_connection:
        _configure(new_connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
