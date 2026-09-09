"""The canonical model — Test Hub's public wire contract.

Everything that crosses a boundary (HTTP body, adapter output) is one of these
models. Database rows are *never* serialized directly; see
ADR-0001 D3. Every top-level payload carries ``schema_version`` so a consumer
can refuse a shape it does not understand instead of guessing.

The hierarchy::

    Workspace
      └── Project
            └── TestRun            one execution of a suite of tests
                  ├── TestSuite    a file / class / feature grouping
                  │     └── TestCase        a test's identity within the run
                  │           └── TestAttempt   one execution of it (retries!)
                  ├── Artifact     a file produced by the run
                  └── Event        the append-only log the run was folded from

A retry is a new :class:`TestAttempt`, never a mutation of the previous one.
That is what makes flakiness observable at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    field_validator,
    model_validator,
)

from test_hub.domain.status import RunStatus, TestStatus
from test_hub.version import SCHEMA_VERSION

__all__ = [
    "Artifact",
    "ImportResult",
    "OverviewSnapshot",
    "ParserWarning",
    "Project",
    "RunCounts",
    "RunDetail",
    "RunSummary",
    "TestAttempt",
    "TestCase",
    "TestRun",
    "TestSuite",
    "TestType",
    "TriggerType",
    "Workspace",
    "utcnow",
]

# ------------------------------------------------------------------ helpers

Slug = Annotated[
    str,
    Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._\- ]*$"),
]
"""A human-facing key: project, team, component, environment, branch.

Deliberately permissive about case and spaces (real team names have both) and
deliberately strict about everything else, because these values end up in file
names, URLs and SQL ``LIKE`` patterns.
"""

ShortText = Annotated[str, Field(max_length=512)]
LongText = Annotated[str, Field(max_length=64_000)]


def utcnow() -> datetime:
    """Timezone-aware now, in UTC. The only clock the domain uses."""
    return datetime.now(UTC)


class _Base(BaseModel):
    """Shared configuration for every canonical model."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        ser_json_timedelta="float",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _ignore_own_computed_fields(cls, data: Any) -> Any:
        """Let a model round-trip its own serialized output.

        Computed fields (``pass_rate``, ``executed``, ``status`` on a test case…)
        are written out but derived, not stored — so with ``extra="forbid"`` a
        client that reads a response and validates it back into the same model
        would be rejected for sending fields it did not invent. Dropping exactly
        the computed names, and nothing else, keeps that round trip working
        while a genuine typo in a publisher's payload is still an error.
        """
        if isinstance(data, dict):
            computed = cls.model_computed_fields
            if computed and any(name in data for name in computed):
                return {key: value for key, value in data.items() if key not in computed}
        return data


class _VersionedBase(_Base):
    """A payload that is allowed to cross a version boundary on its own."""

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        description="Version of the Test Hub canonical schema this payload conforms to.",
    )

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value}; this build speaks {SCHEMA_VERSION}"
            )
        return value


# ------------------------------------------------------------- enumerations


class TestType(StrEnum):
    """What kind of testing produced the run.

    Open-ended in spirit but closed in the contract: the dashboard groups and
    colours by this, so it has to be a known set. ``OTHER`` is the escape hatch,
    and the free-text original is kept in ``TestRun.metadata['test_type_raw']``
    when an adapter had to fall back to it.
    """

    UNIT = "unit"
    COMPONENT = "component"
    INTEGRATION = "integration"
    CONTRACT = "contract"
    FUNCTIONAL = "functional"
    E2E = "e2e"
    API = "api"
    UI = "ui"
    PERFORMANCE = "performance"
    SECURITY = "security"
    ACCESSIBILITY = "accessibility"
    SMOKE = "smoke"
    REGRESSION = "regression"
    ACCEPTANCE = "acceptance"
    OTHER = "other"


class TriggerType(StrEnum):
    """What caused the run to happen."""

    MANUAL = "manual"
    PUSH = "push"
    PULL_REQUEST = "pull_request"
    SCHEDULE = "schedule"
    API = "api"
    RETRY = "retry"
    LOCAL = "local"
    UNKNOWN = "unknown"


