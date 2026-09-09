"""The private SQLAlchemy schema. Nothing in here ever crosses a boundary.

ADR-0001 D3 draws a hard line between the wire contract
(:mod:`test_hub.domain.models`, Pydantic) and the rows on disk (this module).
The repositories translate; no ORM object is ever serialized, returned from an
API handler or embedded in a snapshot. That is what lets the table layout change
— a new index, a split table, PostgreSQL — without a schema-version bump.

Three decisions in here are worth knowing before you touch anything:

**Time is a string.** SQLite has no datetime type; the driver hands back naive
values and every duration computed from them is silently wrong by whatever the
server's offset happens to be. :class:`UtcDateTime` therefore stores a
fixed-width ISO-8601 UTC string and always returns a timezone-aware
:class:`~datetime.datetime`. Fixed width matters twice over: it makes the
lexicographic order of the stored text identical to chronological order, so
``ORDER BY started_at DESC`` is correct without a function call and can use a
plain index.

**RunContext is flattened.** The canonical model nests framework, branch,
environment and the rest inside ``TestRun.context``. Stored as a JSON blob those
facets could not be indexed, and the dashboard filters on exactly them. They are
therefore columns, and :mod:`test_hub.storage.repositories` reassembles a
``RunContext`` on the way out.

**Every table carries ``workspace_id``** (ADR-0001 D9), including the ones the
open-source edition will only ever fill with one value.

The coercion helpers at the bottom exist because event payloads are typed with
bare ``str`` while the canonical models constrain the same fields with length
and pattern rules. A publisher that sends a 400 KiB stack trace or a project key
with a slash in it must not produce a row that can never be read back out
again, so everything is squeezed to fit *on the way in*.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, MetaData, String, Text, UniqueConstraint
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

__all__ = [
    "LONG_TEXT_MAX",
    "NAMING_CONVENTION",
    "SHORT_TEXT_MAX",
    "SLUG_MAX",
    "ArtifactRow",
    "AttemptRow",
    "Base",
    "EventRow",
    "IdempotencyKeyRow",
    "ProjectRow",
    "RunRow",
    "SuiteRow",
    "TestCaseRow",
    "UtcDateTime",
    "WorkspaceRow",
    "coerce_slug",
    "coerce_slugs",
    "long_text",
    "short_text",
    "short_texts",
]

#: Mirrors the constraints on ``test_hub.domain.models``' ``Slug``,
#: ``ShortText`` and ``LongText`` aliases. Duplicated as plain integers rather
#: than introspected out of the Pydantic models, because a value that no longer
#: fits should fail loudly in a review diff, not silently at 3am in a repository
#: that cannot rebuild a run.
SLUG_MAX = 120
SHORT_TEXT_MAX = 512
LONG_TEXT_MAX = 64_000

#: Deterministic constraint names. SQLite cannot ``ALTER`` a constraint it cannot
#: name, and Alembic's ``render_as_batch`` rebuilds a table by copying its
#: constraints — anonymous ones do not survive the trip.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: The exact rendering used for every stored timestamp: fixed width, always UTC,
#: always six fractional digits. Changing it invalidates the ordering guarantee
#: described in the module docstring.
_ISO_UTC = "%Y-%m-%dT%H:%M:%S.%f+00:00"

_SLUG_ALLOWED = re.compile(r"[^A-Za-z0-9._\- ]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is timezone-aware UTC on both sides of the wire.

    Backed by a string rather than SQLAlchemy's ``DateTime`` because pysqlite
    round-trips ``DateTime`` through a naive value: it writes the local wall
    clock and reads back a ``datetime`` with ``tzinfo is None``. Subtracting two
    of those gives a duration that is wrong by the offset difference, and
    handing one to a Pydantic ``AwareDatetime`` field is a validation error at
    read time. Neither failure is loud, which is exactly why this type exists.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return aware.astimezone(UTC).strftime(_ISO_UTC)

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base carrying the naming convention for the whole schema."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ------------------------------------------------------------------ tenancy


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    slug: Mapped[str] = mapped_column(String(SLUG_MAX), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ProjectRow(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_projects_workspace_id_key"),
        Index("ix_projects_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(SLUG_MAX), nullable=False)
    name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), nullable=False)
    description: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    default_team: Mapped[str | None] = mapped_column(String(SLUG_MAX))
    repository_url: Mapped[str | None] = mapped_column(String(2048))
    tags: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# ---------------------------------------------------------------------- runs


class RunRow(Base):
    """One run, with :class:`~test_hub.domain.models.RunContext` flattened out.

    ``counts`` is stored as JSON even though it is derived, because the run list
    is the hottest read in the product and refolding a thousand runs' attempts to
    draw one page of a table is not a trade worth making. It is rewritten from
    the attempt rows after every batch, so it is a cache with exactly one writer.
    """

    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_workspace_id_started_at", "workspace_id", "started_at"),
        Index("ix_runs_project_key", "project_key"),
        Index("ix_runs_status", "status"),
        Index("ix_runs_team", "team"),
        Index("ix_runs_environment", "environment"),
        Index("ix_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_key: Mapped[str] = mapped_column(String(SLUG_MAX), nullable=False)
    name: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    team: Mapped[str | None] = mapped_column(String(SLUG_MAX))
    component: Mapped[str | None] = mapped_column(String(SLUG_MAX))
    test_type: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")

    # --- RunContext, flattened so the dashboard's facets can be indexed
    framework: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    framework_version: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    language: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    language_version: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    environment: Mapped[str | None] = mapped_column(String(SLUG_MAX))
    branch: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    commit_message: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    pipeline_id: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    pipeline_url: Mapped[str | None] = mapped_column(String(2048))
    ci_provider: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    hostname: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    os: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    executor: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    counts: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    tags: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, nullable=False, default=list)
    source_adapter: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class SuiteRow(Base):
    """A suite *within one run*.

    ``id`` is the row's own key; ``suite_id`` is the suite's stable identity and
    is what the domain model calls ``id``. Two runs of the same suite are two
    rows sharing one ``suite_id``, which is what makes per-suite history possible.
    """

    __tablename__ = "suites"
    __table_args__ = (
        UniqueConstraint("run_id", "suite_id", name="uq_suites_run_id_suite_id"),
        Index("ix_suites_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    suite_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), nullable=False)
    path: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid())
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)


class TestCaseRow(Base):
    """A test's identity within one run, plus the folded verdict of its attempts.

    ``status``, ``flaky``, ``attempt_count`` and ``duration_ms`` are all derived
    from :class:`AttemptRow` and rewritten by the fold. They are stored so that
    "which tests failed in this run" is one indexed query rather than a join and
    a group-by over every attempt in the database.
    """

    __tablename__ = "test_cases"
    __table_args__ = (
        UniqueConstraint("run_id", "test_id", name="uq_test_cases_run_id_test_id"),
        Index("ix_test_cases_test_id", "test_id"),
        Index("ix_test_cases_run_id", "run_id"),
        Index("ix_test_cases_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    test_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    suite_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), nullable=False)
    class_name: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    suite_path: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), nullable=False)
    params: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    full_name: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    file: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    owner: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    tags: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    markers: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    flaky: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)


class AttemptRow(Base):
    """One execution of one test. Append-only by construction.

    A retry inserts a row with the next ``attempt_number``; nothing ever updates
    an earlier attempt's outcome. The unique constraint is what enforces that a
    replayed event corrects a record rather than inventing a second attempt 1.
    """

    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "test_id", "attempt_number", name="uq_attempts_run_id_test_id_attempt_number"
        ),
        Index("ix_attempts_run_id_test_id", "run_id", "test_id"),
        Index("ix_attempts_test_id", "test_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    test_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    attempt_number: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    original_status: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    failure: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
    stdout: Mapped[str | None] = mapped_column(Text)
    stderr: Mapped[str | None] = mapped_column(Text)
    logs: Mapped[str | None] = mapped_column(Text)
    skip_reason: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    worker: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)


# -------------------------------------------------------------------- events


class EventRow(Base):
    """The append-only log every row above was folded from.

    ``UNIQUE(source, event_id)`` is the whole idempotency story: reposting a
    batch collides here and is counted as ignored. ``id`` is the row's key and
    ``event_id`` is the CloudEvents ``id``, which is only unique *within* a
    source and therefore cannot be the primary key on its own.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source", "event_id", name="uq_events_source_event_id"),
        Index("ix_events_run_id_sequence", "run_id", "sequence"),
        Index("ix_events_workspace_id_received_at", "workspace_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    source: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    time: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    data: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_run_id", "run_id"),
        Index("ix_artifacts_sha256", "sha256"),
        Index("ix_artifacts_test_id", "test_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    test_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid())
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid())
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    name: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(SHORT_TEXT_MAX), nullable=False, default="application/octet-stream"
    )
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)


