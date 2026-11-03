"""Index ``idempotency_keys.run_id`` for the retention sweep.

``idempotency_keys`` was the one table the prune deletes by ``run_id`` that
declared no indexes at all, so its only index was the ``key`` primary key and a
``DELETE ... WHERE run_id IN (...)`` full-scanned a table that grows with every
import — once per batch. Every other table the prune touches already leads an
index with ``run_id``.

This is the whole schema cost of retention (ADR-0002 D3): the missing foreign
keys on ``events``, ``artifacts`` and ``idempotency_keys`` are deliberately
*not* added here, because SQLite cannot add a constraint without rebuilding the
table, and rebuilding the event log to save six lines of ``DELETE`` is not a
trade worth making.

Revision ID: 0002_retention_indexes
Revises: 0001_initial
Create Date: 2026-09-09 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_retention_indexes"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_idempotency_keys_run_id", "idempotency_keys", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_run_id", table_name="idempotency_keys")
