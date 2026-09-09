"""The event contract — a CloudEvents envelope carrying CDEvents-shaped test facts.

Every write into Test Hub becomes one of these, whether it arrived as a JUnit XML
file, a REST call from a Jenkins job, or a live publisher streaming results as
they happen (ADR-0001 D2). Folding events into rows is the only way state changes,
which is what makes reposting an event a no-op and a retry a first-class fact
rather than an overwrite.

**Envelope.** The outer shape is CloudEvents 1.0 (``specversion``, ``id``,
``source``, ``type``, ``time``, ``datacontenttype``, ``subject``) with three
Test Hub extension attributes: ``workspaceid``, ``runid`` and ``sequence``.
CloudEvents extension names must be lowercase alphanumerics, which is why they
are not spelled ``run_id``; the aliases let you write either.

**Types.** Named after CDEvents' test-run vocabulary, shortened to the part that
matters here::

    run.queued  run.started  suite.started  test.started
    test.finished  suite.finished  artifact.published  run.finished

**Ordering.** ``sequence`` is a monotonically increasing integer *within a run*,
assigned by the publisher. It is what lets a consumer detect a gap, and what lets
the server apply out-of-order arrivals deterministically. It is not global and
not a timestamp.

**Idempotency.** ``(source, id)`` is unique. Reposting is accepted and ignored,
and the response says how many events were ignored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from test_hub.domain.models import (
    ArtifactKind,
    FailureDetail,
    RunContext,
    TestType,
    TriggerType,
    utcnow,
)
from test_hub.domain.status import RunStatus, TestStatus
from test_hub.version import CLOUDEVENTS_SPEC_VERSION, SCHEMA_VERSION

__all__ = [
    "ArtifactPublishedData",
    "DomainEvent",
    "EventBatch",
    "EventType",
    "RunFinishedData",
    "RunQueuedData",
    "RunStartedData",
    "SuiteFinishedData",
    "SuiteStartedData",
    "TestFinishedData",
    "TestStartedData",
    "make_event",
]

DEFAULT_SOURCE = "https://test-hub.dev/source/unknown"

# `specversion` below is annotated `Literal["1.0"]` so that a wrong value is a
# type error rather than a runtime surprise, which means the literal has to be
# written out. This keeps it tied to the constant everything else reads.
assert CLOUDEVENTS_SPEC_VERSION == "1.0", (
    "CLOUDEVENTS_SPEC_VERSION changed; update the Literal on DomainEvent.specversion "
    "and bump SCHEMA_VERSION — the envelope shape is part of the wire contract"
)


class EventType(StrEnum):
    """The closed set of event types. Adding one is a schema-version change."""

    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    SUITE_STARTED = "suite.started"
    TEST_STARTED = "test.started"
    TEST_FINISHED = "test.finished"
    SUITE_FINISHED = "suite.finished"
    ARTIFACT_PUBLISHED = "artifact.published"
    RUN_FINISHED = "run.finished"


class _Data(BaseModel):
    """Base for event payloads."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


# ------------------------------------------------------------- payloads


class RunQueuedData(_Data):
    """A run exists and is expected to start.

    Carries every piece of run metadata, because this is often the only event a
    publisher sends before results start arriving, and the dashboard should be
    able to show the run — project, branch, pipeline — while it is still empty.
    """

    project_key: str
    name: str | None = None
    team: str | None = None
    component: str | None = None
    test_type: TestType = TestType.OTHER
    trigger: TriggerType = TriggerType.UNKNOWN
    context: RunContext = Field(default_factory=RunContext)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_tests: int | None = Field(
        default=None, description="Collected test count, if the framework knows it up front."
    )


class RunStartedData(_Data):
    """Execution has begun."""

    started_at: AwareDatetime
    project_key: str | None = None
    context: RunContext | None = None


class SuiteStartedData(_Data):
    suite_id: uuid.UUID
    name: str
    path: str
    parent_id: uuid.UUID | None = None
    started_at: AwareDatetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SuiteFinishedData(_Data):
    suite_id: uuid.UUID
    finished_at: AwareDatetime | None = None
    duration_ms: int | None = None


class TestStartedData(_Data):
    """A test attempt has begun.

    ``test_id`` is the stable identity. Publishers that cannot compute it may
    omit it and supply the identity components instead; the server derives it
    with :func:`test_hub.domain.identity.test_identity`.
    """

    test_id: uuid.UUID | None = None
    suite_id: uuid.UUID
    name: str
    class_name: str | None = None
    suite_path: str
    params: str | None = None
    full_name: str | None = None
    file: str | None = None
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)
    markers: list[str] = Field(default_factory=list)
    attempt_number: Annotated[int, Field(ge=1)] = 1
    started_at: AwareDatetime | None = None
    worker: str | None = None


class TestFinishedData(_Data):
    """A test attempt has produced a result.

    A retry is a second ``test.finished`` with ``attempt_number`` incremented —
    never a correction of the first.
    """

    test_id: uuid.UUID | None = None
    suite_id: uuid.UUID
    name: str
    class_name: str | None = None
    suite_path: str
    params: str | None = None
    full_name: str | None = None
    file: str | None = None
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)
    markers: list[str] = Field(default_factory=list)
    attempt_number: Annotated[int, Field(ge=1)] = 1
    status: TestStatus
    original_status: str | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    failure: FailureDetail | None = None
    stdout: str | None = None
    stderr: str | None = None
    logs: str | None = None
    skip_reason: str | None = None
    worker: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactPublishedData(_Data):
    """A file produced by the run has been stored.

    The bytes travel over the artifact endpoint; this event records the fact and
    the content address, so an artifact uploaded twice is one stored object.
    """

    artifact_id: uuid.UUID
    name: str
    kind: ArtifactKind = ArtifactKind.OTHER
    content_type: str = "application/octet-stream"
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    test_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunFinishedData(_Data):
    """The run is over.

    ``status`` is advisory: the server folds the attempts and uses the publisher's
    value only to distinguish ``cancelled`` from a derived pass/fail, or when no
    tests were reported at all.
    """

    finished_at: AwareDatetime
    status: RunStatus | None = None
    duration_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


