"""Rows in, canonical models out — and the fold that is the only write path.

Two responsibilities live here, and they are deliberately in one module because
they are two directions of the same translation.

**Reading.** Every query returns Pydantic models from
:mod:`test_hub.domain.models`. No ORM object escapes (ADR-0001 D3). The
translation is not mechanical: ``RunContext`` is reassembled from flat columns,
counts are rebuilt from stored JSON that deliberately excludes the computed
fields, and a suite's counts are folded from its tests at read time rather than
stored, because a suite has no independent existence to keep in sync.

**Writing.** :meth:`SqlEventRepository.apply_events` folds a batch of
:class:`~test_hub.domain.events.DomainEvent` into rows. It is the *only* way
state changes in Test Hub, which is what makes the two hard properties true:

* *Idempotent* — ``UNIQUE(source, event_id)`` decides, and a caller-supplied
  idempotency key short-circuits an entire replayed batch back to the answer it
  got the first time.
* *Derived* — counts, statuses, flakiness and durations are recomputed from the
  attempt rows after every batch. A publisher's own totals are read as a hint at
  most. They drift the instant anything is retried or uploaded in shards, and a
  dashboard that disagrees with its own detail page is worse than a slow one.

The one trap worth stating plainly: **a retry is a new attempt row**. Nothing in
here ever overwrites an attempt's outcome with a later one. Attempt 1 failing
and attempt 2 passing is the entire observable definition of flaky, and an
UPDATE would erase it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import HttpUrl, TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_hub.config import Settings
from test_hub.domain.events import (
    ArtifactPublishedData,
    DomainEvent,
    EventType,
    RunFinishedData,
    RunQueuedData,
    RunStartedData,
    SuiteFinishedData,
    SuiteStartedData,
    TestFinishedData,
    TestStartedData,
)
from test_hub.domain.identity import test_identity
from test_hub.domain.identity import workspace_id as derive_workspace_id
from test_hub.domain.metrics import (
    effective_status,
    flake_rate,
    is_flaky_within_run,
    percentile,
    run_duration_ms,
)
from test_hub.domain.models import (
    Artifact,
    ArtifactKind,
    FailureDetail,
    OverviewSnapshot,
    ParserWarning,
    Project,
    ProjectHealth,
    RunComparison,
    RunContext,
    RunCounts,
    RunDetail,
    RunSummary,
    TestAttempt,
    TestCase,
    TestDetail,
    TestHistoryPoint,
    TestRun,
    TestSuite,
    TestType,
    TrendPoint,
    TriggerType,
    Workspace,
    utcnow,
)
from test_hub.domain.status import (
    RUN_TERMINAL_STATUSES,
    RunStatus,
    TestStatus,
    is_failureish,
    is_successful,
)
from test_hub.errors import ValidationError
from test_hub.logging import get_redactor
from test_hub.storage.base import RunFilter, WriteResult
from test_hub.storage.locks import WriteLocks
from test_hub.storage.models import (
    ArtifactRow,
    AttemptRow,
    EventRow,
    IdempotencyKeyRow,
    ProjectRow,
    RunRow,
    SuiteRow,
    TestCaseRow,
    WorkspaceRow,
    coerce_slug,
    coerce_slugs,
    long_text,
    short_text,
    short_texts,
)

__all__ = ["SqlEventRepository", "SqlRunRepository", "artifact_to_model"]

#: Placeholder project key for a run whose first event was not ``run.queued``.
#: Treated as "still empty" so a late-arriving queued event can correct it.
UNKNOWN_PROJECT = "unknown"

#: Statuses that mean "still expected to produce events".
_LIVE_STATUSES = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)

_HTTP_URL = TypeAdapter(HttpUrl)
_LIKE_ESCAPE = "\\"


# ------------------------------------------------------------- conversions


def _http_url(value: str | None) -> str | None:
    """Keep a URL only if it will still validate on the way back out.

    ``RunContext.pipeline_url`` is an ``HttpUrl``; storing a publisher's
    ``jenkins-internal/job/7`` would make every later read of that run raise.
    """
    if not value:
        return None
    try:
        _HTTP_URL.validate_python(value)
    except PydanticValidationError:
        return None
    return value[:2048]


def _counts_to_json(counts: RunCounts) -> dict[str, int]:
    """Only the stored fields — the computed ones would be rejected on read."""
    return {name: int(getattr(counts, name)) for name in RunCounts.model_fields}


def _counts_from_json(data: Any) -> RunCounts:
    if not isinstance(data, dict):
        return RunCounts()
    known = {
        name: int(value)
        for name, value in data.items()
        if name in RunCounts.model_fields and isinstance(value, (int, float))
    }
    return RunCounts(**known)


def _test_status(raw: str) -> TestStatus:
    try:
        return TestStatus(raw)
    except ValueError:
        return TestStatus.UNKNOWN


def _run_status(raw: str) -> RunStatus:
    try:
        return RunStatus(raw)
    except ValueError:
        return RunStatus.UNKNOWN


def _test_type(raw: str) -> TestType:
    try:
        return TestType(raw)
    except ValueError:
        return TestType.OTHER


def _trigger(raw: str) -> TriggerType:
    try:
        return TriggerType(raw)
    except ValueError:
        return TriggerType.UNKNOWN


def _artifact_kind(raw: str) -> ArtifactKind:
    try:
        return ArtifactKind(raw)
    except ValueError:
        return ArtifactKind.OTHER


def _context_of(row: RunRow) -> RunContext:
    return RunContext(
        framework=row.framework,
        framework_version=row.framework_version,
        language=row.language,
        language_version=row.language_version,
        environment=row.environment,
        branch=row.branch,
        commit_sha=row.commit_sha,
        commit_message=row.commit_message,
        pipeline_id=row.pipeline_id,
        pipeline_url=_HTTP_URL.validate_python(row.pipeline_url) if row.pipeline_url else None,
        ci_provider=row.ci_provider,
        hostname=row.hostname,
        os=row.os,
        executor=row.executor,
    )


def _warnings_of(row: RunRow) -> list[ParserWarning]:
    out: list[ParserWarning] = []
    for raw in row.warnings or []:
        try:
            out.append(ParserWarning.model_validate(raw))
        except PydanticValidationError:
            continue
    return out


def _run_to_model(row: RunRow) -> TestRun:
    return TestRun(
        id=row.id,
        workspace_id=row.workspace_id,
        project_key=row.project_key,
        name=row.name,
        team=row.team,
        component=row.component,
        test_type=_test_type(row.test_type),
        trigger=_trigger(row.trigger),
        status=_run_status(row.status),
        context=_context_of(row),
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        counts=_counts_from_json(row.counts),
        tags=list(row.tags or []),
        metadata=dict(row.meta or {}),
        warnings=_warnings_of(row),
        source_adapter=row.source_adapter,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _run_to_summary(row: RunRow) -> RunSummary:
    status = _run_status(row.status)
    return RunSummary(
        id=row.id,
        project_key=row.project_key,
        name=row.name,
        team=row.team,
        component=row.component,
        test_type=_test_type(row.test_type),
        trigger=_trigger(row.trigger),
        status=status,
        environment=row.environment,
        branch=row.branch,
        commit_sha=row.commit_sha,
        framework=row.framework,
        language=row.language,
        pipeline_url=_HTTP_URL.validate_python(row.pipeline_url) if row.pipeline_url else None,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        counts=_counts_from_json(row.counts),
        tags=list(row.tags or []),
        is_live=status in (RunStatus.QUEUED, RunStatus.RUNNING),
    )


def _attempt_to_model(row: AttemptRow, artifact_ids: Sequence[uuid.UUID]) -> TestAttempt:
    failure: FailureDetail | None = None
    if row.failure:
        try:
            failure = FailureDetail.model_validate(row.failure)
        except PydanticValidationError:
            failure = None
    return TestAttempt(
        id=row.id,
        test_id=row.test_id,
        run_id=row.run_id,
        attempt_number=row.attempt_number,
        status=_test_status(row.status),
        original_status=row.original_status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        failure=failure,
        stdout=row.stdout,
        stderr=row.stderr,
        logs=row.logs,
        skip_reason=row.skip_reason,
        worker=row.worker,
        artifact_ids=list(artifact_ids),
        metadata=dict(row.meta or {}),
    )


def _case_to_model(row: TestCaseRow, attempts: Sequence[TestAttempt]) -> TestCase:
    return TestCase(
        id=row.test_id,
        run_id=row.run_id,
        suite_id=row.suite_id,
        name=row.name,
        class_name=row.class_name,
        suite_path=row.suite_path,
        params=row.params,
        full_name=row.full_name,
        file=row.file,
        owner=row.owner,
        tags=list(row.tags or []),
        markers=list(row.markers or []),
        attempts=list(attempts),
        metadata=dict(row.meta or {}),
    )


def artifact_to_model(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        run_id=row.run_id,
        test_id=row.test_id,
        attempt_id=row.attempt_id,
        kind=_artifact_kind(row.kind),
        name=row.name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        url=row.url,
        created_at=row.created_at,
        metadata=dict(row.meta or {}),
    )


def _escape_like(term: str) -> str:
    """Escape LIKE's wildcards so ``%`` and ``_`` in a value match themselves."""
    return term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")