class ArtifactKind(StrEnum):
    """What an attached file is, so the dashboard knows how to offer it."""

    SCREENSHOT = "screenshot"
    VIDEO = "video"
    LOG = "log"
    TRACE = "trace"
    REPORT = "report"
    COVERAGE = "coverage"
    RAW_REPORT = "raw_report"
    OTHER = "other"


class WarningSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"


# ---------------------------------------------------------------- warnings


class ParserWarning(_Base):
    """Something the input said that could not be represented exactly.

    Raised — never swallowed — whenever an adapter has to guess, drop or coerce.
    The import response carries every warning, and the dashboard shows them on
    the run, so an unmapped status or a truncated log is visible rather than
    silently changing the numbers.
    """

    code: Annotated[str, Field(pattern=r"^[a-z0-9_.]+$", max_length=64)]
    message: ShortText
    severity: WarningSeverity = WarningSeverity.WARNING
    location: ShortText | None = Field(
        default=None, description="Where in the input it happened, e.g. 'testsuite[2]/testcase[7]'."
    )
    detail: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------- workspace


class Workspace(_Base):
    """The top of the hierarchy and the tenancy boundary.

    The open-source edition creates exactly one workspace, ``default``. Every
    row in the database still carries its id — see ADR-0001 D9.
    """

    id: uuid.UUID
    slug: Slug
    name: ShortText
    created_at: AwareDatetime = Field(default_factory=utcnow)