EventData = (
    RunQueuedData
    | RunStartedData
    | SuiteStartedData
    | TestStartedData
    | TestFinishedData
    | SuiteFinishedData
    | ArtifactPublishedData
    | RunFinishedData
)

#: Which payload model belongs to which event type. Used to validate ``data``
#: after the envelope has been parsed, so a bad payload names its event type.
DATA_MODELS: dict[EventType, type[_Data]] = {
    EventType.RUN_QUEUED: RunQueuedData,
    EventType.RUN_STARTED: RunStartedData,
    EventType.SUITE_STARTED: SuiteStartedData,
    EventType.TEST_STARTED: TestStartedData,
    EventType.TEST_FINISHED: TestFinishedData,
    EventType.SUITE_FINISHED: SuiteFinishedData,
    EventType.ARTIFACT_PUBLISHED: ArtifactPublishedData,
    EventType.RUN_FINISHED: RunFinishedData,
}


# ------------------------------------------------------------- envelope


class DomainEvent(BaseModel):
    """A CloudEvents 1.0 envelope around one Test Hub fact.

    >>> event = make_event(
    ...     EventType.RUN_STARTED,
    ...     run_id=uuid.UUID(int=1),
    ...     workspace_id=uuid.UUID(int=2),
    ...     sequence=1,
    ...     data=RunStartedData(started_at=utcnow()),
    ...     source="urn:test-hub:cli",
    ... )
    >>> event.type
    <EventType.RUN_STARTED: 'run.started'>
    >>> event.specversion
    '1.0'
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    # --- CloudEvents core attributes
    specversion: Literal["1.0"] = "1.0"
    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        description="Globally unique event id. Unique together with `source`.",
    )
    source: Annotated[str, Field(min_length=1, max_length=512)] = DEFAULT_SOURCE
    type: EventType
    time: AwareDatetime = Field(default_factory=utcnow)
    datacontenttype: str = "application/json"
    subject: str | None = None

    # --- Test Hub extension attributes.
    #
    # CloudEvents requires extension attribute names to be lowercase
    # alphanumerics, so the field name — and therefore what goes on the wire — is
    # `workspaceid`, not `workspace_id`. `validation_alias` lets a Python caller
    # (and a hand-written JSON body) use the readable spelling on the way in;
    # only one spelling ever comes out.
    schemaversion: int = Field(
        default=SCHEMA_VERSION,
        validation_alias=AliasChoices("schemaversion", "schema_version"),
    )
    workspaceid: uuid.UUID = Field(validation_alias=AliasChoices("workspaceid", "workspace_id"))
    runid: uuid.UUID = Field(validation_alias=AliasChoices("runid", "run_id"))
    sequence: Annotated[int, Field(ge=0)] = Field(
        description="Monotonic sequence number within the run. Not global, not a clock."
    )

    data: dict[str, Any] = Field(default_factory=dict)

    # Readable aliases, so Python code never has to type `workspaceid`.
    @property
    def workspace_id(self) -> uuid.UUID:
        return self.workspaceid

    @property
    def run_id(self) -> uuid.UUID:
        return self.runid

    @property
    def schema_version(self) -> int:
        return self.schemaversion

    @field_validator("schemaversion")
    @classmethod
    def _known_version(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value}; this build speaks {SCHEMA_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def _payload_matches_type(self) -> DomainEvent:
        """Validate ``data`` against the model for this event type.

        Kept as a validated dict rather than a discriminated union so the event
        can be stored and replayed verbatim; the typed view is available through
        :meth:`payload`.
        """
        DATA_MODELS[self.type].model_validate(self.data)
        return self

    def payload(self) -> _Data:
        """The typed payload for this event."""
        return DATA_MODELS[self.type].model_validate(self.data)

    def dedupe_key(self) -> str:
        """The value the store enforces uniqueness on."""
        return f"{self.source}\x1f{self.id}"


class EventBatch(BaseModel):
    """A batch of events posted together for one run.

    Applied atomically: either every new event in the batch lands or none does,
    so a partially-applied batch can never leave counts half-updated.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    events: Annotated[list[DomainEvent], Field(min_length=1, max_length=5000)]

    @model_validator(mode="after")
    def _one_run(self) -> EventBatch:
        run_ids = {event.runid for event in self.events}
        if len(run_ids) > 1:
            raise ValueError("every event in a batch must belong to the same run")
        return self


def make_event(
    event_type: EventType,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    sequence: int,
    data: _Data | dict[str, Any],
    source: str = DEFAULT_SOURCE,
    event_id: uuid.UUID | None = None,
    time: datetime | None = None,
    subject: str | None = None,
) -> DomainEvent:
    """Build a valid :class:`DomainEvent`. The one constructor adapters should use."""
    payload = data if isinstance(data, dict) else data.model_dump(mode="json", exclude_none=False)
    # Built from a mapping rather than keyword arguments: the extension fields
    # are named `workspaceid`/`runid` on the wire and accept the readable
    # spelling only as a validation alias, which keyword arguments cannot express
    # to a type checker.
    return DomainEvent.model_validate(
        {
            "id": event_id or uuid.uuid4(),
            "source": source,
            "type": event_type,
            "time": time or utcnow(),
            "subject": subject,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "sequence": sequence,
            "data": payload,
        }
    )
