"""Live publishing — creating a run and streaming results into it.

The other half of ingestion. :mod:`test_hub.service.imports` handles "here is a
finished report file"; this module handles "here is a run that is happening right
now, and here are its results as they arrive". Both end at the same
``apply_events`` call, so a live-published run and an imported one are
indistinguishable once stored — which is exactly the property that lets the
dashboard show them side by side.

Sequence numbers are the subtle part. A publisher may assign its own (and should:
it is the only way to detect a gap), but many will not bother. When a batch
arrives with sequence 0 everywhere, we allocate a contiguous block from the run's
current high-water mark inside the same transaction, so two publishers posting
concurrently cannot be given the same numbers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import IO, Any

from test_hub.domain.events import (
    DomainEvent,
    EventType,
    RunFinishedData,
    RunQueuedData,
    RunStartedData,
    make_event,
)
from test_hub.domain.models import (
    Artifact,
    ArtifactKind,
    RunContext,
    TestRun,
    TestType,
    TriggerType,
    utcnow,
)
from test_hub.domain.status import RunStatus
from test_hub.errors import ConflictError, NotFoundError, ValidationError
from test_hub.logging import get_logger
from test_hub.service.imports import read_bounded
from test_hub.service.runtime import AppContext
from test_hub.storage.base import WriteResult

__all__ = ["ArtifactUpload", "RunService", "RunSpec"]

logger = get_logger(__name__)

#: Characters that must never reach the filesystem or a Content-Disposition
#: header, whatever a publisher calls its screenshot.
_UNSAFE_NAME = set('/\\\x00\r\n"')
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *{f"com{n}" for n in range(1, 10)},
    *{f"lpt{n}" for n in range(1, 10)},
}


@dataclass(slots=True)
class RunSpec:
    """What a publisher says when it opens a run."""

    project_key: str
    run_id: uuid.UUID | None = None
    name: str | None = None
    team: str | None = None
    component: str | None = None
    test_type: TestType = TestType.OTHER
    trigger: TriggerType = TriggerType.UNKNOWN
    context: RunContext | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    expected_tests: int | None = None
    started: bool = True
    source: str = "urn:test-hub:api"


@dataclass(slots=True)
class ArtifactUpload:
    """A file being attached to a run."""

    name: str
    stream: IO[bytes]
    kind: ArtifactKind = ArtifactKind.OTHER
    content_type: str = "application/octet-stream"
    test_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    metadata: dict[str, Any] | None = None


def safe_artifact_name(name: str) -> str:
    """Reduce a publisher-supplied filename to something safe to store and serve.

    Artifacts are stored content-addressed, so this name is only ever a *label*
    — but it is a label we put in a ``Content-Disposition`` header and show in a
    browser, so it still has to be free of separators, traversal, control
    characters and Windows device names.
    """
    cleaned = "".join(
        character for character in name.strip() if character not in _UNSAFE_NAME
    ).replace("..", "_")
    cleaned = "".join(character if character.isprintable() else "_" for character in cleaned).strip(
        ". "
    )
    if not cleaned:
        return "artifact"
    if cleaned.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:255]


class RunService:
    """Create runs, accept events for them, close them, attach files to them."""

    def __init__(self, context: AppContext) -> None:
        self._context = context
        self._settings = context.settings

    # ------------------------------------------------------------- create

    def create_run(self, spec: RunSpec) -> TestRun:
        """Open a run and return it. Idempotent on ``run_id``.

        Re-posting the same run id returns the existing run rather than
        erroring: a retried CI step should not fail because the first attempt
        already announced the run.
        """
        run_id = spec.run_id or uuid.uuid4()
        with self._context.uow_factory(read_only=True) as uow:
            existing = uow.runs.get_run(run_id)
            if existing is not None:
                logger.debug("run %s already exists; returning it unchanged", run_id)
                return existing

        now = utcnow()
        events = [
            make_event(
                EventType.RUN_QUEUED,
                run_id=run_id,
                workspace_id=self._context.workspace_id,
                sequence=1,
                source=spec.source,
                time=now,
                data=RunQueuedData(
                    project_key=spec.project_key,
                    name=spec.name,
                    team=spec.team,
                    component=spec.component,
                    test_type=spec.test_type,
                    trigger=spec.trigger,
                    context=spec.context or RunContext(),
                    tags=list(spec.tags or []),
                    metadata=dict(spec.metadata or {}),
                    expected_tests=spec.expected_tests,
                ),
            )
        ]
        if spec.started:
            events.append(
                make_event(
                    EventType.RUN_STARTED,
                    run_id=run_id,
                    workspace_id=self._context.workspace_id,
                    sequence=2,
                    source=spec.source,
                    time=now,
                    data=RunStartedData(started_at=now, project_key=spec.project_key),
                )
            )

        with self._context.uow_factory() as uow:
            result = uow.events.apply_events(events)
            uow.commit()
        self._context.bus.publish_nowait(self._context.workspace_id, result.emitted)

        with self._context.uow_factory(read_only=True) as uow:
            run = uow.runs.get_run(run_id)
        if run is None:  # pragma: no cover - would mean apply_events lied
            raise ConflictError(f"run {run_id} was not created")
        logger.info("run %s opened for project %s", run_id, spec.project_key)
        return run

    # -------------------------------------------------------------- events

    def post_events(
        self,
        run_id: uuid.UUID,
        events: list[DomainEvent],
        *,
        idempotency_key: str | None = None,
        allow_unknown_run: bool = False,
    ) -> WriteResult:
        """Apply a batch of events to a run.

        ``allow_unknown_run`` exists for the one legitimate race: a publisher
        that posts ``run.queued`` and the first results in the same batch. Every
        other caller wants the 404, because silently inventing a run from a typo
        in a URL is how you get orphaned data.
        """
        if not events:
            raise ValidationError("no events were supplied")
        if len(events) > self._settings.max_events_per_batch:
            raise ValidationError(
                f"batch of {len(events)} events exceeds the limit of "
                f"{self._settings.max_events_per_batch}"
            )

        mismatched = {event.run_id for event in events} - {run_id}
        if mismatched:
            raise ValidationError(
                "every event in the batch must carry the run id from the URL",
                detail={"url_run_id": str(run_id), "event_run_ids": sorted(map(str, mismatched))},
            )

        workspace_id = self._context.workspace_id
        foreign = {event.workspace_id for event in events} - {workspace_id}
        if foreign:
            raise ValidationError(
                "events belong to a different workspace",
                detail={"expected": str(workspace_id)},
            )

        with self._context.uow_factory() as uow:
            run = uow.runs.get_run(run_id)
            opens_run = any(event.type is EventType.RUN_QUEUED for event in events)
            if run is None and not (allow_unknown_run or opens_run):
                raise NotFoundError(f"run {run_id} does not exist")

            numbered = self._assign_sequences(uow, run_id, events)
            result = uow.events.apply_events(numbered, idempotency_key=idempotency_key)
            uow.commit()

        self._context.bus.publish_nowait(workspace_id, result.emitted)
        logger.debug(
            "run %s: %s events applied, %s ignored", run_id, result.applied, result.ignored
        )
        return result

    def _assign_sequences(
        self, uow: Any, run_id: uuid.UUID, events: list[DomainEvent]
    ) -> list[DomainEvent]:
        """Fill in sequence numbers the publisher did not supply.

        Done inside the caller's transaction, against the run's current
        high-water mark, so two publishers posting at the same moment get
        disjoint blocks instead of both starting at 1.
        """
        if all(event.sequence > 0 for event in events):
            return events
        cursor = uow.events.next_sequence(run_id)
        numbered: list[DomainEvent] = []
        for event in events:
            if event.sequence > 0:
                numbered.append(event)
                continue
            numbered.append(event.model_copy(update={"sequence": cursor}))
            cursor += 1
        return numbered

    # -------------------------------------------------------------- finish

    def finish_run(
        self,
        run_id: uuid.UUID,
        *,
        status: RunStatus | None = None,
        finished_at: Any = None,
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "urn:test-hub:api",
    ) -> TestRun:
        """Close a run. The final status is folded from the attempts, not trusted.

        A publisher's ``status`` is honoured only for ``cancelled`` — the one
        outcome that cannot be derived from the results, because a cancelled run
        looks exactly like a run that simply stopped reporting.
        """
        with self._context.uow_factory(read_only=True) as uow:
            run = uow.runs.get_run(run_id)
            if run is None:
                raise NotFoundError(f"run {run_id} does not exist")
            sequence = uow.events.next_sequence(run_id)

        event = make_event(
            EventType.RUN_FINISHED,
            run_id=run_id,
            workspace_id=self._context.workspace_id,
            sequence=sequence,
            source=source,
            data=RunFinishedData(
                finished_at=finished_at or utcnow(),
                status=status,
                duration_ms=duration_ms,
                metadata=dict(metadata or {}),
            ),
        )
        with self._context.uow_factory() as uow:
            result = uow.events.apply_events([event])
            uow.commit()
        self._context.bus.publish_nowait(self._context.workspace_id, result.emitted)

        with self._context.uow_factory(read_only=True) as uow:
            finished = uow.runs.get_run(run_id)
        if finished is None:  # pragma: no cover
            raise NotFoundError(f"run {run_id} disappeared while finishing")
        logger.info("run %s finished as %s", run_id, finished.status)
        return finished

    # ------------------------------------------------------------ artifacts

    def add_artifact(self, run_id: uuid.UUID, upload: ArtifactUpload) -> Artifact:
        """Store a file against a run and announce it as an event."""
        with self._context.uow_factory(read_only=True) as uow:
            if uow.runs.get_run(run_id) is None:
                raise NotFoundError(f"run {run_id} does not exist")

        buffer = read_bounded(upload.stream, self._settings.max_artifact_bytes, what="artifact")
        size = buffer.getbuffer().nbytes
        if size == 0:
            raise ValidationError("the uploaded artifact is empty")

        name = safe_artifact_name(upload.name)
        artifact_id = uuid.uuid4()

        with self._context.uow_factory() as uow:
            stored = uow.artifacts.put(buffer, max_bytes=self._settings.max_artifact_bytes)
            artifact = Artifact(
                id=artifact_id,
                run_id=run_id,
                test_id=upload.test_id,
                attempt_id=upload.attempt_id,
                kind=upload.kind,
                name=name,
                content_type=upload.content_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                url=f"/api/v1/artifacts/{artifact_id}",
                metadata=dict(upload.metadata or {}),
            )
            recorded = uow.artifacts.record(artifact)
            sequence = uow.events.next_sequence(run_id)
            event = make_event(
                EventType.ARTIFACT_PUBLISHED,
                run_id=run_id,
                workspace_id=self._context.workspace_id,
                sequence=sequence,
                data={
                    "artifact_id": str(recorded.id),
                    "name": recorded.name,
                    "kind": recorded.kind.value,
                    "content_type": recorded.content_type,
                    "size_bytes": recorded.size_bytes,
                    "sha256": recorded.sha256,
                    "test_id": str(upload.test_id) if upload.test_id else None,
                    "attempt_id": str(upload.attempt_id) if upload.attempt_id else None,
                    "url": recorded.url,
                    "metadata": recorded.metadata,
                },
            )
            result = uow.events.apply_events([event])
            uow.commit()

        self._context.bus.publish_nowait(self._context.workspace_id, result.emitted)
        logger.info("artifact %s (%s bytes) attached to run %s", name, size, run_id)
        return recorded