def _like(term: str) -> str:
    """Escape a user search term so ``%`` in a branch name is not a wildcard."""
    return f"%{_escape_like(term)}%"


def _sort_columns(dialect: str) -> list[Any]:
    """Newest first: ``started_at`` descending, nulls last, then ``created_at``.

    On SQLite, no ``NULLS LAST`` clause and no ``CASE`` wrapper: SQLite sorts
    NULL below every value, so a plain ``DESC`` already puts the runs that never
    started at the bottom — and unlike a ``CASE`` expression it can be answered
    by walking ``ix_runs_workspace_id_started_at`` backwards instead of building
    a temporary b-tree for every page of the run list.

    PostgreSQL sorts NULL *above* every value in a descending sort, so the same
    plain ``DESC`` would put every queued run at the top of every page. It has
    to be said out loud there.
    """
    if dialect == "sqlite":
        return [RunRow.started_at.desc(), RunRow.created_at.desc()]
    return [RunRow.started_at.desc().nulls_last(), RunRow.created_at.desc()]


def _insert_if_absent(session: Session, table: Any, values: dict[str, Any]) -> None:
    """``INSERT … ON CONFLICT DO NOTHING`` in whichever dialect ``session`` speaks.

    For the rows every writer may try to create at once — the workspace, a
    project the first time two of its pipelines report together. On SQLite the
    write lock means nothing can race the select-then-insert in front of this,
    so it never conflicts. On PostgreSQL a concurrent first insert can, and
    ``DO NOTHING`` turns the loser's unique violation (and its aborted
    transaction) into a no-op; the caller then reads back whichever row won.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        session.execute(pg_insert(table).values(**values).on_conflict_do_nothing())
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        session.execute(sqlite_insert(table).values(**values).on_conflict_do_nothing())


# ------------------------------------------------------------- repository


class SqlRunRepository:
    """Every read the product performs, plus the two writes events cannot carry."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._dialect = session.get_bind().dialect.name

    # ----------------------------------------------------------- tenancy

    def _workspace_by_slug(self, slug: str) -> WorkspaceRow | None:
        return self._session.execute(
            select(WorkspaceRow).where(WorkspaceRow.slug == slug)
        ).scalar_one_or_none()

    def ensure_workspace(self, slug: str, name: str) -> Workspace:
        clean_slug = coerce_slug(slug) or "default"
        row = self._workspace_by_slug(clean_slug)
        if row is None:
            # Every API process and every CLI command calls this on start, so on
            # PostgreSQL two of them creating a fresh database is a real race.
            _insert_if_absent(
                self._session,
                WorkspaceRow.__table__,
                {
                    "id": derive_workspace_id(clean_slug),
                    "slug": clean_slug,
                    "name": short_text(name) or clean_slug,
                    "created_at": utcnow(),
                },
            )
            row = self._workspace_by_slug(clean_slug)
            if row is None:
                raise RuntimeError(f"workspace {clean_slug!r} could not be created or found")
        return Workspace(id=row.id, slug=row.slug, name=row.name, created_at=row.created_at)

    def get_workspace(self, workspace_id: uuid.UUID) -> Workspace | None:
        row = self._session.get(WorkspaceRow, workspace_id)
        if row is None:
            return None
        return Workspace(id=row.id, slug=row.slug, name=row.name, created_at=row.created_at)

    def ensure_project(self, workspace_id: uuid.UUID, key: str, **fields: Any) -> Project:
        """Create the project, or fill in blanks on the one that is already there.

        Fill-in rather than overwrite: an import names the project after its key
        because that is all a JUnit file knows, and it must not clobber a
        description someone typed in.
        """
        clean_key = coerce_slug(key) or UNKNOWN_PROJECT
        row = self._project_by_key(workspace_id, clean_key)
        now = utcnow()
        if row is None:
            # The first two pipelines of a new project usually report together.
            # On PostgreSQL both get here; one insert wins, the other becomes a
            # no-op and falls through to filling in blanks on the winner's row,
            # exactly as if it had arrived second.
            _insert_if_absent(
                self._session,
                ProjectRow.__table__,
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "key": clean_key,
                    "name": short_text(str(fields.get("name") or "")) or clean_key,
                    "description": short_text(fields.get("description")),
                    "default_team": coerce_slug(fields.get("default_team")),
                    "repository_url": _http_url(fields.get("repository_url")),
                    "tags": coerce_slugs(fields.get("tags")),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = self._project_by_key(workspace_id, clean_key)
            if row is None:
                raise RuntimeError(f"project {clean_key!r} could not be created or found")

        # A row this call just inserted already holds every field, so this only
        # ever changes a row that somebody else created first.
        changed = False
        if not row.description and fields.get("description"):
            row.description = short_text(fields["description"])
            changed = True
        if not row.default_team and fields.get("default_team"):
            row.default_team = coerce_slug(fields["default_team"])
            changed = True
        if not row.repository_url and fields.get("repository_url"):
            row.repository_url = _http_url(fields["repository_url"])
            changed = True
        if not row.tags and fields.get("tags"):
            row.tags = coerce_slugs(fields["tags"])
            changed = True
        if changed:
            row.updated_at = now
        return self._project_to_model(row)

    def _project_by_key(self, workspace_id: uuid.UUID, key: str) -> ProjectRow | None:
        return self._session.execute(
            select(ProjectRow).where(ProjectRow.workspace_id == workspace_id, ProjectRow.key == key)
        ).scalar_one_or_none()

    @staticmethod
    def _project_to_model(row: ProjectRow) -> Project:
        return Project(
            id=row.id,
            workspace_id=row.workspace_id,
            key=row.key,
            name=row.name,
            description=row.description,
            default_team=row.default_team,
            repository_url=(
                _HTTP_URL.validate_python(row.repository_url) if row.repository_url else None
            ),
            tags=list(row.tags or []),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def list_projects(self, workspace_id: uuid.UUID) -> list[Project]:
        rows = (
            self._session.execute(
                select(ProjectRow)
                .where(ProjectRow.workspace_id == workspace_id)
                .order_by(ProjectRow.key.asc())
            )
            .scalars()
            .all()
        )
        return [self._project_to_model(row) for row in rows]

    # -------------------------------------------------------------- runs

    def get_run(self, run_id: uuid.UUID) -> TestRun | None:
        row = self._session.get(RunRow, run_id)
        return _run_to_model(row) if row is not None else None

    def get_run_detail(self, run_id: uuid.UUID, *, comparison: bool = True) -> RunDetail | None:
        run_row = self._session.get(RunRow, run_id)
        if run_row is None:
            return None

        case_rows = (
            self._session.execute(select(TestCaseRow).where(TestCaseRow.run_id == run_id))
            .scalars()
            .all()
        )
        attempt_rows = (
            self._session.execute(
                select(AttemptRow)
                .where(AttemptRow.run_id == run_id)
                .order_by(AttemptRow.test_id.asc(), AttemptRow.attempt_number.asc())
            )
            .scalars()
            .all()
        )
        artifact_rows = (
            self._session.execute(
                select(ArtifactRow)
                .where(ArtifactRow.run_id == run_id)
                .order_by(ArtifactRow.created_at.asc())
            )
            .scalars()
            .all()
        )
        suite_rows = (
            self._session.execute(
                select(SuiteRow).where(SuiteRow.run_id == run_id).order_by(SuiteRow.path.asc())
            )
            .scalars()
            .all()
        )

        by_attempt: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for artifact in artifact_rows:
            if artifact.attempt_id is not None:
                by_attempt[artifact.attempt_id].append(artifact.id)

        attempts_by_test: dict[uuid.UUID, list[TestAttempt]] = defaultdict(list)
        for attempt in attempt_rows:
            attempts_by_test[attempt.test_id].append(
                _attempt_to_model(attempt, by_attempt.get(attempt.id, []))
            )

        tests = [
            _case_to_model(case, attempts_by_test.get(case.test_id, []))
            for case in sorted(case_rows, key=lambda row: (row.suite_path, row.name))
        ]

        counts_by_suite: dict[uuid.UUID, RunCounts] = defaultdict(RunCounts)
        for case in case_rows:
            counts = counts_by_suite[case.suite_id]
            counts.add(_test_status(case.status))
            if case.flaky:
                counts.flaky += 1
            if case.attempt_count > 1:
                counts.retried += 1

        suites = [
            TestSuite(
                id=suite.suite_id,
                run_id=suite.run_id,
                name=suite.name,
                path=suite.path,
                parent_id=suite.parent_id,
                started_at=suite.started_at,
                finished_at=suite.finished_at,
                duration_ms=suite.duration_ms,
                counts=counts_by_suite.get(suite.suite_id, RunCounts()),
                metadata=dict(suite.meta or {}),
            )
            for suite in suite_rows
        ]

        return RunDetail(
            run=_run_to_model(run_row),
            suites=suites,
            tests=tests,
            artifacts=[artifact_to_model(row) for row in artifact_rows],
            comparison=self.compare_run(run_id) if comparison else None,
        )

    # ----------------------------------------------------------- listing

    def _filtered(self, run_filter: RunFilter) -> Any:
        stmt = select(RunRow)
        conditions: list[Any] = []
        if run_filter.workspace_id is not None:
            conditions.append(RunRow.workspace_id == run_filter.workspace_id)
        if run_filter.project_keys:
            conditions.append(RunRow.project_key.in_(list(run_filter.project_keys)))
        if run_filter.teams:
            conditions.append(RunRow.team.in_(list(run_filter.teams)))
        if run_filter.components:
            conditions.append(RunRow.component.in_(list(run_filter.components)))
        if run_filter.test_types:
            conditions.append(RunRow.test_type.in_([str(t) for t in run_filter.test_types]))
        if run_filter.environments:
            conditions.append(RunRow.environment.in_(list(run_filter.environments)))
        if run_filter.branches:
            conditions.append(RunRow.branch.in_(list(run_filter.branches)))
        if run_filter.statuses:
            conditions.append(RunRow.status.in_([str(s) for s in run_filter.statuses]))
        if run_filter.live_only:
            conditions.append(RunRow.status.in_(_LIVE_STATUSES))
        if run_filter.tags:
            # `tags` is a JSON array of slugs. Slugs cannot contain a quote, so
            # matching the quoted token cannot straddle two elements: `"ui"`
            # does not occur inside `["uikit"]`. Escaped, because `_` is a legal
            # slug character and an unescaped one is a LIKE wildcard.
            #
            # ILIKE, here and in the search below, because SQLite's LIKE ignores
            # ASCII case and PostgreSQL's does not; ILIKE is what keeps one
            # query meaning the same thing on both. (SQLAlchemy renders it as
            # `lower(x) LIKE lower(y)` on SQLite, which is the same comparison.)
            conditions.append(
                sa.or_(
                    *[
                        sa.cast(RunRow.tags, sa.Text).ilike(
                            f'%"{_escape_like(tag)}"%', escape=_LIKE_ESCAPE
                        )
                        for tag in run_filter.tags
                    ]
                )
            )
        if run_filter.search:
            term = _like(run_filter.search)
            conditions.append(
                sa.or_(
                    RunRow.name.ilike(term, escape=_LIKE_ESCAPE),
                    RunRow.project_key.ilike(term, escape=_LIKE_ESCAPE),
                    RunRow.branch.ilike(term, escape=_LIKE_ESCAPE),
                    RunRow.commit_sha.ilike(term, escape=_LIKE_ESCAPE),
                    RunRow.commit_message.ilike(term, escape=_LIKE_ESCAPE),
                    RunRow.pipeline_id.ilike(term, escape=_LIKE_ESCAPE),
                )
            )
        if run_filter.since is not None:
            conditions.append(self._at_or_after(run_filter.since))
        if run_filter.until is not None:
            conditions.append(self._at_or_before(run_filter.until))
        if conditions:
            stmt = stmt.where(sa.and_(*conditions))
        return stmt

    @staticmethod
    def _at_or_after(moment: datetime) -> Any:
        """A run happened at or after ``moment``, falling back to when it was created."""
        return sa.or_(
            sa.and_(RunRow.started_at.is_not(None), RunRow.started_at >= moment),
            sa.and_(RunRow.started_at.is_(None), RunRow.created_at >= moment),
        )

    @staticmethod
    def _at_or_before(moment: datetime) -> Any:
        return sa.or_(
            sa.and_(RunRow.started_at.is_not(None), RunRow.started_at <= moment),
            sa.and_(RunRow.started_at.is_(None), RunRow.created_at <= moment),
        )

    def list_runs(self, run_filter: RunFilter) -> list[RunSummary]:
        stmt = (
            self._filtered(run_filter)
            .order_by(*_sort_columns(self._dialect))
            .limit(max(0, run_filter.limit))
            .offset(max(0, run_filter.offset))
        )
        rows = self._session.execute(stmt).scalars().all()
        return [_run_to_summary(row) for row in rows]

    def count_runs(self, run_filter: RunFilter) -> int:
        inner = self._filtered(run_filter).subquery()
        total = self._session.execute(select(sa.func.count()).select_from(inner)).scalar_one()
        return int(total)

    # ------------------------------------------------------- comparison

    def compare_run(self, run_id: uuid.UUID) -> RunComparison | None:
        """Measure a run against the last comparable one.

        "Comparable" is the narrowest honest slice: same project, team,
        component, test type, environment and branch. Comparing a nightly
        regression run on ``main`` against a smoke run on a feature branch
        produces deltas that look like regressions and are not.
        """
        current = self._session.get(RunRow, run_id)
        if current is None:
            return None

        current_key = current.started_at or current.created_at
        baseline = self._session.execute(
            select(RunRow)
            .where(
                RunRow.id != current.id,
                RunRow.workspace_id == current.workspace_id,
                RunRow.project_key == current.project_key,
                _eq_or_null(RunRow.team, current.team),
                _eq_or_null(RunRow.component, current.component),
                RunRow.test_type == current.test_type,
                _eq_or_null(RunRow.environment, current.environment),
                _eq_or_null(RunRow.branch, current.branch),
                sa.func.coalesce(RunRow.started_at, RunRow.created_at) < current_key,
            )
            .order_by(sa.func.coalesce(RunRow.started_at, RunRow.created_at).desc())
            .limit(1)
        ).scalar_one_or_none()
        if baseline is None:
            return None

        current_statuses = self._case_statuses(current.id)
        baseline_statuses = self._case_statuses(baseline.id)
        current_ids = set(current_statuses)
        baseline_ids = set(baseline_statuses)
        shared = current_ids & baseline_ids

        newly_failing = sorted(
            test_id
            for test_id in shared
            if is_failureish(current_statuses[test_id])
            and not is_failureish(baseline_statuses[test_id])
        )
        newly_passing = sorted(
            test_id
            for test_id in shared
            if is_successful(current_statuses[test_id])
            and is_failureish(baseline_statuses[test_id])
        )
        still_failing = sorted(
            test_id
            for test_id in shared
            if is_failureish(current_statuses[test_id])
            and is_failureish(baseline_statuses[test_id])
        )

        current_counts = _counts_from_json(current.counts)
        baseline_counts = _counts_from_json(baseline.counts)
        pass_rate_delta: float | None = None
        if current_counts.pass_rate is not None and baseline_counts.pass_rate is not None:
            pass_rate_delta = round(current_counts.pass_rate - baseline_counts.pass_rate, 2)
        duration_delta: int | None = None
        if current.duration_ms is not None and baseline.duration_ms is not None:
            duration_delta = current.duration_ms - baseline.duration_ms

        return RunComparison(
            baseline_run_id=baseline.id,
            baseline_started_at=baseline.started_at,
            pass_rate_delta=pass_rate_delta,
            duration_delta_ms=duration_delta,
            total_delta=current_counts.total - baseline_counts.total,
            newly_failing=newly_failing,
            newly_passing=newly_passing,
            still_failing=still_failing,
            added=sorted(current_ids - baseline_ids),
            removed=sorted(baseline_ids - current_ids),
        )

    def _case_statuses(self, run_id: uuid.UUID) -> dict[uuid.UUID, TestStatus]:
        rows = self._session.execute(
            select(TestCaseRow.test_id, TestCaseRow.status).where(TestCaseRow.run_id == run_id)
        ).all()
        return {row[0]: _test_status(row[1]) for row in rows}

    # ------------------------------------------------------------ tests

    def get_test_detail(
        self, test_id: uuid.UUID, *, workspace_id: uuid.UUID, history: int = 30
    ) -> TestDetail | None:
        rows = self._session.execute(
            select(TestCaseRow, RunRow)
            .join(RunRow, RunRow.id == TestCaseRow.run_id)
            .where(TestCaseRow.test_id == test_id, RunRow.workspace_id == workspace_id)
            .order_by(*_sort_columns(self._dialect))
            .limit(max(1, history))
        ).all()
        if not rows:
            return None

        newest_case, newest_run = rows[0]
        attempt_rows = (
            self._session.execute(
                select(AttemptRow)
                .where(AttemptRow.run_id == newest_case.run_id, AttemptRow.test_id == test_id)
                .order_by(AttemptRow.attempt_number.asc())
            )
            .scalars()
            .all()
        )
        latest = _case_to_model(newest_case, [_attempt_to_model(a, []) for a in attempt_rows])

        points = [
            TestHistoryPoint(
                run_id=run.id,
                started_at=run.started_at,
                status=_test_status(case.status),
                duration_ms=case.duration_ms,
                attempts=max(1, case.attempt_count),
                branch=run.branch,
                environment=run.environment,
            )
            for case, run in rows
        ]

        # `flake_rate` reads oldest-first; the history itself stays newest-first
        # so that every list this repository returns is ordered the same way.
        oldest_first = [
            (_test_status(case.status), bool(case.flaky)) for case, _run in reversed(rows)
        ]
        executed = [
            status
            for status, _ in oldest_first
            if status not in (TestStatus.SKIPPED, TestStatus.CANCELLED, TestStatus.UNKNOWN)
        ]
        failure_pct: float | None = None
        if executed:
            failures = sum(1 for status in executed if is_failureish(status))
            failure_pct = round(100.0 * failures / len(executed), 2)
        durations = [case.duration_ms for case, _ in rows if case.duration_ms is not None]

        return TestDetail(
            test_id=test_id,
            project_key=newest_run.project_key,
            name=newest_case.name,
            class_name=newest_case.class_name,
            suite_path=newest_case.suite_path,
            params=newest_case.params,
            full_name=newest_case.full_name,
            owner=newest_case.owner,
            tags=list(newest_case.tags or []),
            latest=latest,
            history=points,
            flake_rate=flake_rate(oldest_first),
            failure_rate=failure_pct,
            average_duration_ms=round(sum(durations) / len(durations)) if durations else None,
        )

    # ----------------------------------------------------------- facets

    def facets(self, workspace_id: uuid.UUID) -> dict[str, list[str]]:
        """Distinct values for every filter the dashboard offers."""

        def distinct(column: Any) -> list[str]:
            rows = self._session.execute(
                select(column)
                .where(RunRow.workspace_id == workspace_id, column.is_not(None))
                .distinct()
                .order_by(column.asc())
            ).all()
            return [str(row[0]) for row in rows if row[0]]

        tags: set[str] = set()
        for row in self._session.execute(
            select(RunRow.tags).where(RunRow.workspace_id == workspace_id)
        ).all():
            for tag in row[0] or []:
                tags.add(str(tag))

        return {
            "projects": distinct(RunRow.project_key),
            "teams": distinct(RunRow.team),
            "components": distinct(RunRow.component),
            "test_types": distinct(RunRow.test_type),
            "environments": distinct(RunRow.environment),
            "branches": distinct(RunRow.branch),
            "frameworks": distinct(RunRow.framework),
            "languages": distinct(RunRow.language),
            "tags": sorted(tags),
        }

    # --------------------------------------------------------- overview

    def overview(self, workspace_id: uuid.UUID, *, window_days: int = 30) -> OverviewSnapshot:
        now = utcnow()
        window = max(1, window_days)
        since = now - timedelta(days=window)
        previous_since = since - timedelta(days=window)

        # One scan covering both the window and the window before it, so the
        # per-project pass-rate delta costs no extra round trip.
        rows = self._session.execute(
            select(
                RunRow.id,
                RunRow.project_key,
                RunRow.status,
                RunRow.started_at,
                RunRow.created_at,
                RunRow.duration_ms,
                RunRow.counts,
            ).where(RunRow.workspace_id == workspace_id, self._at_or_after(previous_since))
        ).all()

        current: list[Any] = []
        previous: list[Any] = []
        for row in rows:
            moment = row.started_at or row.created_at
            (current if moment >= since else previous).append(row)

        counts = RunCounts()
        for row in current:
            counts = counts.merge(_counts_from_json(row.counts))

        live = self.list_runs(RunFilter(workspace_id=workspace_id, live_only=True, limit=50))
        recent = self.list_runs(RunFilter(workspace_id=workspace_id, limit=20))

        return OverviewSnapshot(
            generated_at=now,
            window_days=window,
            counts=counts,
            total_runs=len(current),
            total_runs_all_time=self.count_runs(RunFilter(workspace_id=workspace_id)),
            live_runs=live,
            recent_runs=recent,
            projects=self._project_health(workspace_id, current, previous, now, live),
            trend=self._trend(current, since, window),
            top_failing_tests=self._top_failing(workspace_id, since),
            flakiest_tests=self._flakiest(workspace_id, since),
            filters=self.facets(workspace_id),
        )

    def _project_health(
        self,
        workspace_id: uuid.UUID,
        current: Sequence[Any],
        previous: Sequence[Any],
        now: datetime,
        live: Sequence[RunSummary],
    ) -> list[ProjectHealth]:
        day_ago = now - timedelta(days=1)
        by_project: dict[str, list[Any]] = defaultdict(list)
        for row in current:
            by_project[row.project_key].append(row)
        previous_by_project: dict[str, list[Any]] = defaultdict(list)
        for row in previous:
            previous_by_project[row.project_key].append(row)

        flaky_counts: dict[str, int] = dict(
            self._session.execute(
                select(RunRow.project_key, sa.func.count(sa.distinct(TestCaseRow.test_id)))
                .join(TestCaseRow, TestCaseRow.run_id == RunRow.id)
                .where(
                    RunRow.workspace_id == workspace_id,
                    TestCaseRow.flaky.is_(True),
                    self._at_or_after(now - timedelta(days=self._settings.history_window_days)),
                )
                .group_by(RunRow.project_key)
            ).all()
        )
        live_by_project: dict[str, int] = defaultdict(int)
        for summary in live:
            live_by_project[summary.project_key] += 1

        health: list[ProjectHealth] = []
        for project in self.list_projects(workspace_id):
            runs = sorted(
                by_project.get(project.key, ()),
                key=lambda row: row.started_at or row.created_at,
                reverse=True,
            )
            merged = RunCounts()
            for row in runs:
                merged = merged.merge(_counts_from_json(row.counts))
            previous_merged = RunCounts()
            for row in previous_by_project.get(project.key, ()):
                previous_merged = previous_merged.merge(_counts_from_json(row.counts))

            delta: float | None = None
            if merged.pass_rate is not None and previous_merged.pass_rate is not None:
                delta = round(merged.pass_rate - previous_merged.pass_rate, 2)

            newest = runs[0] if runs else None
            health.append(
                ProjectHealth(
                    project_key=project.key,
                    name=project.name,
                    team=project.default_team,
                    runs_24h=sum(
                        1 for row in runs if (row.started_at or row.created_at) >= day_ago
                    ),
                    last_run_id=newest.id if newest else None,
                    last_run_at=newest.started_at if newest else None,
                    last_status=_run_status(newest.status) if newest else RunStatus.UNKNOWN,
                    pass_rate=merged.pass_rate,
                    pass_rate_delta=delta,
                    flaky_tests=int(flaky_counts.get(project.key, 0)),
                    live_runs=live_by_project.get(project.key, 0),
                )
            )
        return health

    #: The trend line never returns more buckets than this, whatever the window.
    #: A year of daily points is 366 objects in every overview response for a
    #: chart that is 700 pixels wide — the bucket widens instead.
    MAX_TREND_BUCKETS = 60

    @classmethod
    def _bucket_days(cls, window: int) -> int:
        """How many days each trend bucket covers, for a window of ``window`` days."""
        if window <= cls.MAX_TREND_BUCKETS:
            return 1
        return -(-window // cls.MAX_TREND_BUCKETS)  # ceil, without importing math

    @classmethod
    def _trend(cls, rows: Sequence[Any], since: datetime, window: int) -> list[TrendPoint]:
        """Bucketed pass rate and duration percentiles across the window.

        Buckets are labelled by their first day. A bucket with no runs is still
        emitted, so the chart shows a genuine gap where nothing ran rather than
        joining two distant points with a straight line that implies continuity.
        """
        span = cls._bucket_days(window)
        start = since.astimezone(UTC).date()

        buckets: dict[int, list[Any]] = defaultdict(list)
        for row in rows:
            moment = row.started_at or row.created_at
            offset = (moment.astimezone(UTC).date() - start).days
            buckets[max(0, offset) // span].append(row)

        points: list[TrendPoint] = []
        bucket_count = -(-(window + 1) // span)
        for index in range(bucket_count):
            bucket_rows = buckets.get(index, [])
            merged = RunCounts()
            for row in bucket_rows:
                merged = merged.merge(_counts_from_json(row.counts))
            durations = [row.duration_ms for row in bucket_rows if row.duration_ms is not None]
            p50 = percentile(durations, 0.5)
            p95 = percentile(durations, 0.95)
            points.append(
                TrendPoint(
                    bucket=(start + timedelta(days=index * span)).isoformat(),
                    runs=len(bucket_rows),
                    pass_rate=merged.pass_rate,
                    failure_rate=merged.failure_rate,
                    duration_ms_p50=round(p50) if p50 is not None else None,
                    duration_ms_p95=round(p95) if p95 is not None else None,
                    total=merged.total,
                    failed=merged.failed + merged.error,
                )
            )
        return points

    def _top_failing(self, workspace_id: uuid.UUID, since: datetime) -> list[TestDetail]:
        rows = self._session.execute(
            select(TestCaseRow.test_id, sa.func.count().label("hits"))
            .join(RunRow, RunRow.id == TestCaseRow.run_id)
            .where(
                RunRow.workspace_id == workspace_id,
                self._at_or_after(since),
                TestCaseRow.status.in_(
                    (TestStatus.FAILED.value, TestStatus.ERROR.value, TestStatus.XPASSED.value)
                ),
            )
            .group_by(TestCaseRow.test_id)
            .order_by(sa.desc("hits"))
            .limit(10)
        ).all()
        return self._details_for([row[0] for row in rows], workspace_id)

    def _flakiest(self, workspace_id: uuid.UUID, since: datetime) -> list[TestDetail]:
        rows = self._session.execute(
            select(TestCaseRow.test_id, sa.func.count().label("hits"))
            .join(RunRow, RunRow.id == TestCaseRow.run_id)
            .where(
                RunRow.workspace_id == workspace_id,
                self._at_or_after(since),
                TestCaseRow.flaky.is_(True),
            )
            .group_by(TestCaseRow.test_id)
            .order_by(sa.desc("hits"))
            .limit(10)
        ).all()
        details = self._details_for([row[0] for row in rows], workspace_id)
        details.sort(key=lambda detail: detail.flake_rate or 0.0, reverse=True)
        return details

    def _details_for(
        self, test_ids: Iterable[uuid.UUID], workspace_id: uuid.UUID
    ) -> list[TestDetail]:
        out: list[TestDetail] = []
        for test_id in test_ids:
            detail = self.get_test_detail(
                test_id,
                workspace_id=workspace_id,
                history=self._settings.flaky_window_runs,
            )
            if detail is not None:
                out.append(detail)
        return out

    # ------------------------------------------------------ annotations

    def annotate_run(
        self,
        run_id: uuid.UUID,
        *,
        source_adapter: str | None = None,
        warnings: Sequence[ParserWarning] | None = None,
        name: str | None = None,
    ) -> None:
        row = self._session.get(RunRow, run_id)
        if row is None:
            return
        if source_adapter is not None:
            row.source_adapter = short_text(source_adapter)
        if name is not None:
            row.name = short_text(name)
        if warnings:
            existing = list(row.warnings or [])
            existing.extend(warning.model_dump(mode="json") for warning in warnings)
            # Reassigned, not appended in place: a plain JSON column is not a
            # mutable-tracked type, and an in-place append is never flushed.
            row.warnings = existing[:1000]
        row.updated_at = utcnow()


def _eq_or_null(column: Any, value: Any) -> Any:
    """Match a nullable facet, treating NULL as a value rather than as unknown."""
    if value is None:
        return column.is_(None)
    return column == value


# --------------------------------------------------------------- the fold


class SqlEventRepository:
    """The append-only log and the fold. The only write path in Test Hub.

    Every method here runs inside the surrounding unit of work's transaction and
    does **not** commit: an import stores the raw report as an artifact and
    annotates the run after the events land, and all of that has to succeed or
    fail together. Whoever opened the unit of work commits it.

    The fold reads before it writes, so it needs writers to one run not to
    interleave. SQLite guarantees that for the whole database; on PostgreSQL the
    two entry points below take a per-run advisory lock first
    (:class:`~test_hub.storage.locks.WriteLocks`), which guarantees it per run
    and lets different runs be written in parallel.
    """

    def __init__(
        self,
        session: Session,
        settings: Settings,
        runs: SqlRunRepository,
        *,
        locks: WriteLocks | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._runs = runs
        self._locks = locks if locks is not None else WriteLocks(session)

    # ------------------------------------------------------------ public

    def apply_events(
        self, events: Sequence[DomainEvent], *, idempotency_key: str | None = None
    ) -> WriteResult:
        if not events:
            raise ValidationError("apply_events was given an empty batch")
        run_ids = {event.runid for event in events}
        if len(run_ids) > 1:
            raise ValidationError(
                "every event in a batch must belong to the same run",
                detail={"run_ids": sorted(str(run_id) for run_id in run_ids)},
            )
        run_id = next(iter(run_ids))

        # Key before run, always in that order. A retried import carries the
        # same key with a fresh run id, so the run lock alone would let both
        # copies past `_replay` at once on PostgreSQL. (Live publishing that
        # needed `next_sequence` first already holds the run lock by now; the
        # reverse order is only reachable by one key used for two different
        # kinds of request at once, and PostgreSQL breaks that deadlock itself.)
        if idempotency_key:
            self._locks.idempotency_key(idempotency_key[:512])
        self._locks.run(run_id)

        if idempotency_key:
            replay = self._replay(idempotency_key, len(events))
            if replay is not None:
                return replay

        ordered = sorted(events, key=lambda event: (event.sequence, event.time))
        already = self._existing_keys(ordered)
        seen: set[tuple[str, uuid.UUID]] = set()

        state = _BatchState(run_id=run_id)
        received = utcnow()
        for event in ordered:
            key = (event.source, event.id)
            if key in already or key in seen:
                state.ignored += 1
                continue
            seen.add(key)
            self._session.add(
                EventRow(
                    id=uuid.uuid4(),
                    workspace_id=event.workspaceid,
                    run_id=event.runid,
                    source=event.source,
                    event_id=event.id,
                    type=str(event.type),
                    time=event.time,
                    sequence=event.sequence,
                    data=event.data,
                    received_at=received,
                )
            )
            self._dispatch(event, state)
            state.applied += 1
            state.emitted.append(event)

        run = state.run or self._session.get(RunRow, run_id)
        if run is not None:
            self._refold(run)
        self._session.flush()

        result = WriteResult(
            run_id=run_id,
            applied=state.applied,
            ignored=state.ignored,
            created_run=state.created_run,
            suites_touched=len(state.suites_touched),
            tests_touched=len(state.tests_touched),
            attempts_created=state.attempts_created,
            emitted=state.emitted,
        )
        if idempotency_key:
            self._remember(idempotency_key, result)
        return result

    def list_events(
        self, run_id: uuid.UUID, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[DomainEvent]:
        """Replay a run's log, oldest first.

        ``after_sequence`` is exclusive, with one deliberate exception: the
        default of ``0`` means "from the beginning" rather than "after sequence
        zero". Sequences are ``ge=0``, so a publisher is entitled to number its
        first event ``0``, and a default argument that silently swallowed it
        would make the first event of such a run invisible to every replay.
        """
        conditions = [EventRow.run_id == run_id]
        if after_sequence > 0:
            conditions.append(EventRow.sequence > after_sequence)
        rows = (
            self._session.execute(
                select(EventRow)
                .where(sa.and_(*conditions))
                .order_by(EventRow.sequence.asc(), EventRow.received_at.asc())
                .limit(max(0, limit))
            )
            .scalars()
            .all()
        )
        out: list[DomainEvent] = []
        for row in rows:
            try:
                out.append(
                    DomainEvent(
                        id=row.event_id,
                        source=row.source,
                        type=EventType(row.type),
                        time=row.time,
                        workspaceid=row.workspace_id,
                        runid=row.run_id,
                        sequence=row.sequence,
                        data=dict(row.data or {}),
                    )
                )
            except (PydanticValidationError, ValueError):
                # A row written by a newer schema version we cannot represent.
                # Skipping it is right: replay must not fail the whole stream.
                continue
        return out

    def next_sequence(self, run_id: uuid.UUID) -> int:
        # Locked here and not only in `apply_events`: the number handed out
        # must still be the high-water mark when the batch using it lands, or two
        # live publishers would both be given the same block.
        self._locks.run(run_id)
        highest = self._session.execute(
            select(sa.func.max(EventRow.sequence)).where(EventRow.run_id == run_id)
        ).scalar_one_or_none()
        return int(highest) + 1 if highest is not None else 1

    # ------------------------------------------------------ idempotency

    def _replay(self, key: str, batch_size: int) -> WriteResult | None:
        row = self._session.get(IdempotencyKeyRow, key[:512])
        if row is None:
            return None
        stored = row.result or {}
        return WriteResult(
            run_id=row.run_id,
            applied=0,
            ignored=batch_size,
            created_run=bool(stored.get("created_run", False)),
            suites_touched=int(stored.get("suites_touched", 0)),
            tests_touched=int(stored.get("tests_touched", 0)),
            attempts_created=int(stored.get("attempts_created", 0)),
            emitted=[],
        )

    def _remember(self, key: str, result: WriteResult) -> None:
        self._session.add(
            IdempotencyKeyRow(
                key=key[:512],
                run_id=result.run_id,
                result={
                    "created_run": result.created_run,
                    "suites_touched": result.suites_touched,
                    "tests_touched": result.tests_touched,
                    "attempts_created": result.attempts_created,
                    "applied": result.applied,
                },
                created_at=utcnow(),
            )
        )

    def _existing_keys(self, events: Sequence[DomainEvent]) -> set[tuple[str, uuid.UUID]]:
        """Which of these events are already stored.

        A ``SELECT`` rather than a per-row ``SAVEPOINT`` around the insert: the
        surrounding transaction opened with ``BEGIN IMMEDIATE`` and therefore
        holds the write lock, so nothing can insert between this read and our
        write. A unique-violation rollback here would poison the whole batch.

        On PostgreSQL the same holds per run: ``apply_events`` took this run's
        advisory lock before calling this, so any other batch for the run either
        committed before the read (and is seen) or is waiting behind us.
        """
        found: set[tuple[str, uuid.UUID]] = set()
        ids = [event.id for event in events]
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            rows = self._session.execute(
                select(EventRow.source, EventRow.event_id).where(EventRow.event_id.in_(chunk))
            ).all()
            found.update((row[0], row[1]) for row in rows)
        return found

    # --------------------------------------------------------- dispatch

    def _dispatch(self, event: DomainEvent, state: _BatchState) -> None:
        handler = {
            EventType.RUN_QUEUED: self._on_run_queued,
            EventType.RUN_STARTED: self._on_run_started,
            EventType.SUITE_STARTED: self._on_suite_started,
            EventType.SUITE_FINISHED: self._on_suite_finished,
            EventType.TEST_STARTED: self._on_test_started,
            EventType.TEST_FINISHED: self._on_test_finished,
            EventType.ARTIFACT_PUBLISHED: self._on_artifact_published,
            EventType.RUN_FINISHED: self._on_run_finished,
        }[event.type]
        handler(event, state)

    # ------------------------------------------------------------- runs

    def _ensure_workspace(self, workspace_id: uuid.UUID) -> None:
        if self._session.get(WorkspaceRow, workspace_id) is not None:
            return
        # An event for a workspace nobody created is not a reason to lose a test
        # run. Materialise it rather than failing the foreign key — race-safely,
        # because two runs' first batches can both arrive here on PostgreSQL.
        _insert_if_absent(
            self._session,
            WorkspaceRow.__table__,
            {
                "id": workspace_id,
                "slug": f"workspace-{workspace_id.hex[:12]}",
                "name": f"Workspace {workspace_id.hex[:8]}",
                "created_at": utcnow(),
            },
        )

    def _ensure_run(
        self, event: DomainEvent, state: _BatchState, *, project_key: str | None = None
    ) -> RunRow:
        if state.run is not None:
            return state.run
        row = self._session.get(RunRow, event.runid)
        if row is None:
            self._ensure_workspace(event.workspaceid)
            now = utcnow()
            row = RunRow(
                id=event.runid,
                workspace_id=event.workspaceid,
                project_key=coerce_slug(project_key) or UNKNOWN_PROJECT,
                test_type=TestType.OTHER.value,
                trigger=TriggerType.UNKNOWN.value,
                status=RunStatus.QUEUED.value,
                counts=_counts_to_json(RunCounts()),
                tags=[],
                meta={},
                warnings=[],
                created_at=now,
                updated_at=now,
            )
            self._session.add(row)
            self._session.flush()
            self._runs.ensure_project(row.workspace_id, row.project_key, name=row.project_key)
            state.created_run = True
        state.run = row
        return row

    def _on_run_queued(self, event: DomainEvent, state: _BatchState) -> None:
        data = RunQueuedData.model_validate(event.data)
        run = self._ensure_run(event, state, project_key=data.project_key)

        # A late `run.queued` (an out-of-order publisher, a replayed shard) must
        # never blank out a run that is already reporting results, so every
        # assignment below is fill-if-empty.
        if run.project_key == UNKNOWN_PROJECT:
            run.project_key = coerce_slug(data.project_key) or UNKNOWN_PROJECT
        self._runs.ensure_project(run.workspace_id, run.project_key, name=run.project_key)

        if not run.name:
            run.name = short_text(data.name)
        if not run.team:
            run.team = coerce_slug(data.team)
        if not run.component:
            run.component = coerce_slug(data.component)
        if run.test_type == TestType.OTHER.value:
            run.test_type = str(data.test_type)
        if run.trigger == TriggerType.UNKNOWN.value:
            run.trigger = str(data.trigger)
        self._fill_context(run, data.context)
        if not run.tags:
            run.tags = coerce_slugs(data.tags)
        merged = dict(run.meta or {})
        for key, value in data.metadata.items():
            merged.setdefault(key, value)
        if data.expected_tests is not None:
            merged.setdefault("expected_tests", data.expected_tests)
        run.meta = merged
        run.updated_at = utcnow()

    def _on_run_started(self, event: DomainEvent, state: _BatchState) -> None:
        data = RunStartedData.model_validate(event.data)
        run = self._ensure_run(event, state, project_key=data.project_key)
        if run.project_key == UNKNOWN_PROJECT and data.project_key:
            run.project_key = coerce_slug(data.project_key) or UNKNOWN_PROJECT
        if run.started_at is None:
            run.started_at = data.started_at
        if data.context is not None:
            self._fill_context(run, data.context)
        if _run_status(run.status) not in RUN_TERMINAL_STATUSES:
            run.status = RunStatus.RUNNING.value
        run.updated_at = utcnow()

    def _on_run_finished(self, event: DomainEvent, state: _BatchState) -> None:
        data = RunFinishedData.model_validate(event.data)
        run = self._ensure_run(event, state)
        run.finished_at = data.finished_at
        if data.duration_ms is not None and data.duration_ms >= 0:
            run.duration_ms = data.duration_ms
        if data.status is RunStatus.CANCELLED:
            # The one publisher-supplied status the fold does not override:
            # nothing in the attempt rows can tell us a run was killed.
            run.status = RunStatus.CANCELLED.value
        merged = dict(run.meta or {})
        for key, value in data.metadata.items():
            merged.setdefault(key, value)
        run.meta = merged
        run.updated_at = utcnow()

    @staticmethod
    def _fill_context(run: RunRow, context: RunContext) -> None:
        if not run.framework:
            run.framework = short_text(context.framework)
        if not run.framework_version:
            run.framework_version = short_text(context.framework_version)
        if not run.language:
            run.language = short_text(context.language)
        if not run.language_version:
            run.language_version = short_text(context.language_version)
        if not run.environment:
            run.environment = coerce_slug(context.environment)
        if not run.branch:
            run.branch = short_text(context.branch)
        if not run.commit_sha:
            run.commit_sha = short_text(context.commit_sha, limit=64)
        if not run.commit_message:
            run.commit_message = short_text(context.commit_message)
        if not run.pipeline_id:
            run.pipeline_id = short_text(context.pipeline_id)
        if not run.pipeline_url and context.pipeline_url:
            run.pipeline_url = _http_url(str(context.pipeline_url))
        if not run.ci_provider:
            run.ci_provider = short_text(context.ci_provider)
        if not run.hostname:
            run.hostname = short_text(context.hostname)
        if not run.os:
            run.os = short_text(context.os)
        if not run.executor:
            run.executor = short_text(context.executor)

    # ----------------------------------------------------------- suites

    def _suite(self, run: RunRow, suite_id: uuid.UUID, state: _BatchState) -> SuiteRow | None:
        cached = state.suites.get(suite_id)
        if cached is not None:
            return cached
        row = self._session.execute(
            select(SuiteRow).where(SuiteRow.run_id == run.id, SuiteRow.suite_id == suite_id)
        ).scalar_one_or_none()
        if row is not None:
            state.suites[suite_id] = row
        return row

    def _upsert_suite(
        self,
        run: RunRow,
        state: _BatchState,
        *,
        suite_id: uuid.UUID,
        name: str | None,
        path: str | None,
        parent_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SuiteRow:
        row = self._suite(run, suite_id, state)
        if row is None:
            row = SuiteRow(
                id=uuid.uuid4(),
                run_id=run.id,
                suite_id=suite_id,
                name=short_text(name) or short_text(path) or "(unnamed suite)",
                path=short_text(path) or short_text(name) or "",
                parent_id=parent_id,
                meta=dict(metadata or {}),
            )
            self._session.add(row)
            state.suites[suite_id] = row
        else:
            if name:
                row.name = short_text(name) or row.name
            if path:
                row.path = short_text(path) or row.path
            if parent_id is not None:
                row.parent_id = parent_id
            if metadata:
                merged = dict(row.meta or {})
                merged.update(metadata)
                row.meta = merged
        state.suites_touched.add(suite_id)
        return row

    def _on_suite_started(self, event: DomainEvent, state: _BatchState) -> None:
        data = SuiteStartedData.model_validate(event.data)
        run = self._ensure_run(event, state)
        row = self._upsert_suite(
            run,
            state,
            suite_id=data.suite_id,
            name=data.name,
            path=data.path,
            parent_id=data.parent_id,
            metadata=data.metadata,
        )
        if row.started_at is None:
            row.started_at = data.started_at

    def _on_suite_finished(self, event: DomainEvent, state: _BatchState) -> None:
        data = SuiteFinishedData.model_validate(event.data)
        run = self._ensure_run(event, state)
        row = self._upsert_suite(run, state, suite_id=data.suite_id, name=None, path=None)
        row.finished_at = data.finished_at
        if data.duration_ms is not None and data.duration_ms >= 0:
            row.duration_ms = data.duration_ms
        elif row.started_at is not None and row.finished_at is not None:
            row.duration_ms = max(
                0, round((row.finished_at - row.started_at).total_seconds() * 1000)
            )

    # ------------------------------------------------------------ tests

    def _derive_test_id(self, run: RunRow, data: TestStartedData | TestFinishedData) -> uuid.UUID:
        if data.test_id is not None:
            return data.test_id
        return test_identity(
            project=run.project_key,
            suite_path=data.suite_path,
            test_name=data.name,
            class_name=data.class_name,
            params=data.params,
        )

    def _upsert_case(
        self,
        run: RunRow,
        state: _BatchState,
        *,
        test_id: uuid.UUID,
        data: TestStartedData | TestFinishedData,
    ) -> TestCaseRow:
        # A test that names a suite nobody announced still belongs somewhere;
        # synthesising the suite from the test's own path keeps the run page
        # complete for adapters that only emit test events.
        if self._suite(run, data.suite_id, state) is None:
            self._upsert_suite(
                run, state, suite_id=data.suite_id, name=data.suite_path, path=data.suite_path
            )

        row = state.cases.get(test_id)
        if row is None:
            row = self._session.execute(
                select(TestCaseRow).where(
                    TestCaseRow.run_id == run.id, TestCaseRow.test_id == test_id
                )
            ).scalar_one_or_none()
        if row is None:
            row = TestCaseRow(
                id=uuid.uuid4(),
                run_id=run.id,
                test_id=test_id,
                suite_id=data.suite_id,
                name=short_text(data.name) or "(unnamed test)",
                class_name=short_text(data.class_name),
                suite_path=short_text(data.suite_path) or "",
                params=short_text(data.params),
                full_name=short_text(data.full_name),
                file=short_text(data.file),
                owner=short_text(data.owner),
                tags=coerce_slugs(data.tags),
                markers=short_texts(data.markers),
                status=TestStatus.UNKNOWN.value,
                attempt_count=0,
                flaky=False,
                meta={},
            )
            self._session.add(row)
        else:
            row.suite_id = data.suite_id
            row.name = short_text(data.name) or row.name
            row.class_name = short_text(data.class_name) or row.class_name
            row.suite_path = short_text(data.suite_path) or row.suite_path
            row.params = short_text(data.params) or row.params
            row.full_name = short_text(data.full_name) or row.full_name
            row.file = short_text(data.file) or row.file
            row.owner = short_text(data.owner) or row.owner
            if data.tags:
                row.tags = coerce_slugs(data.tags)
            if data.markers:
                row.markers = short_texts(data.markers)
        state.cases[test_id] = row
        state.tests_touched.add(test_id)
        return row

    def _attempt(
        self, run: RunRow, state: _BatchState, test_id: uuid.UUID, number: int
    ) -> AttemptRow | None:
        cached = state.attempts.get((test_id, number))
        if cached is not None:
            return cached
        row = self._session.execute(
            select(AttemptRow).where(
                AttemptRow.run_id == run.id,
                AttemptRow.test_id == test_id,
                AttemptRow.attempt_number == number,
            )
        ).scalar_one_or_none()
        if row is not None:
            state.attempts[(test_id, number)] = row
        return row

    def _on_test_started(self, event: DomainEvent, state: _BatchState) -> None:
        data = TestStartedData.model_validate(event.data)
        run = self._ensure_run(event, state)
        test_id = self._derive_test_id(run, data)
        self._upsert_case(run, state, test_id=test_id, data=data)

        if self._attempt(run, state, test_id, data.attempt_number) is not None:
            return
        row = AttemptRow(
            id=uuid.uuid4(),
            run_id=run.id,
            test_id=test_id,
            attempt_number=data.attempt_number,
            status=TestStatus.RUNNING.value,
            started_at=data.started_at,
            worker=short_text(data.worker),
            meta={},
        )
        self._session.add(row)
        state.attempts[(test_id, data.attempt_number)] = row
        state.attempts_created += 1

    def _on_test_finished(self, event: DomainEvent, state: _BatchState) -> None:
        data = TestFinishedData.model_validate(event.data)
        run = self._ensure_run(event, state)
        test_id = self._derive_test_id(run, data)
        self._upsert_case(run, state, test_id=test_id, data=data)

        limit = self._settings.max_text_field_bytes
        redactor = get_redactor()
        stdout, cut_out = long_text(redactor.redact(data.stdout), limit)
        stderr, cut_err = long_text(redactor.redact(data.stderr), limit)
        logs, cut_log = long_text(redactor.redact(data.logs), limit)
        failure, cut_fail = self._failure_json(data.failure, limit)
        truncated = cut_out or cut_err or cut_log or cut_fail

        meta = dict(data.metadata)
        if truncated and failure is None:
            # Nowhere else to record it: inventing a FailureDetail on a passing
            # test would make it look like it failed.
            meta["text_truncated"] = True

        row = self._attempt(run, state, test_id, data.attempt_number)
        if row is None:
            # A retry is a *new row*, keyed by attempt_number. Attempt 1's
            # verdict is never rewritten by attempt 2 — that difference is the
            # entire evidence that a test is flaky.
            row = AttemptRow(
                id=uuid.uuid4(),
                run_id=run.id,
                test_id=test_id,
                attempt_number=data.attempt_number,
                status=str(data.status),
                meta={},
            )
            self._session.add(row)
            state.attempts[(test_id, data.attempt_number)] = row
            state.attempts_created += 1

        row.status = str(data.status)
        row.original_status = short_text(data.original_status)
        if data.started_at is not None:
            row.started_at = data.started_at
        row.finished_at = data.finished_at
        if data.duration_ms is not None:
            row.duration_ms = max(0, data.duration_ms)
        elif row.started_at is not None and row.finished_at is not None:
            row.duration_ms = max(
                0, round((row.finished_at - row.started_at).total_seconds() * 1000)
            )
        row.failure = failure
        row.stdout = stdout
        row.stderr = stderr
        row.logs = logs
        row.skip_reason = short_text(redactor.redact(data.skip_reason))
        row.worker = short_text(data.worker)
        row.meta = meta

    @staticmethod
    def _failure_json(
        failure: FailureDetail | None, limit: int
    ) -> tuple[dict[str, Any] | None, bool]:
        """Redact, then truncate, then flag. Order matters.

        Truncating first could leave the tail of a secret in the stored text
        when the pattern that would have matched it straddles the cut.
        """
        if failure is None:
            return None, False
        redactor = get_redactor()
        message, cut_message = long_text(redactor.redact(failure.message), limit)
        stack, cut_stack = long_text(redactor.redact(failure.stack_trace), limit)
        expected, cut_expected = long_text(redactor.redact(failure.expected), limit)
        actual, cut_actual = long_text(redactor.redact(failure.actual), limit)
        cut = cut_message or cut_stack or cut_expected or cut_actual
        detail = FailureDetail(
            type=short_text(failure.type),
            message=message,
            stack_trace=stack,
            expected=expected,
            actual=actual,
            truncated=failure.truncated or cut,
        )
        return detail.model_dump(mode="json"), cut

    # -------------------------------------------------------- artifacts

    def _on_artifact_published(self, event: DomainEvent, state: _BatchState) -> None:
        data = ArtifactPublishedData.model_validate(event.data)
        run = self._ensure_run(event, state)
        row = self._session.get(ArtifactRow, data.artifact_id)
        if row is None:
            row = ArtifactRow(
                id=data.artifact_id,
                workspace_id=run.workspace_id,
                run_id=run.id,
                created_at=utcnow(),
            )
            self._session.add(row)
        row.test_id = data.test_id
        row.attempt_id = data.attempt_id
        row.kind = str(data.kind)
        row.name = short_text(data.name) or "artifact"
        row.content_type = short_text(data.content_type) or "application/octet-stream"
        row.size_bytes = max(0, data.size_bytes)
        row.sha256 = data.sha256
        row.url = short_text(data.url)
        row.meta = dict(data.metadata)

    # ------------------------------------------------------------- fold

    def _refold(self, run: RunRow) -> None:
        """Rebuild every derived number on the run from its attempt rows.

        Runs after every batch rather than incrementally, because incremental
        counters drift: an event applied twice, a shard uploaded out of order or
        a retry arriving before its first attempt all leave a counter wrong in a
        way nobody notices until the dashboard disagrees with the run page.
        """
        self._session.flush()
        attempts = (
            self._session.execute(
                select(AttemptRow)
                .where(AttemptRow.run_id == run.id)
                .order_by(AttemptRow.test_id.asc(), AttemptRow.attempt_number.asc())
            )
            .scalars()
            .all()
        )
        cases = (
            self._session.execute(select(TestCaseRow).where(TestCaseRow.run_id == run.id))
            .scalars()
            .all()
        )

        by_test: dict[uuid.UUID, list[AttemptRow]] = defaultdict(list)
        for attempt in attempts:
            by_test[attempt.test_id].append(attempt)

        counts = RunCounts()
        flaky_tests = 0
        retried_tests = 0
        for case in cases:
            rows = by_test.get(case.test_id, [])
            statuses = [_test_status(row.status) for row in rows]
            case.status = str(effective_status(statuses))
            case.attempt_count = len(rows)
            case.flaky = is_flaky_within_run(statuses)
            case.duration_ms = sum(row.duration_ms or 0 for row in rows) if rows else None
            counts.add(effective_status(statuses))
            if case.flaky:
                flaky_tests += 1
            if len(rows) > 1:
                retried_tests += 1

        counts.flaky = flaky_tests
        counts.retried = retried_tests
        run.counts = _counts_to_json(counts)

        computed = run_duration_ms(
            run.started_at, run.finished_at, [row.duration_ms or 0 for row in attempts]
        )
        if computed is not None:
            run.duration_ms = computed
        run.status = str(self._fold_status(run, counts, has_attempts=bool(attempts)))
        run.updated_at = utcnow()

    @staticmethod
    def _fold_status(run: RunRow, counts: RunCounts, *, has_attempts: bool) -> RunStatus:
        if run.status == RunStatus.CANCELLED.value:
            return RunStatus.CANCELLED
        if run.finished_at is not None:
            if counts.error > 0:
                return RunStatus.ERROR
            if counts.failed > 0 or counts.xpassed > 0:
                return RunStatus.FAILED
            if counts.total == 0:
                return RunStatus.UNKNOWN
            return RunStatus.PASSED
        if has_attempts or run.started_at is not None:
            return RunStatus.RUNNING
        return RunStatus.QUEUED


class _BatchState:
    """Scratch space for one :meth:`SqlEventRepository.apply_events` call.

    Rows touched by an earlier event in the same batch are kept here so the
    fifteenth ``test.finished`` for a suite does not re-query it, and — more
    importantly — so two events in one batch cannot each insert their own copy
    of the same row before the first flush.
    """

    __slots__ = (
        "applied",
        "attempts",
        "attempts_created",
        "cases",
        "created_run",
        "emitted",
        "ignored",
        "run",
        "run_id",
        "suites",
        "suites_touched",
        "tests_touched",
    )

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        self.run: RunRow | None = None
        self.created_run = False
        self.applied = 0
        self.ignored = 0
        self.attempts_created = 0
        self.suites: dict[uuid.UUID, SuiteRow] = {}
        self.cases: dict[uuid.UUID, TestCaseRow] = {}
        self.attempts: dict[tuple[uuid.UUID, int], AttemptRow] = {}
        self.suites_touched: set[uuid.UUID] = set()
        self.tests_touched: set[uuid.UUID] = set()
        self.emitted: list[DomainEvent] = []
