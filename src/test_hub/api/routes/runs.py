"""Live publishing endpoints — open a run, stream results into it, close it.

This is the path a framework plugin or a shell script uses when it wants results
to appear on the dashboard *while the suite is still running*, rather than
uploading a file at the end.

.. code-block:: shell

    RUN=$(curl -sX POST localhost:8000/api/v1/runs \\
      -H 'content-type: application/json' \\
      -d '{"project_key":"payments-api","test_type":"e2e"}' | jq -r .id)

    curl -X POST localhost:8000/api/v1/runs/$RUN/events \\
      -H 'content-type: application/json' -H "X-Idempotency-Key: batch-1" \\
      -d @events.json

    curl -X POST localhost:8000/api/v1/runs/$RUN/finish -d '{}'

Every one of those calls can be repeated safely. That is not a nicety: CI steps
get retried, networks time out after the server already committed, and a
publisher that cannot retry is a publisher that loses data.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile

from test_hub.api.deps import Context, Runs, require_write
from test_hub.api.schemas import (
    ArtifactResponse,
    CreateRunRequest,
    EventsRequest,
    EventsResponse,
    FinishRunRequest,
)
from test_hub.domain.models import Artifact, ArtifactKind, TestRun
from test_hub.errors import NotFoundError
from test_hub.service.runs import ArtifactUpload, RunSpec

router = APIRouter(tags=["publishing"], dependencies=[Depends(require_write)])


@router.post("/runs", response_model=TestRun, status_code=201, summary="Open a run")
def create_run(body: CreateRunRequest, service: Runs) -> TestRun:
    return service.create_run(
        RunSpec(
            project_key=body.project_key,
            run_id=body.run_id,
            name=body.name,
            team=body.team,
            component=body.component,
            test_type=body.test_type,
            trigger=body.trigger,
            context=body.context,
            tags=body.tags,
            metadata=body.metadata,
            expected_tests=body.expected_tests,
            started=body.started,
        )
    )


@router.post(
    "/runs/{run_id}/events",
    response_model=EventsResponse,
    summary="Append events to a run",
    responses={404: {"description": "The run does not exist."}},
)
def post_events(
    run_id: uuid.UUID,
    body: EventsRequest,
    service: Runs,
    context: Context,
    x_idempotency_key: Annotated[str | None, Header()] = None,
) -> EventsResponse:
    """Apply a batch of events.

    Idempotent twice over: at the batch level through ``X-Idempotency-Key``, and
    at the event level through the unique ``(source, id)`` constraint. Reposting
    returns ``applied: 0`` and leaves every count where it was.
    """
    result = service.post_events(run_id, body.events, idempotency_key=x_idempotency_key)
    with context.uow_factory(read_only=True) as uow:
        run = uow.runs.get_run(run_id)
    if run is None:  # pragma: no cover - apply_events would have raised
        raise NotFoundError(f"run {run_id} does not exist")
    return EventsResponse(
        run_id=run_id,
        applied=result.applied,
        ignored=result.ignored,
        created_run=result.created_run,
        counts=run.counts,
        status=run.status,
    )


@router.post("/runs/{run_id}/finish", response_model=TestRun, summary="Close a run")
def finish_run(run_id: uuid.UUID, body: FinishRunRequest, service: Runs) -> TestRun:
    return service.finish_run(
        run_id,
        status=body.status,
        finished_at=body.finished_at,
        duration_ms=body.duration_ms,
        metadata=body.metadata,
    )


@router.post(
    "/runs/{run_id}/artifacts",
    response_model=ArtifactResponse,
    status_code=201,
    summary="Attach a file to a run",
    responses={413: {"description": "The file exceeds TEST_HUB_MAX_ARTIFACT_BYTES."}},
)
def upload_artifact(
    run_id: uuid.UUID,
    service: Runs,
    file: Annotated[UploadFile, File()],
    kind: Annotated[ArtifactKind, Form()] = ArtifactKind.OTHER,
    test_id: Annotated[uuid.UUID | None, Form()] = None,
    attempt_id: Annotated[uuid.UUID | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
) -> ArtifactResponse:
    """Store a screenshot, trace, log or report against a run.

    Content-addressed: uploading the same bytes from fifty parallel shards
    stores one copy and returns ``deduplicated: true`` for the other
    forty-nine.
    """
    artifact: Artifact = service.add_artifact(
        run_id,
        ArtifactUpload(
            name=name or file.filename or "artifact",
            stream=file.file,
            kind=kind,
            content_type=file.content_type or "application/octet-stream",
            test_id=test_id,
            attempt_id=attempt_id,
        ),
    )
    return ArtifactResponse(artifact=artifact)
