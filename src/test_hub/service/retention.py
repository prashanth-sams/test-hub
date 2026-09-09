"""Retention: removing runs that are older than the operator wants to keep.

Test Hub's first release kept everything forever, and said so
(``docs/scaling.md``). This module is the other half: a policy an operator
resolves, applies, and can run again tomorrow without thinking about it.

Three things here are not obvious from the outside, and all three are the
reason this is a service rather than a ``DELETE`` statement.

**Half of the schema does not cascade.** ``suites``, ``test_cases`` and
``attempts`` carry ``ON DELETE CASCADE`` and are removed by SQLite when a run
goes. ``events``, ``artifacts`` and ``idempotency_keys`` carry a bare
``run_id`` with no foreign key at all, so deleting a run leaves every one of its
events behind — orphaned, unreachable and invisible to any query the product
makes. :data:`UNLINKED_TABLES` is the list this module clears by hand, and
``tests/test_retention.py`` fails if a new table with a ``run_id`` joins the
schema without joining that list.

**Blobs are shared.** Artifacts are content-addressed, so one file backs every
row that ever uploaded those bytes. Deleting a run says nothing about whether
its screenshot is still somebody else's screenshot, so the blobs are collected
in a second pass, after the rows have committed, by
:meth:`~test_hub.storage.base.ArtifactStore.collect_unreferenced`.

**One transaction would stall the product.** ADR-0001 D4 buys write correctness
with a single writer: every transaction holds SQLite's write lock for its whole
life. A prune that deleted six months in one transaction would block every
publisher in CI for the duration, so this works in committed batches and is
resumable rather than atomic. Interrupt it and run it again; it picks up from
the oldest run still standing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

import sqlalchemy as sa

from test_hub.domain.status import RunStatus
from test_hub.errors import ConfigurationError
from test_hub.logging import get_logger
from test_hub.storage.models import (
    ArtifactRow,
    AttemptRow,
    EventRow,
    IdempotencyKeyRow,
    RunRow,
    SuiteRow,
    TestCaseRow,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from test_hub.service.runtime import AppContext

__all__ = [
    "UNIT_DAYS",
    "UNLINKED_TABLES",
    "PruneResult",
    "RetentionPolicy",
    "RetentionService",
    "parse_duration",
    "resolve_cutoff",
]

logger = get_logger(__name__)

_T = TypeVar("_T")

#: See :data:`test_hub.storage.artifacts._PARAM_CHUNK`. Same cap, same reason.
_PARAM_CHUNK = 500

#: Tables carrying a ``run_id`` that ``runs`` has no foreign key to, so nothing
#: cascades into them. The prune deletes these by hand, in this order —
#: ``artifacts`` first because its digests have to be collected before the rows
#: go, and ``runs`` last so that its cascade runs against a schema that has
#: already lost everything else pointing at it.
UNLINKED_TABLES: tuple[str, ...] = ("artifacts", "events", "idempotency_keys")

#: Statuses that mean the run may still be being written to.
_LIVE_STATUSES: tuple[str, ...] = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)

#: The duration grammar, in days. ``m`` is thirty days and ``y`` is three
#: hundred and sixty-five — deliberately not calendar arithmetic, which would
#: mean taking on ``dateutil`` so that a retention cutoff could land on the
#: 31st. Fixed-length units are predictable, are one line to test, and are what
#: every log-rotation tool already means.
UNIT_DAYS: dict[str, int] = {"d": 1, "w": 7, "m": 30, "y": 365}

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([dwmy]?)\s*$", re.IGNORECASE)


def parse_duration(text: str) -> timedelta:
    """``90d``, ``6m``, ``2w``, ``1y``, or a bare number of days.

    Raises :class:`~test_hub.errors.ConfigurationError` with the whole grammar
    in the message, because this is read off a cron entry that nobody is
    watching and the failure has to explain itself in one line.
    """
    match = _DURATION_RE.match(text)
    if match is None:
        raise ConfigurationError(
            f"cannot read {text!r} as a duration. Use a number of days, or a number "
            f"followed by d (day), w (7 days), m (30 days) or y (365 days) — "
            f"for example 90d, 6m, 2w, 1y.",
            detail={"value": text},
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise ConfigurationError(
            f"a retention cutoff must be a positive duration, not {text!r}",
            detail={"value": text},
        )
    return timedelta(days=amount * UNIT_DAYS[(match.group(2) or "d").lower()])


def resolve_cutoff(
    *,
    older_than: str | None,
    before: datetime | None,
    default_days: int | None,
    now: datetime | None = None,
) -> datetime:
    """Turn whichever of the three the operator supplied into one instant."""
    supplied = [
        name for name, value in (("--older-than", older_than), ("--before", before)) if value
    ]
    if len(supplied) > 1:
        raise ConfigurationError(
            "--older-than and --before say the same thing two different ways; pass one."
        )
    if before is not None:
        return before if before.tzinfo else before.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    if older_than is not None:
        return now - parse_duration(older_than)
    if default_days is not None:
        return now - timedelta(days=default_days)
    raise ConfigurationError(
        "no retention cutoff. Pass --older-than 90d or --before 2026-01-01, or set "
        "TEST_HUB_RETENTION_DAYS."
    )


@dataclass(slots=True)
class RetentionPolicy:
    """A resolved answer to "what goes, and what is protected from going"."""

    cutoff: datetime
    keep_min: int = 5
    project_keys: Sequence[str] = ()
    by_import_date: bool = False
    include_unfinished: bool = False
    batch_size: int = 100
    grace_seconds: float = 24 * 3600.0


@dataclass(slots=True)
class PruneResult:
    """What a prune did, or — for a plan — what it would have done.

    Counts are what *committed*. A prune interrupted after two batches reports
    two batches' worth and nothing it had merely intended, because the number an
    operator reads has to match the number of rows that are actually gone.
    """

    dry_run: bool
    cutoff: datetime
    runs: int = 0
    suites: int = 0
    test_cases: int = 0
    attempts: int = 0
    events: int = 0
    artifacts: int = 0
    idempotency_keys: int = 0
    blobs_unlinked: int = 0
    bytes_freed: int = 0
    blobs_retained: int = 0
    skipped_live: int = 0
    protected: int = 0
    batches: int = 0
    vacuumed: bool = False
    by_project: dict[str, int] = field(default_factory=dict)
    oldest: datetime | None = None
    newest: datetime | None = None

    @property
    def rows(self) -> int:
        """Every row deleted, cascaded ones included."""
        return (
            self.runs
            + self.suites
            + self.test_cases
            + self.attempts
            + self.events
            + self.artifacts
            + self.idempotency_keys
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "cutoff": self.cutoff.isoformat(),
            "runs": self.runs,
            "rows": self.rows,
            "deleted": {
                "runs": self.runs,
                "suites": self.suites,
                "test_cases": self.test_cases,
                "attempts": self.attempts,
                "events": self.events,
                "artifacts": self.artifacts,
                "idempotency_keys": self.idempotency_keys,
            },
            "blobs_unlinked": self.blobs_unlinked,
            "blobs_retained": self.blobs_retained,
            "bytes_freed": self.bytes_freed,
            "skipped_live": self.skipped_live,
            "protected": self.protected,
            "batches": self.batches,
            "vacuumed": self.vacuumed,
            "by_project": dict(sorted(self.by_project.items())),
            "oldest": self.oldest.isoformat() if self.oldest else None,
            "newest": self.newest.isoformat() if self.newest else None,
        }


def _chunks(items: Sequence[_T], size: int = _PARAM_CHUNK) -> Iterator[Sequence[_T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class RetentionService:
    """Applies a :class:`RetentionPolicy` to the store."""

    def __init__(self, context: AppContext) -> None:
        self._context = context

    # ------------------------------------------------------------- planning

    def plan(self, policy: RetentionPolicy) -> PruneResult:
        """Count what a prune would remove, touching nothing.

        Read-only, so it takes a deferred transaction and does not queue in
        front of a publisher (see ``tests/test_read_only_scope.py``).
        """
        result = PruneResult(dry_run=True, cutoff=policy.cutoff)
        with self._context.uow_factory(read_only=True) as uow:
            session = uow.session
            protected = self._protected_ids(session, policy)
            result.protected = len(protected)
            result.skipped_live = self._count_live(session, policy, protected)
            doomed = list(self._eligible_ids(session, policy, protected))
            if not doomed:
                return result

            self._tally(session, doomed, result)
            result.oldest, result.newest = self._span(session, doomed)
            result.by_project = self._by_project(session, doomed)
            result.batches = (len(doomed) + policy.batch_size - 1) // policy.batch_size

            # The rows still exist, so the sweep is asked to answer the question
            # as if they did not. Without `excluding_runs` every digest would
            # look referenced and a plan would promise to free nothing.
            sweep = uow.artifacts.collect_unreferenced(
                self._digests(session, doomed),
                grace_seconds=policy.grace_seconds,
                excluding_runs=doomed,
                apply=False,
            )
            result.blobs_unlinked = sweep.unlinked
            result.bytes_freed = sweep.bytes_freed
            result.blobs_retained = sweep.retained
        return result

    # -------------------------------------------------------------- pruning

    def prune(self, policy: RetentionPolicy, *, vacuum: bool = False) -> PruneResult:
        """Delete in committed batches, then collect the blobs nobody wants."""
        result = PruneResult(dry_run=False, cutoff=policy.cutoff)
        digests: list[str] = []

        with self._context.uow_factory(read_only=True) as uow:
            protected = self._protected_ids(uow.session, policy)
            result.protected = len(protected)
            result.skipped_live = self._count_live(uow.session, policy, protected)

        while True:
            with self._context.uow_factory() as uow:
                session = uow.session
                batch = list(
                    self._eligible_ids(session, policy, protected, limit=policy.batch_size)
                )
                if not batch:
                    break

                self._tally(session, batch, result)
                oldest, newest = self._span(session, batch)
                result.oldest = _earliest(result.oldest, oldest)
                result.newest = _latest(result.newest, newest)
                for key, count in self._by_project(session, batch).items():
                    result.by_project[key] = result.by_project.get(key, 0) + count

                # Collect the digests before the rows that name them are gone.
                digests.extend(self._digests(session, batch))
                self._delete(session, batch)
                uow.commit()
                result.batches += 1

            logger.info(
                "pruned batch %d: %d runs, %d rows so far", result.batches, len(batch), result.rows
            )

        if digests:
            # A fresh transaction, after the deletes have committed. Unlinking
            # inside the batch above would leave live rows pointing at missing
            # files if that batch rolled back (ADR-0002 D4).
            with self._context.uow_factory() as uow:
                sweep = uow.artifacts.collect_unreferenced(
                    digests, grace_seconds=policy.grace_seconds
                )
                uow.commit()
            result.blobs_unlinked = sweep.unlinked
            result.bytes_freed = sweep.bytes_freed
            result.blobs_retained = sweep.retained

        if vacuum and result.runs:
            self._vacuum()
            result.vacuumed = True
        return result

    # ------------------------------------------------------------ selection

    def _run_time(self, policy: RetentionPolicy) -> Any:
        """The column a run is dated by.

        ``started_at`` when the publisher reported one, ``created_at`` when it
        did not — the same expression the dashboard's ``until`` filter uses
        (``SqlRunRepository._at_or_before``), so that filtering the dashboard to
        a date and pruning to the same date select the same runs.

        Both columns are fixed-width ISO-8601 UTC strings (``UtcDateTime``), so
        the string comparison this compiles to *is* a chronological comparison.
        That stops being true the moment anyone stores a local offset.
        """
        if policy.by_import_date:
            return RunRow.created_at
        return sa.func.coalesce(RunRow.started_at, RunRow.created_at)

    def _base_filter(self, policy: RetentionPolicy) -> list[Any]:
        conditions: list[Any] = [
            RunRow.workspace_id == self._context.workspace_id,
            self._run_time(policy) <= policy.cutoff,
        ]
        if policy.project_keys:
            conditions.append(RunRow.project_key.in_(list(policy.project_keys)))
        if not policy.include_unfinished:
            conditions.append(RunRow.status.not_in(_LIVE_STATUSES))
        return conditions

    def _eligible_ids(
        self,
        session: Any,
        policy: RetentionPolicy,
        protected: set[uuid.UUID],
        *,
        limit: int | None = None,
    ) -> list[uuid.UUID]:
        """Doomed runs, oldest first.

        Oldest-first is what makes an interrupted prune resumable: every batch
        removes the runs that were most eligible, so running it again resumes
        rather than restarts.
        """
        stmt = (
            sa.select(RunRow.id)
            .where(*self._base_filter(policy))
            .order_by(self._run_time(policy).asc(), RunRow.id.asc())
        )
        if protected and len(protected) <= _PARAM_CHUNK:
            # The floor is small by construction — `keep_min` per project — so
            # it fits in one bound list and LIMIT keeps doing the paging.
            stmt = stmt.where(RunRow.id.not_in(list(protected)))
            protected = set()
        if not protected:
            if limit:
                stmt = stmt.limit(limit)
            return list(session.execute(stmt).scalars().all())
        # A workspace with hundreds of projects can exceed the parameter cap.
        # Filtering in Python costs a full id scan per batch and is still
        # correct, which is the trade worth making at that size.
        rows = session.execute(stmt).scalars().all()
        eligible = [run_id for run_id in rows if run_id not in protected]
        return eligible[:limit] if limit else eligible

    def _protected_ids(self, session: Any, policy: RetentionPolicy) -> set[uuid.UUID]:
        """The newest ``keep_min`` runs of every project, whatever their age.

        Age alone is the wrong policy for a project that has gone quiet: a
        service that ran nightly for a year and then stabilised has every run on
        the far side of any cutoff, so a bare 90-day rule deletes it out of the
        dashboard entirely. Nobody who asked to keep 90 days meant that.
        """
        if policy.keep_min <= 0:
            return set()
        keys = (
            session.execute(
                sa.select(RunRow.project_key)
                .where(RunRow.workspace_id == self._context.workspace_id)
                .distinct()
            )
            .scalars()
            .all()
        )
        if policy.project_keys:
            wanted = set(policy.project_keys)
            keys = [key for key in keys if key in wanted]

        protected: set[uuid.UUID] = set()
        for key in keys:
            newest = (
                session.execute(
                    sa.select(RunRow.id)
                    .where(
                        RunRow.workspace_id == self._context.workspace_id,
                        RunRow.project_key == key,
                    )
                    .order_by(self._run_time(policy).desc(), RunRow.id.desc())
                    .limit(policy.keep_min)
                )
                .scalars()
                .all()
            )
            protected.update(newest)
        return protected

    def _count_live(self, session: Any, policy: RetentionPolicy, protected: set[uuid.UUID]) -> int:
        """Runs past the cutoff that were left alone for still being live.

        Reported rather than deleted: one of these is either a suite that is
        genuinely still running, or a publisher that died months ago. Deleting
        the first is data loss during a write; leaving the second is a leak that
        this count makes visible.
        """
        if policy.include_unfinished:
            return 0
        conditions = [
            RunRow.workspace_id == self._context.workspace_id,
            self._run_time(policy) <= policy.cutoff,
            RunRow.status.in_(_LIVE_STATUSES),
        ]
        if policy.project_keys:
            conditions.append(RunRow.project_key.in_(list(policy.project_keys)))
        rows = session.execute(sa.select(RunRow.id).where(*conditions)).scalars().all()
        return sum(1 for run_id in rows if run_id not in protected)

    # -------------------------------------------------------------- details

    def _tally(self, session: Any, run_ids: Sequence[uuid.UUID], result: PruneResult) -> None:
        """Count the dependent rows before anything deletes them.

        SQLite's cascade is silent — it reports no row count — so suites, test
        cases and attempts have to be counted here or the result would under-report
        by most of what it actually removed.
        """
        result.runs += len(run_ids)
        for attr, row in (
            ("suites", SuiteRow),
            ("test_cases", TestCaseRow),
            ("attempts", AttemptRow),
            ("events", EventRow),
            ("artifacts", ArtifactRow),
            ("idempotency_keys", IdempotencyKeyRow),
        ):
            total = 0
            for chunk in _chunks(run_ids):
                total += int(
                    session.execute(
                        sa.select(sa.func.count()).select_from(row).where(row.run_id.in_(chunk))
                    ).scalar_one()
                )
            setattr(result, attr, getattr(result, attr) + total)

    def _digests(self, session: Any, run_ids: Sequence[uuid.UUID]) -> list[str]:
        digests: list[str] = []
        for chunk in _chunks(run_ids):
            digests.extend(
                session.execute(
                    sa.select(ArtifactRow.sha256).where(ArtifactRow.run_id.in_(chunk)).distinct()
                )
                .scalars()
                .all()
            )
        return list(dict.fromkeys(digests))

    def _span(
        self, session: Any, run_ids: Sequence[uuid.UUID]
    ) -> tuple[datetime | None, datetime | None]:
        oldest: datetime | None = None
        newest: datetime | None = None
        for chunk in _chunks(run_ids):
            row = session.execute(
                sa.select(
                    sa.func.min(sa.func.coalesce(RunRow.started_at, RunRow.created_at)),
                    sa.func.max(sa.func.coalesce(RunRow.started_at, RunRow.created_at)),
                ).where(RunRow.id.in_(chunk))
            ).one()
            oldest = _earliest(oldest, _as_utc(row[0]))
            newest = _latest(newest, _as_utc(row[1]))
        return oldest, newest

    def _by_project(self, session: Any, run_ids: Sequence[uuid.UUID]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for chunk in _chunks(run_ids):
            for key, count in session.execute(
                sa.select(RunRow.project_key, sa.func.count())
                .where(RunRow.id.in_(chunk))
                .group_by(RunRow.project_key)
            ).all():
                counts[key] = counts.get(key, 0) + int(count)
        return counts

    # ------------------------------------------------------------- deleting

    def _delete(self, session: Any, run_ids: Sequence[uuid.UUID]) -> None:
        """Remove one batch. Order matters; see :data:`UNLINKED_TABLES`."""
        options = {"synchronize_session": False}
        for row in (ArtifactRow, EventRow, IdempotencyKeyRow):
            for chunk in _chunks(run_ids):
                session.execute(
                    sa.delete(row).where(row.run_id.in_(chunk)), execution_options=options
                )
        # Last, and the only one that cascades: suites, test_cases and attempts
        # go with it because they are the three tables that do have the foreign
        # key, and `PRAGMA foreign_keys=ON` is set on every connection.
        for chunk in _chunks(run_ids):
            session.execute(
                sa.delete(RunRow).where(RunRow.id.in_(chunk)), execution_options=options
            )

    def _vacuum(self) -> None:
        """Return freed pages to the filesystem.

        Deleting rows does not shrink the file — SQLite marks pages reusable and
        keeps them — so an operator who pruned to free a disk sees no change
        until this runs. It is not the default because it rewrites the whole
        database, wants up to twice the current file size in free space, and
        holds an exclusive lock throughout: all three are worse than the problem
        on a disk that is already nearly full.

        Run on a raw DBAPI connection because ``VACUUM`` cannot execute inside a
        transaction, and every SQLAlchemy connection here opens one
        (``BEGIN IMMEDIATE``) before the first statement.
        """
        raw = self._context.database.engine.raw_connection()
        try:
            cursor = raw.cursor()
            try:
                cursor.execute("VACUUM")
            finally:
                cursor.close()
        finally:
            raw.close()


def _earliest(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    return candidate if current is None else min(current, candidate)


def _latest(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    return candidate if current is None else max(current, candidate)


def _as_utc(value: Any) -> datetime | None:
    """Coerce whatever the aggregate handed back into an aware UTC datetime.

    ``min()``/``max()`` over a ``TypeDecorator``-backed column bypass
    ``process_result_value``, so this comes back as the stored ISO string rather
    than as a ``datetime``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