class Project(_Base):
    """A unit of software whose tests are being tracked."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    key: Slug = Field(description="Stable, human-typed key used by publishers, e.g. payments-api.")
    name: ShortText
    description: ShortText | None = None
    default_team: Slug | None = None
    repository_url: HttpUrl | None = None
    tags: list[Slug] = Field(default_factory=list, max_length=64)
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)


# -------------------------------------------------------------------- counts


class RunCounts(_Base):
    """Aggregate counts for a run, a suite or a filtered slice.

    Always derived by folding attempts — never taken from a publisher's own
    totals, which drift the moment anything is retried or partially uploaded.
    A test contributes exactly once, through its *effective* attempt (the last
    one), so a test that failed and then passed on retry counts as one pass.

    See ``docs/metrics.md`` for the formulas, including the exact treatment of
    ``xfailed``/``xpassed``.
    """

    total: int = 0
    passed: int = 0
    failed: int = 0
    error: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    cancelled: int = 0
    unknown: int = 0
    running: int = 0
    queued: int = 0
    flaky: int = 0
    retried: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def completed(self) -> int:
        """Tests with a final outcome — the denominator that excludes in-flight work."""
        return (
            self.passed
            + self.failed
            + self.error
            + self.skipped
            + self.xfailed
            + self.xpassed
            + self.cancelled
            + self.unknown
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def executed(self) -> int:
        """Completed tests that actually ran — the pass-rate denominator."""
        return self.completed - self.skipped - self.cancelled

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_rate(self) -> float | None:
        """``(passed + xfailed) / executed``, as a percentage, or ``None``.

        ``None`` — not ``0.0`` — when nothing executed, so the dashboard can
        show "—" instead of a red 0%% for a run that only skipped.
        """
        if self.executed <= 0:
            return None
        return round(100.0 * (self.passed + self.xfailed) / self.executed, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failure_rate(self) -> float | None:
        """``(failed + error + xpassed) / executed``, as a percentage, or ``None``."""
        if self.executed <= 0:
            return None
        return round(100.0 * (self.failed + self.error + self.xpassed) / self.executed, 2)

    def add(self, status: TestStatus, *, count: int = 1) -> None:
        """Fold one effective attempt status into the counts."""
        self.total += count
        attribute = {
            TestStatus.PASSED: "passed",
            TestStatus.FAILED: "failed",
            TestStatus.ERROR: "error",
            TestStatus.SKIPPED: "skipped",
            TestStatus.XFAILED: "xfailed",
            TestStatus.XPASSED: "xpassed",
            TestStatus.CANCELLED: "cancelled",
            TestStatus.UNKNOWN: "unknown",
            TestStatus.RUNNING: "running",
            TestStatus.QUEUED: "queued",
        }[status]
        setattr(self, attribute, getattr(self, attribute) + count)

    def merge(self, other: RunCounts) -> RunCounts:
        """Sum two count blocks (used to roll suites up into a run)."""
        return RunCounts(
            **{name: getattr(self, name) + getattr(other, name) for name in RunCounts.model_fields}
        )


# --------------------------------------------------------------- attempts


class FailureDetail(_Base):
    """Why an attempt did not pass."""

    type: ShortText | None = Field(default=None, description="Exception or assertion class.")
    message: LongText | None = None
    stack_trace: LongText | None = None
    expected: LongText | None = None
    actual: LongText | None = None
    truncated: bool = Field(
        default=False, description="True when the message or trace was cut to fit the limits."
    )


class TestAttempt(_Base):
    """One execution of one test.

    A retry produces a new attempt with ``attempt_number`` incremented; the
    earlier attempt is preserved exactly as it was recorded. Nothing in Test Hub
    ever overwrites an attempt.
    """

    id: uuid.UUID
    test_id: uuid.UUID = Field(description="Stable test identity — see test_hub.domain.identity.")
    run_id: uuid.UUID
    attempt_number: Annotated[int, Field(ge=1)] = 1
    status: TestStatus
    original_status: ShortText | None = Field(
        default=None, description="Exactly what the source report called it, before normalization."
    )
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    failure: FailureDetail | None = None
    stdout: LongText | None = None
    stderr: LongText | None = None
    logs: LongText | None = None
    skip_reason: ShortText | None = None
    worker: ShortText | None = Field(default=None, description="xdist worker / shard, if any.")
    artifact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent_timing(self) -> TestAttempt:
        """Derive whichever of the three timing fields is missing, if it can be."""
        if self.duration_ms is None and self.started_at and self.finished_at:
            delta = (self.finished_at - self.started_at).total_seconds() * 1000.0
            object.__setattr__(self, "duration_ms", max(0, round(delta)))
        return self


class TestCase(_Base):
    """A test's identity within one run, plus every attempt it made.

    ``id`` is the stable identity (:func:`test_hub.domain.identity.test_identity`)
    and is the same value in every run that executed this test — that is what
    history, comparison and flakiness are keyed on.
    """

    id: uuid.UUID
    run_id: uuid.UUID
    suite_id: uuid.UUID
    name: ShortText
    class_name: ShortText | None = None
    suite_path: ShortText
    params: ShortText | None = Field(default=None, description="Canonical normalized parameters.")
    full_name: ShortText | None = Field(
        default=None, description="Human-readable identifier as the framework prints it."
    )
    file: ShortText | None = None
    owner: ShortText | None = None
    tags: list[Slug] = Field(default_factory=list, max_length=64)
    markers: list[ShortText] = Field(default_factory=list, max_length=64)
    attempts: list[TestAttempt] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> TestStatus:
        """The effective status: the outcome of the last attempt."""
        if not self.attempts:
            return TestStatus.UNKNOWN
        return self.attempts[-1].status

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_ms(self) -> int:
        """Total time across every attempt — retries are work the run paid for."""
        return sum(attempt.duration_ms or 0 for attempt in self.attempts)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def flaky(self) -> bool:
        """True when this test both failed and succeeded within this one run.

        The strict, within-run definition. The looser across-runs definition
        lives in :mod:`test_hub.domain.metrics`, which needs history to compute.
        """
        from test_hub.domain.metrics import is_flaky_within_run

        return is_flaky_within_run([attempt.status for attempt in self.attempts])


class TestSuite(_Base):
    """A grouping of tests — a file, a class, a feature, whatever the framework groups by."""

    id: uuid.UUID
    run_id: uuid.UUID
    name: ShortText
    path: ShortText
    parent_id: uuid.UUID | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    counts: RunCounts = Field(default_factory=RunCounts)
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------- artifacts


class Artifact(_Base):
    """A file produced by a run — a screenshot, a log, a trace, the raw report.

    Stored content-addressed: ``sha256`` is the storage key, so uploading the
    same screenshot from a hundred shards costs one copy.
    """

    id: uuid.UUID
    run_id: uuid.UUID
    test_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    kind: ArtifactKind = ArtifactKind.OTHER
    name: ShortText
    content_type: ShortText = "application/octet-stream"
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    url: ShortText | None = Field(default=None, description="Where the API serves it from.")
    created_at: AwareDatetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


# -------------------------------------------------------------------- runs


class RunContext(_Base):
    """Where a run came from. Every field is optional; publishers know varying amounts."""

    framework: ShortText | None = Field(default=None, description="pytest, JUnit 5, Jest, ...")
    framework_version: ShortText | None = None
    language: ShortText | None = Field(default=None, description="python, java, javascript, ...")
    language_version: ShortText | None = None
    environment: Slug | None = Field(default=None, description="staging, prod, local, ...")
    branch: ShortText | None = None
    commit_sha: Annotated[str, Field(max_length=64)] | None = None
    commit_message: ShortText | None = None
    pipeline_id: ShortText | None = Field(default=None, description="Build / job identifier.")
    pipeline_url: HttpUrl | None = None
    ci_provider: ShortText | None = None
    hostname: ShortText | None = None
    os: ShortText | None = None
    executor: ShortText | None = Field(default=None, description="Who or what started it.")


class TestRun(_VersionedBase):
    """One execution of a set of tests. The central object of the whole product."""

    id: uuid.UUID = Field(description="Stable UUID, chosen by the publisher or the server.")
    workspace_id: uuid.UUID
    project_key: Slug
    name: ShortText | None = None
    team: Slug | None = None
    component: Slug | None = Field(default=None, description="Service or component under test.")
    test_type: TestType = TestType.OTHER
    trigger: TriggerType = TriggerType.UNKNOWN
    status: RunStatus = RunStatus.QUEUED
    context: RunContext = Field(default_factory=RunContext)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    counts: RunCounts = Field(default_factory=RunCounts)
    tags: list[Slug] = Field(default_factory=list, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[ParserWarning] = Field(default_factory=list, max_length=1000)
    source_adapter: ShortText | None = Field(
        default=None,
        description="Adapter that produced this run, when it came from a file.",
    )
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_live(self) -> bool:
        """True while the run is still expected to produce events."""
        return self.status in (RunStatus.QUEUED, RunStatus.RUNNING)


class RunSummary(_Base):
    """The compact form of a run used in lists — no suites, no tests, no traces."""

    id: uuid.UUID
    project_key: Slug
    name: ShortText | None = None
    team: Slug | None = None
    component: Slug | None = None
    test_type: TestType = TestType.OTHER
    trigger: TriggerType = TriggerType.UNKNOWN
    status: RunStatus = RunStatus.QUEUED
    environment: Slug | None = None
    branch: ShortText | None = None
    commit_sha: str | None = None
    framework: ShortText | None = None
    language: ShortText | None = None
    pipeline_url: HttpUrl | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_ms: int | None = None
    counts: RunCounts = Field(default_factory=RunCounts)
    tags: list[Slug] = Field(default_factory=list)
    is_live: bool = False


class RunComparison(_Base):
    """A run measured against the previous comparable run.

    "Comparable" means: same project, team, component, test type, environment and
    branch — the narrowest slice that makes the comparison honest. Fields are
    ``None`` when there is no previous run to compare against.
    """

    baseline_run_id: uuid.UUID | None = None
    baseline_started_at: AwareDatetime | None = None
    pass_rate_delta: float | None = None
    duration_delta_ms: int | None = None
    total_delta: int | None = None
    newly_failing: list[uuid.UUID] = Field(default_factory=list)
    newly_passing: list[uuid.UUID] = Field(default_factory=list)
    still_failing: list[uuid.UUID] = Field(default_factory=list)
    added: list[uuid.UUID] = Field(default_factory=list)
    removed: list[uuid.UUID] = Field(default_factory=list)


class RunDetail(_VersionedBase):
    """A run with everything the run page needs, in one payload."""

    run: TestRun
    suites: list[TestSuite] = Field(default_factory=list)
    tests: list[TestCase] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    comparison: RunComparison | None = None


class TestHistoryPoint(_Base):
    """One test's outcome in one run — the row of a test's history table."""

    run_id: uuid.UUID
    started_at: AwareDatetime | None = None
    status: TestStatus
    duration_ms: int | None = None
    attempts: int = 1
    branch: ShortText | None = None
    environment: Slug | None = None


class TestDetail(_VersionedBase):
    """Everything known about one stable test identity, across runs."""

    test_id: uuid.UUID
    project_key: Slug
    name: ShortText
    class_name: ShortText | None = None
    suite_path: ShortText
    params: ShortText | None = None
    full_name: ShortText | None = None
    owner: ShortText | None = None
    tags: list[Slug] = Field(default_factory=list)
    latest: TestCase | None = None
    history: list[TestHistoryPoint] = Field(default_factory=list)
    flake_rate: float | None = Field(
        default=None, description="Percentage of recent runs in which this test was flaky."
    )
    failure_rate: float | None = None
    average_duration_ms: int | None = None


# ---------------------------------------------------------------- overview


class TrendPoint(_Base):
    """One bucket of a trend line."""

    bucket: ShortText = Field(description="ISO date or datetime identifying the bucket.")
    runs: int = 0
    pass_rate: float | None = None
    failure_rate: float | None = None
    duration_ms_p50: int | None = None
    duration_ms_p95: int | None = None
    total: int = 0
    failed: int = 0


class ProjectHealth(_Base):
    """A project's line on the executive overview."""

    project_key: Slug
    name: ShortText
    team: Slug | None = None
    runs_24h: int = 0
    last_run_id: uuid.UUID | None = None
    last_run_at: AwareDatetime | None = None
    last_status: RunStatus = RunStatus.UNKNOWN
    pass_rate: float | None = None
    pass_rate_delta: float | None = None
    flaky_tests: int = 0
    live_runs: int = 0