class IdempotencyKeyRow(Base):
    """A caller-supplied key and the answer it was given the first time.

    Replaying a whole import — the CI job retried, the network dropped the
    response — must return the original answer rather than doing the work twice
    or, worse, returning "0 applied" and letting the caller conclude that the
    import failed.
    """

    __tablename__ = "idempotency_keys"
    # `run_id` is indexed for the retention sweep and nothing else: it is the
    # only table the prune deletes by run that has no other reason to carry the
    # index, and without it every batch full-scans a table that grows with
    # every import (ADR-0002, schema change).
    __table_args__ = (Index("ix_idempotency_keys_run_id", "run_id"),)

    key: Mapped[str] = mapped_column(String(SHORT_TEXT_MAX), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# ------------------------------------------------------- inbound coercion


def _strip_control(value: str) -> str:
    """Remove characters that would corrupt a log line, a CSV or an SVG label."""
    return _CONTROL.sub("", value)


def short_text(value: str | None, limit: int = SHORT_TEXT_MAX) -> str | None:
    """Fit a free-text value into a ``ShortText`` column, or ``None``."""
    if value is None:
        return None
    cleaned = _strip_control(value).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def short_texts(values: object, *, limit: int = 64) -> list[str]:
    """Fit a list of free-text values into a ``list[ShortText]`` field."""
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for value in values:
        text = short_text(str(value))
        if text is not None:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def long_text(value: str | None, max_bytes: int) -> tuple[str | None, bool]:
    """Fit a stack trace or a log into a ``LongText`` field.

    Returns the value and whether anything was cut. Two ceilings apply: the
    operator's ``max_text_field_bytes`` and the hard ``LongText`` limit of the
    wire contract. The smaller wins — storing more than the canonical model can
    carry produces a row that raises a validation error every time anyone tries
    to read it.
    """
    if value is None:
        return None, False
    truncated = False
    raw = value.encode("utf-8")
    if len(raw) > max_bytes:
        value = raw[:max_bytes].decode("utf-8", "ignore")
        truncated = True
    if len(value) > LONG_TEXT_MAX:
        value = value[:LONG_TEXT_MAX]
        truncated = True
    return value, truncated


def coerce_slug(value: str | None, *, limit: int = SLUG_MAX) -> str | None:
    """Squeeze a publisher's free-text key into the canonical ``Slug`` shape.

    Event payloads type project keys, teams and environments as bare ``str``,
    but the models they are read back into constrain them to
    ``^[a-zA-Z0-9][a-zA-Z0-9._\\- ]*$``. Rejecting the write would throw away a
    perfectly good test run over a slash in a team name, so the value is
    repaired instead: disallowed characters become ``-`` and a leading
    non-alphanumeric is dropped.
    """
    if value is None:
        return None
    cleaned = _SLUG_ALLOWED.sub("-", _strip_control(value).strip())
    cleaned = cleaned.lstrip("-._ ")
    if not cleaned:
        return None
    return cleaned[:limit]


def coerce_slugs(values: object, *, limit: int = 64) -> list[str]:
    """Coerce a list of tags, dropping anything that cannot be repaired."""
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        slug = coerce_slug(str(value))
        if slug is None or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
        if len(out) >= limit:
            break
    return out
