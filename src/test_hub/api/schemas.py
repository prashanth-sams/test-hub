"""Request and response bodies for ``/api/v1``.

These are the *public* contract — separate from the canonical domain models they
wrap, and separate again from the ORM (ADR-0001 D3). Responses mostly re-export
domain models unchanged; requests are their own thing, because what a publisher
is allowed to *send* is a much smaller surface than what Test Hub *stores*. You
cannot, for instance, post a run's counts: they are derived, and accepting them
would create two sources of truth for the number on the front page.

Every model forbids unknown fields. A publisher with a typo in a field name gets
a 422 naming the field rather than a silently ignored value and a wrong dashboard.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from test_hub.domain.events import DomainEvent
from test_hub.domain.models import (
    Artifact,
    ArtifactKind,
    ImportResult,
    Project,
    RunContext,
    RunCounts,
    RunSummary,
    TestRun,
    TestType,
    TriggerType,
    utcnow,
)
from test_hub.domain.status import RunStatus
from test_hub.version import SCHEMA_VERSION, __version__

__all__ = [
    "ArtifactResponse",
    "CreateRunRequest",
    "ErrorResponse",
    "EventsRequest",
    "EventsResponse",
    "FinishRunRequest",
    "HealthResponse",
    "ImportResponse",
    "ProjectListResponse",
    "PruneRequest",
    "PruneResponse",
    "RetentionSettingsResponse",
    "RunListResponse",
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ requests


class CreateRunRequest(_Request):
    """``POST /api/v1/runs``.

    ``run_id`` is optional and, when supplied, makes the call idempotent: a CI
    step that is retried announces the same run rather than creating a second.
    Publishers that can generate a UUID should always send one.
    """

    project_key: Annotated[str, Field(min_length=1, max_length=120)]
    run_id: uuid.UUID | None = None
    name: str | None = None
    team: str | None = None
    component: str | None = None
    test_type: TestType = TestType.OTHER
    trigger: TriggerType = TriggerType.UNKNOWN
    context: RunContext = Field(default_factory=RunContext)
    tags: list[str] = Field(default_factory=list, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_tests: int | None = Field(
        default=None, ge=0, description="Collected test count, if known before execution."
    )
    started: bool = Field(
        default=True, description="False to open the run as queued rather than running."
    )


class EventsRequest(_Request):
    """``POST /api/v1/runs/{run_id}/events``.

    Send ``sequence: 0`` on every event to have the server allocate a contiguous
    block; send your own monotonic numbers if you want gap detection, which you
    probably do for a long-running suite.
    """

    events: Annotated[list[DomainEvent], Field(min_length=1, max_length=5000)]


class FinishRunRequest(_Request):
    """``POST /api/v1/runs/{run_id}/finish``.

    ``status`` is advisory and only honoured for ``cancelled``; every other
    outcome is folded from the results that were actually reported.
    """

    finished_at: AwareDatetime | None = None
    status: RunStatus | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------- responses


class ImportResponse(_Response):
    """``POST /api/v1/imports``.

    Deliberately verbose: which adapter was chosen and how confident detection
    was are the first two questions asked when an import produces a surprise.
    """

    result: ImportResult


class RunListResponse(_Response):
    runs: list[RunSummary]
    total: int
    limit: int
    offset: int


class ProjectListResponse(_Response):
    projects: list[Project]


class EventsResponse(_Response):
    """What applying a batch did.

    ``ignored`` is how a publisher confirms idempotency worked: repost a batch
    and every event comes back ignored, with the counts unchanged.
    """

    run_id: uuid.UUID
    applied: int
    ignored: int
    created_run: bool
    counts: RunCounts
    status: RunStatus


class ArtifactResponse(_Response):
    artifact: Artifact
    deduplicated: bool = Field(
        default=False, description="True when identical content was already stored."
    )


class RunResponse(_Response):
    run: TestRun


class ArtifactListResponse(_Response):
    artifacts: list[Artifact]


class ErrorResponse(_Response):
    """The single error shape every failing endpoint returns."""

    error: str = Field(description="Stable machine-readable code, e.g. `unknown_format`.")
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class AdapterInfo(_Response):
    """One row of ``GET /api/v1/adapters``.

    Extra keys from an adapter's ``describe()`` are ignored rather than rejected:
    a third-party adapter is entitled to describe itself with fields this build
    has never heard of, and refusing the whole listing over one of them would
    make an unrelated plugin able to break the endpoint.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    display_name: str = ""
    description: str = ""
    priority: int = 0
    extensions: list[str] = Field(default_factory=list)
    content_types: list[str] = Field(default_factory=list)


class AdapterListResponse(_Response):
    adapters: list[AdapterInfo]


class HealthResponse(_Response):
    """``GET /healthz`` — cheap enough for a container healthcheck every 10s."""

    status: str = "ok"
    version: str = __version__
    schema_version: int = SCHEMA_VERSION
    database: str = "ok"
    workspace_id: uuid.UUID | None = None
    live_subscribers: int = 0
    time: AwareDatetime = Field(default_factory=utcnow)


ARTIFACT_KINDS = [kind.value for kind in ArtifactKind]


# ----------------------------------------------------------------- retention


class RetentionSettingsResponse(_Response):
    """What the dashboard needs to draw the retention panel.

    ``admin`` is about *this caller*, not about the server: it answers "may the
    person holding this page delete anything", which is the only question the UI
    can act on. A dashboard that drew a delete button and then got a 403 would be
    worse than one that never drew it.
    """

    admin: bool = Field(description="Whether this caller may run a prune.")
    retention_days: int | None = Field(
        default=None, description="The configured default cutoff, if any."
    )
    min_runs: int = Field(description="Runs always kept per project.")
    grace_hours: float = Field(description="How long a new artifact blob is protected.")
    units: dict[str, int] = Field(description="The duration grammar, in hours per unit.")


class PruneRequest(_Request):
    """A cutoff, expressed the way the caller prefers.

    Exactly one of ``older_than`` and ``before``. ``confirm`` is required for the
    destructive endpoint and ignored by the preview: an API that deletes a year
    of history on a request a client sent by accident is an API with one field
    missing.
    """

    older_than: str | None = Field(
        default=None,
        description="A duration: 90d, 6m, 2w, 1y, 12h, or a bare number of days.",
    )
    before: AwareDatetime | None = Field(
        default=None, description="An absolute cutoff. Runs at or before it are removed."
    )
    keep_min: int | None = Field(
        default=None, ge=0, description="Runs kept per project. Defaults to the server setting."
    )
    project_keys: list[str] = Field(default_factory=list, description="Restrict to these projects.")
    by_import_date: bool = Field(
        default=False, description="Cut on when the run was imported, not when it ran."
    )
    include_unfinished: bool = Field(
        default=False, description="Also remove queued and running runs past the cutoff."
    )
    confirm: bool = Field(default=False, description="Required by the destructive endpoint.")


class PruneResponse(_Response):
    """What a prune did, or — when ``dry_run`` — what it would do."""

    dry_run: bool
    cutoff: AwareDatetime
    runs: int
    rows: int
    deleted: dict[str, int]
    blobs_unlinked: int
    blobs_retained: int
    bytes_freed: int
    skipped_live: int
    protected: int
    batches: int
    vacuumed: bool = Field(
        default=False,
        description=(
            "Always false over HTTP. VACUUM rewrites the whole database behind an "
            "exclusive lock, which is not something a web request should start; run "
            "`test-hub prune --vacuum` on the host to reclaim the file space."
        ),
    )
    by_project: dict[str, int]
    oldest: AwareDatetime | None = None
    newest: AwareDatetime | None = None