class OverviewSnapshot(_VersionedBase):
    """The portfolio view: totals, trends, project health and what is running now.

    Deliberately separate from :class:`RunDetail`: the overview is the
    executive-altitude answer and must stay cheap enough to poll, while the run
    detail is the engineering drill-down and is allowed to be large.
    """

    generated_at: AwareDatetime = Field(default_factory=utcnow)
    window_days: int = 30
    counts: RunCounts = Field(default_factory=RunCounts)
    total_runs: int = Field(default=0, description="Runs that started inside `window_days`.")
    total_runs_all_time: int = Field(
        default=0,
        description="Runs in the workspace regardless of the window. When this is non-zero "
        "and `total_runs` is zero, every run is older than the window — the dashboard says "
        "so rather than showing an empty state that looks like an empty installation.",
    )
    live_runs: list[RunSummary] = Field(default_factory=list)
    recent_runs: list[RunSummary] = Field(
        default_factory=list,
        description="The newest runs regardless of the window, so a dashboard opened on a "
        "quiet workspace still has something to show.",
    )
    projects: list[ProjectHealth] = Field(default_factory=list)
    trend: list[TrendPoint] = Field(default_factory=list)
    top_failing_tests: list[TestDetail] = Field(default_factory=list)
    flakiest_tests: list[TestDetail] = Field(default_factory=list)
    filters: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Distinct values available for each facet: teams, components, types, ...",
    )


# ----------------------------------------------------------------- results


class ImportResult(_VersionedBase):
    """What an import returns — the contract the CLI, SDK and REST all speak."""

    run_id: uuid.UUID
    project_key: Slug
    adapter: ShortText = Field(description="Name of the adapter that parsed the input.")
    format: ShortText | None = Field(default=None, description="Detected format identifier.")
    detection_confidence: float | None = None
    created: bool = Field(
        default=True, description="False when this import updated an existing run."
    )
    counts: RunCounts = Field(default_factory=RunCounts)
    suites_imported: int = 0
    tests_imported: int = 0
    attempts_imported: int = 0
    events_applied: int = 0
    events_ignored: int = Field(
        default=0, description="Events skipped because they had already been applied."
    )
    warnings: list[ParserWarning] = Field(default_factory=list)
    duration_ms: int | None = None
