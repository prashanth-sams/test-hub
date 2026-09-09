"""Database migrations, and the storage guarantees underneath them."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from test_hub.config import Settings
from test_hub.storage import create_database


@pytest.fixture
def database(tmp_path: Path) -> Any:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    return create_database(settings)


def table_names(database: Any) -> set[str]:
    with database.engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def test_upgrade_creates_the_whole_schema(database: Any) -> None:
    database.upgrade()
    tables = table_names(database)
    for expected in (
        "workspaces",
        "projects",
        "runs",
        "suites",
        "test_cases",
        "attempts",
        "events",
        "artifacts",
    ):
        assert expected in tables, f"{expected} was not created"


def test_upgrade_stamps_a_revision(database: Any) -> None:
    database.upgrade()
    assert database.current_revision()


def test_upgrade_is_idempotent(database: Any) -> None:
    """`serve` runs it on every start; a second run must be a no-op."""
    database.upgrade()
    revision = database.current_revision()
    database.upgrade()
    assert database.current_revision() == revision


def test_the_migration_and_the_orm_agree(database: Any) -> None:
    """A model added without a migration is a bug that only appears in production.

    Comparing the migrated schema against ``metadata.create_all`` catches it in
    CI instead.
    """
    from test_hub.storage.models import Base

    database.upgrade()
    migrated = table_names(database)

    declared = set(Base.metadata.tables) - {"alembic_version"}
    missing = declared - migrated
    assert not missing, f"these tables exist in the ORM but not in any migration: {missing}"


def test_columns_match_between_migration_and_orm(database: Any) -> None:
    from test_hub.storage.models import Base

    database.upgrade()
    with database.engine.connect() as connection:
        inspector = sa.inspect(connection)
        for name, table in Base.metadata.tables.items():
            if name == "alembic_version":
                continue
            actual = {column["name"] for column in inspector.get_columns(name)}
            declared = {column.name for column in table.columns}
            assert declared <= actual, f"{name} is missing {declared - actual}"


def test_wal_mode_and_foreign_keys_are_enabled(database: Any) -> None:
    """WAL is what lets a reader and a writer coexist; FKs are what keep it coherent."""
    database.upgrade()
    with database.engine.connect() as connection:
        journal = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert str(journal).lower() == "wal"
    assert foreign_keys == 1


def test_timestamps_round_trip_as_utc_aware(database: Any) -> None:
    """SQLite drops tzinfo. Losing it silently makes every duration wrong."""
    from datetime import UTC, datetime

    from test_hub.storage.models import WorkspaceRow

    database.upgrade()
    moment = datetime(2026, 3, 11, 8, 41, 2, tzinfo=UTC)
    with database.session() as session:
        session.add(WorkspaceRow(id=uuid.uuid4(), slug="tz-check", name="tz", created_at=moment))
        session.commit()
    with database.session() as session:
        row = session.execute(
            sa.select(WorkspaceRow).where(WorkspaceRow.slug == "tz-check")
        ).scalar_one()
        assert row.created_at.tzinfo is not None
        assert row.created_at == moment


def test_the_event_uniqueness_constraint_exists(database: Any) -> None:
    """The idempotency guarantee is a database constraint, not application logic."""
    database.upgrade()
    with database.engine.connect() as connection:
        inspector = sa.inspect(connection)
        constraints = [
            set(unique["column_names"]) for unique in inspector.get_unique_constraints("events")
        ]
        indexes = [set(index["column_names"]) for index in inspector.get_indexes("events")]
    assert {"source", "event_id"} in constraints or {"source", "event_id"} in indexes


def test_downgrade_removes_the_schema(database: Any) -> None:
    """An irreversible migration is one nobody can safely deploy."""
    from alembic import command
    from alembic.config import Config

    from test_hub.storage import migrations_path

    database.upgrade()
    config = Config()
    config.set_main_option("script_location", str(migrations_path()))
    config.set_main_option("sqlalchemy.url", database.engine.url.render_as_string(False))
    command.downgrade(config, "base")

    remaining = table_names(database) - {"alembic_version"}
    assert remaining == set(), f"downgrade left tables behind: {remaining}"


def test_create_all_is_equivalent_for_tests(tmp_path: Path) -> None:
    """`create_all` is the fast path fixtures use; it must produce the same tables."""
    settings = Settings(data_dir=tmp_path / "createall")
    settings.ensure_directories()
    database = create_database(settings)
    database.create_all()
    assert "runs" in table_names(database)
    assert "events" in table_names(database)
