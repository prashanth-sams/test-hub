"""Initial schema: workspaces, projects, runs, suites, tests, attempts, events, artifacts.

The whole shape of Test Hub in one migration. Two things in here are load-bearing
and easy to lose in a later hand-edit:

* Timestamps are ``String(32)`` because ``test_hub.storage.models.UtcDateTime``
  is a string-backed ``TypeDecorator``. Changing this to ``sa.DateTime`` would
  make SQLite hand back naive datetimes and every duration in the product wrong.
* ``UNIQUE(source, event_id)`` on ``events`` is the idempotency guarantee that
  the whole write path is built on (ADR-0001 D2). It is not an optimisation.

Every table carries ``workspace_id`` from the very first migration (ADR-0001 D9),
because adding a tenancy column to a populated events table later is the
migration nobody wants to write.

Revision ID: 0001_initial
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The storage form of test_hub.storage.models.UtcDateTime.
TIMESTAMP = sa.String(32)
UUID = sa.Uuid()
SLUG = sa.String(120)
SHORT = sa.String(512)


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", UUID, nullable=False),
        sa.Column("slug", SLUG, nullable=False),
        sa.Column("name", SHORT, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    op.create_table(
        "projects",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("key", SLUG, nullable=False),
        sa.Column("name", SHORT, nullable=False),
        sa.Column("description", SHORT, nullable=True),
        sa.Column("default_team", SLUG, nullable=True),
        sa.Column("repository_url", sa.String(2048), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_projects_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("workspace_id", "key", name="uq_projects_workspace_id_key"),
    )
    op.create_index("ix_projects_workspace_id", "projects", ["workspace_id"])

    op.create_table(
        "runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("project_key", SLUG, nullable=False),
        sa.Column("name", SHORT, nullable=True),
        sa.Column("team", SLUG, nullable=True),
        sa.Column("component", SLUG, nullable=True),
        sa.Column("test_type", sa.String(32), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("framework", SHORT, nullable=True),
        sa.Column("framework_version", SHORT, nullable=True),
        sa.Column("language", SHORT, nullable=True),
        sa.Column("language_version", SHORT, nullable=True),
        sa.Column("environment", SLUG, nullable=True),
        sa.Column("branch", SHORT, nullable=True),
        sa.Column("commit_sha", sa.String(64), nullable=True),
        sa.Column("commit_message", SHORT, nullable=True),
        sa.Column("pipeline_id", SHORT, nullable=True),
        sa.Column("pipeline_url", sa.String(2048), nullable=True),
        sa.Column("ci_provider", SHORT, nullable=True),
        sa.Column("hostname", SHORT, nullable=True),
        sa.Column("os", SHORT, nullable=True),
        sa.Column("executor", SHORT, nullable=True),
        sa.Column("started_at", TIMESTAMP, nullable=True),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("source_adapter", SHORT, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_runs_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
    )
    # The run list is always "this workspace, newest first". The index is
    # ascending and SQLite walks it backwards for the DESC order; an index
    # declared DESC would buy nothing and would make `alembic check` report
    # permanent drift, because reflection cannot see a column's sort order.
    op.create_index("ix_runs_workspace_id_started_at", "runs", ["workspace_id", "started_at"])
    op.create_index("ix_runs_project_key", "runs", ["project_key"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_team", "runs", ["team"])
    op.create_index("ix_runs_environment", "runs", ["environment"])
    op.create_index("ix_runs_created_at", "runs", ["created_at"])

    op.create_table(
        "suites",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("suite_id", UUID, nullable=False),
        sa.Column("name", SHORT, nullable=False),
        sa.Column("path", SHORT, nullable=False),
        sa.Column("parent_id", UUID, nullable=True),
        sa.Column("started_at", TIMESTAMP, nullable=True),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_suites_run_id_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_suites"),
        sa.UniqueConstraint("run_id", "suite_id", name="uq_suites_run_id_suite_id"),
    )
    op.create_index("ix_suites_run_id", "suites", ["run_id"])

    op.create_table(
        "test_cases",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("test_id", UUID, nullable=False),
        sa.Column("suite_id", UUID, nullable=False),
        sa.Column("name", SHORT, nullable=False),
        sa.Column("class_name", SHORT, nullable=True),
        sa.Column("suite_path", SHORT, nullable=False),
        sa.Column("params", SHORT, nullable=True),
        sa.Column("full_name", SHORT, nullable=True),
        sa.Column("file", SHORT, nullable=True),
        sa.Column("owner", SHORT, nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("markers", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("flaky", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_test_cases_run_id_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_test_cases"),
        sa.UniqueConstraint("run_id", "test_id", name="uq_test_cases_run_id_test_id"),
    )
    op.create_index("ix_test_cases_test_id", "test_cases", ["test_id"])
    op.create_index("ix_test_cases_run_id", "test_cases", ["run_id"])
    op.create_index("ix_test_cases_status", "test_cases", ["status"])

    op.create_table(
        "attempts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("test_id", UUID, nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("original_status", SHORT, nullable=True),
        sa.Column("started_at", TIMESTAMP, nullable=True),
        sa.Column("finished_at", TIMESTAMP, nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("failure", sa.JSON(), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("logs", sa.Text(), nullable=True),
        sa.Column("skip_reason", SHORT, nullable=True),
        sa.Column("worker", SHORT, nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_attempts_run_id_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_attempts"),
        sa.UniqueConstraint(
            "run_id",
            "test_id",
            "attempt_number",
            name="uq_attempts_run_id_test_id_attempt_number",
        ),
    )
    op.create_index("ix_attempts_run_id_test_id", "attempts", ["run_id", "test_id"])
    op.create_index("ix_attempts_test_id", "attempts", ["test_id"])

    op.create_table(
        "events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("source", SHORT, nullable=False),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("time", TIMESTAMP, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("received_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        # Reposting an event collides here and is counted as ignored. This is
        # the idempotency guarantee of the whole write path.
        sa.UniqueConstraint("source", "event_id", name="uq_events_source_event_id"),
    )
    op.create_index("ix_events_run_id_sequence", "events", ["run_id", "sequence"])
    op.create_index("ix_events_workspace_id_received_at", "events", ["workspace_id", "received_at"])

    op.create_table(
        "artifacts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("test_id", UUID, nullable=True),
        sa.Column("attempt_id", UUID, nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", SHORT, nullable=False),
        sa.Column("content_type", SHORT, nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("url", SHORT, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
    )
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])
    op.create_index("ix_artifacts_sha256", "artifacts", ["sha256"])
    op.create_index("ix_artifacts_test_id", "artifacts", ["test_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("key", SHORT, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_idempotency_keys"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_index("ix_artifacts_test_id", table_name="artifacts")
    op.drop_index("ix_artifacts_sha256", table_name="artifacts")
    op.drop_index("ix_artifacts_run_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_events_workspace_id_received_at", table_name="events")
    op.drop_index("ix_events_run_id_sequence", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_attempts_test_id", table_name="attempts")
    op.drop_index("ix_attempts_run_id_test_id", table_name="attempts")
    op.drop_table("attempts")
    op.drop_index("ix_test_cases_status", table_name="test_cases")
    op.drop_index("ix_test_cases_run_id", table_name="test_cases")
    op.drop_index("ix_test_cases_test_id", table_name="test_cases")
    op.drop_table("test_cases")
    op.drop_index("ix_suites_run_id", table_name="suites")
    op.drop_table("suites")
    op.drop_index("ix_runs_created_at", table_name="runs")
    op.drop_index("ix_runs_environment", table_name="runs")
    op.drop_index("ix_runs_team", table_name="runs")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_index("ix_runs_project_key", table_name="runs")
    op.drop_index("ix_runs_workspace_id_started_at", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_projects_workspace_id", table_name="projects")
    op.drop_table("projects")
    op.drop_table("workspaces")
