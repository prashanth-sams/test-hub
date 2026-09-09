"""Storage interfaces.

The shipped implementation is SQLite plus a local filesystem (ADR-0001 D4). These
protocols are the seam where PostgreSQL, S3-compatible object storage and a real
message bus will land, and the reason nothing above them has to change when they
do. ``docs/scaling.md`` maps each protocol to what would implement it.

Everything is scoped by ``workspace_id``, including in the open-source edition
that only ever creates one workspace.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Any, Protocol, runtime_checkable

from test_hub.domain.events import DomainEvent
from test_hub.domain.models import (
    Artifact,
    OverviewSnapshot,
    ParserWarning,
    Project,
    RunComparison,
    RunDetail,
    RunSummary,
    TestDetail,
    TestRun,
    Workspace,
)
from test_hub.domain.status import RunStatus

__all__ = [
    "ArtifactStore",
    "ArtifactSweep",
    "EventBus",
    "EventRepository",
    "RunFilter",
    "RunRepository",
    "StoredArtifact",
    "UnitOfWork",
    "WriteResult",
]


@dataclass(slots=True)
class RunFilter:
    """The facets every run listing supports.

    One object rather than a dozen keyword arguments, because exactly the same
    filter is applied by the REST layer, the exporter and the demo seeder, and a
    new facet should be one field in one place.
    """

    workspace_id: uuid.UUID | None = None
    project_keys: Sequence[str] = ()
    teams: Sequence[str] = ()
    components: Sequence[str] = ()
    test_types: Sequence[str] = ()
    environments: Sequence[str] = ()
    branches: Sequence[str] = ()
    statuses: Sequence[RunStatus] = ()
    tags: Sequence[str] = ()
    search: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    live_only: bool = False
    limit: int = 50
    offset: int = 0

    def cache_key(self) -> str:
        """A stable string for this filter, for future caching layers."""
        parts = [
            str(self.workspace_id),
            ",".join(sorted(self.project_keys)),
            ",".join(sorted(self.teams)),
            ",".join(sorted(self.components)),
            ",".join(sorted(self.test_types)),
            ",".join(sorted(self.environments)),
            ",".join(sorted(self.branches)),
            ",".join(sorted(str(status) for status in self.statuses)),
            ",".join(sorted(self.tags)),
            self.search or "",
            self.since.isoformat() if self.since else "",
            self.until.isoformat() if self.until else "",
            str(self.live_only),
            f"{self.limit}:{self.offset}",
        ]
        return "|".join(parts)


@dataclass(slots=True)
class WriteResult:
    """What applying a batch of events changed.

    ``ignored`` is the idempotency signal: a replayed batch comes back with
    ``applied == 0`` and ``ignored == len(batch)``, and no counts move.
    """

    run_id: uuid.UUID
    applied: int = 0
    ignored: int = 0
    created_run: bool = False
    suites_touched: int = 0
    tests_touched: int = 0
    attempts_created: int = 0
    emitted: list[DomainEvent] = field(default_factory=list)


@dataclass(slots=True)
class StoredArtifact:
    """The result of putting bytes into the artifact store."""

    sha256: str
    size_bytes: int
    path: str
    created: bool


@dataclass(slots=True)
class ArtifactSweep:
    """What a garbage-collection pass over the blob store did.

    ``retained`` counts digests that were candidates but survived, either
    because a row still points at them — content addressing means one blob can
    back rows in many runs — or because they were written inside the grace
    window and an uncommitted import may still be about to claim them.
    """

    unlinked: int = 0
    bytes_freed: int = 0
    retained: int = 0

    def __add__(self, other: ArtifactSweep) -> ArtifactSweep:
        return ArtifactSweep(
            unlinked=self.unlinked + other.unlinked,
            bytes_freed=self.bytes_freed + other.bytes_freed,
            retained=self.retained + other.retained,
        )


@runtime_checkable
class RunRepository(Protocol):
    """Reads and writes runs, suites, tests and attempts."""

    def ensure_workspace(self, slug: str, name: str) -> Workspace: ...

    def get_workspace(self, workspace_id: uuid.UUID) -> Workspace | None: ...

    def ensure_project(self, workspace_id: uuid.UUID, key: str, **fields: Any) -> Project: ...

    def list_projects(self, workspace_id: uuid.UUID) -> list[Project]: ...

    def get_run(self, run_id: uuid.UUID) -> TestRun | None: ...

    def get_run_detail(self, run_id: uuid.UUID, *, comparison: bool = True) -> RunDetail | None: ...

    def list_runs(self, run_filter: RunFilter) -> list[RunSummary]: ...

    def count_runs(self, run_filter: RunFilter) -> int: ...

    def overview(self, workspace_id: uuid.UUID, *, window_days: int = 30) -> OverviewSnapshot: ...

    def get_test_detail(
        self, test_id: uuid.UUID, *, workspace_id: uuid.UUID, history: int = 30
    ) -> TestDetail | None: ...

    def compare_run(self, run_id: uuid.UUID) -> RunComparison | None: ...

    def facets(self, workspace_id: uuid.UUID) -> dict[str, list[str]]: ...

    def annotate_run(
        self,
        run_id: uuid.UUID,
        *,
        source_adapter: str | None = None,
        warnings: Sequence[ParserWarning] | None = None,
        name: str | None = None,
    ) -> None:
        """Attach import-time facts to a run that the event stream cannot carry.

        Which adapter parsed the file, and what it could not represent exactly,
        are properties of *this import* rather than of the test run itself, so
        they arrive after the events have been folded. Passing ``None`` for a
        field leaves it untouched; warnings are appended, not replaced, because
        one run can be built from several imported shards.
        """
        ...


@runtime_checkable
class EventRepository(Protocol):
    """The append-only event log, and the fold that turns it into rows.

    :meth:`apply_events` is the only write path in the system. It must be atomic
    and idempotent: re-applying an event already stored under the same
    ``(source, id)`` changes nothing and is counted as ignored.
    """

    def apply_events(
        self, events: Sequence[DomainEvent], *, idempotency_key: str | None = None
    ) -> WriteResult: ...

    def list_events(
        self, run_id: uuid.UUID, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[DomainEvent]: ...

    def next_sequence(self, run_id: uuid.UUID) -> int: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Content-addressed blob storage.

    The filesystem implementation writes ``<root>/<aa>/<bb>/<sha256>``; an S3
    implementation would use the same digest as the key, so the two are
    interchangeable without touching a caller.
    """

    def put(self, stream: IO[bytes], *, max_bytes: int) -> StoredArtifact: ...

    def open(self, sha256: str) -> IO[bytes]: ...

    def exists(self, sha256: str) -> bool: ...

    def delete(self, sha256: str) -> bool: ...

    def record(self, artifact: Artifact) -> Artifact: ...

    def list_for_run(self, run_id: uuid.UUID) -> list[Artifact]: ...

    def get(self, artifact_id: uuid.UUID) -> Artifact | None: ...

    def collect_unreferenced(
        self,
        digests: Iterable[str],
        *,
        grace_seconds: float,
        excluding_runs: Collection[uuid.UUID] = (),
        apply: bool = True,
    ) -> ArtifactSweep: ...


@runtime_checkable
class EventBus(Protocol):
    """Fan-out of applied events to live dashboard subscribers.

    In-process today (ADR-0001 D6). The ``last_event_id`` parameter is what makes
    a reconnect cheap; an implementation that cannot replay from a given id must
    say so by yielding a ``resync`` directive rather than silently skipping.
    """

    async def publish(self, workspace_id: uuid.UUID, events: Iterable[DomainEvent]) -> None: ...

    def subscribe(
        self, workspace_id: uuid.UUID, *, last_event_id: str | None = None
    ) -> AsyncIterator[Any]: ...

    @property
    def subscriber_count(self) -> int: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Groups repositories into one transactional scope."""

    runs: RunRepository
    events: EventRepository
    artifacts: ArtifactStore

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, *exc: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
