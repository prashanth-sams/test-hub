"""Widen every publisher-supplied integer to 64 bits on PostgreSQL.

``sequence``, ``duration_ms``, ``size_bytes``, ``attempt_number`` and
``attempt_count`` are validated ``ge=0`` and nothing else, so they are only as
wide as the column. ``0001_initial`` declared them ``sa.Integer()``, which is
64-bit on SQLite and 32-bit on PostgreSQL: a publisher numbering its events by
epoch milliseconds, or a JUnit file claiming a test ran for 25 days, would be
accepted by one backend and fail the whole import on the other.

A no-op on SQLite, deliberately. Its ``INTEGER`` is already 64-bit, and the
batch-mode ``ALTER`` Alembic would emit there rebuilds each table by copying
it — the event log included — to change nothing at all.

Revision ID: 0003_postgresql_bigint
Revises: 0002_retention_indexes
Create Date: 2026-11-03 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_postgresql_bigint"
down_revision: str | None = "0002_retention_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, column, nullable) — every Integer column in the schema.
COUNTERS: tuple[tuple[str, str, bool], ...] = (
    ("runs", "duration_ms", True),
    ("suites", "duration_ms", True),
    ("test_cases", "duration_ms", True),
    ("test_cases", "attempt_count", False),
    ("attempts", "attempt_number", False),
    ("attempts", "duration_ms", True),
    ("events", "sequence", False),
    ("artifacts", "size_bytes", False),
)


def _is_postgresql() -> bool:
    # get_context() rather than get_bind(): it also works for `--sql`, where
    # there is no connection to ask.
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgresql():
        return
    for table, column, nullable in COUNTERS:
        op.alter_column(
            table,
            column,
            type_=sa.BigInteger(),
            existing_type=sa.Integer(),
            existing_nullable=nullable,
        )


def downgrade() -> None:
    if not _is_postgresql():
        return
    for table, column, nullable in COUNTERS:
        op.alter_column(
            table,
            column,
            type_=sa.Integer(),
            existing_type=sa.BigInteger(),
            existing_nullable=nullable,
        )
