"""Read endpoints — what the dashboard, and anyone with `curl`, asks for.

Two altitudes, kept apart on purpose (it is a product requirement, not an
implementation detail): ``/overview`` answers "how is the portfolio doing" and is
cheap enough to poll, while ``/runs/{id}`` and ``/tests/{id}`` are the
engineering drill-down and are allowed to return a lot.

Filters are shared across ``/overview`` and ``/runs`` and mean the same thing in
both, so a dashboard can carry the user's filter selection from one view to the
other without translating it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import AwareDatetime

from test_hub.api.deps import Context, require_read
from test_hub.api.schemas import ArtifactListResponse, ProjectListResponse, RunListResponse
from test_hub.domain.models import OverviewSnapshot, RunDetail, TestDetail
from test_hub.domain.status import RunStatus
from test_hub.errors import NotFoundError
from test_hub.storage.base import RunFilter

router = APIRouter(tags=["query"], dependencies=[Depends(require_read)])

# Every handler below opens its unit of work with ``read_only=True``. On SQLite
# that is the difference between a query queueing behind a publisher and running
# alongside it: a writing transaction takes the write lock up front (see
# storage/db.py), and a dashboard polling the overview would otherwise contend
# with every import.

_CSV = "Repeat the parameter or pass a comma-separated list."


def _split(values: list[str] | None) -> list[str]:
    """Accept both ``?team=a&team=b`` and ``?team=a,b``."""
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def build_filter(
    context: Context,
    project: Annotated[list[str] | None, Query(description=_CSV)] = None,
    team: Annotated[list[str] | None, Query(description=_CSV)] = None,
    component: Annotated[list[str] | None, Query(description=_CSV)] = None,
    test_type: Annotated[list[str] | None, Query(alias="type", description=_CSV)] = None,
    environment: Annotated[list[str] | None, Query(description=_CSV)] = None,
    branch: Annotated[list[str] | None, Query(description=_CSV)] = None,
    status: Annotated[list[RunStatus] | None, Query(description=_CSV)] = None,
    tag: Annotated[list[str] | None, Query(description=_CSV)] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    live: Annotated[bool, Query(description="Only runs that are queued or running.")] = False,
    # Aware only: a timestamp with no offset would be read as UTC, and a caller
    # writing local time would get a window hours away from the one they meant
    # without any sign of it. A 422 that says so is the kinder answer.
    since: Annotated[
        AwareDatetime | None,
        Query(description="Runs that started at or after this instant. ISO 8601 with an offset."),
    ] = None,
    until: Annotated[
        AwareDatetime | None,
        Query(description="Runs that started at or before this instant. ISO 8601 with an offset."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunFilter:
    """Every facet the dashboard exposes, in one dependency."""
    return RunFilter(
        workspace_id=context.workspace_id,
        project_keys=_split(project),
        teams=_split(team),
        components=_split(component),
        test_types=_split(test_type),
        environments=_split(environment),
        branches=_split(branch),
        statuses=tuple(status or ()),
        tags=_split(tag),
        search=search,
        since=since,
        until=until,
        live_only=live,
        limit=limit,
        offset=offset,
    )


Filter = Annotated[RunFilter, Depends(build_filter)]


@router.get("/overview", response_model=OverviewSnapshot, summary="Portfolio overview")
def get_overview(
    context: Context,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
    after: Annotated[
        int | None,
        Query(
            ge=0,
            description="The cursor (SSE `id:`) of the newest change the caller has seen. The "
            "answer includes it and every change before it — and may be shared with other "
            "callers asking for the same. Without it, the answer is as new as the request.",
        ),
    ] = None,
) -> OverviewSnapshot:
    """Totals, trends, project health, live runs and the worst offenders."""

    def compute() -> OverviewSnapshot:
        with context.uow_factory(read_only=True) as uow:
            return uow.runs.overview(context.workspace_id, window_days=window_days)

    # Every open dashboard asks for this the moment a result lands. Computed
    # per request, a burst of results piled requests up faster than they
    # finished until no worker thread or connection was free; shared, it is
    # one computation per change however many dashboards are watching.
    current = context.bus.latest_cursor(context.workspace_id)
    return context.overviews.get(
        window_days,
        need=current if after is None else after,
        current=current,
        compute=compute,
        # Only a caller on the event stream says `after`, and only such a
        # caller is told — and asks again — when a newer answer exists.
        reuse_finished=after is not None,
    )


@router.get("/projects", response_model=ProjectListResponse, summary="List projects")
def list_projects(context: Context) -> ProjectListResponse:
    with context.uow_factory(read_only=True) as uow:
        return ProjectListResponse(projects=uow.runs.list_projects(context.workspace_id))


@router.get("/facets", summary="Distinct values available for each filter")
def get_facets(context: Context) -> dict[str, list[str]]:
    """What is worth offering in a filter dropdown, for this workspace."""
    with context.uow_factory(read_only=True) as uow:
        return uow.runs.facets(context.workspace_id)


@router.get("/runs", response_model=RunListResponse, summary="List runs")
def list_runs(context: Context, run_filter: Filter) -> RunListResponse:
    with context.uow_factory(read_only=True) as uow:
        runs = uow.runs.list_runs(run_filter)
        total = uow.runs.count_runs(run_filter)
    return RunListResponse(runs=runs, total=total, limit=run_filter.limit, offset=run_filter.offset)


@router.get(
    "/runs/{run_id}",
    response_model=RunDetail,
    summary="One run, with its suites, tests, attempts and comparison",
    responses={404: {"description": "No such run."}},
)
def get_run(
    context: Context,
    run_id: uuid.UUID,
    compare: Annotated[bool, Query(description="Include the previous-run comparison.")] = True,
) -> RunDetail:
    with context.uow_factory(read_only=True) as uow:
        detail = uow.runs.get_run_detail(run_id, comparison=compare)
    if detail is None:
        raise NotFoundError(f"run {run_id} does not exist")
    return detail


@router.get(
    "/tests/{test_id}",
    response_model=TestDetail,
    summary="One test identity, with its history across runs",
    responses={404: {"description": "No such test."}},
)
def get_test(
    context: Context,
    test_id: uuid.UUID,
    history: Annotated[int, Query(ge=1, le=200)] = 30,
) -> TestDetail:
    with context.uow_factory(read_only=True) as uow:
        detail = uow.runs.get_test_detail(
            test_id, workspace_id=context.workspace_id, history=history
        )
    if detail is None:
        raise NotFoundError(f"test {test_id} has no recorded results")
    return detail


@router.get(
    "/runs/{run_id}/artifacts",
    response_model=ArtifactListResponse,
    summary="Files attached to a run",
)
def list_artifacts(context: Context, run_id: uuid.UUID) -> ArtifactListResponse:
    with context.uow_factory(read_only=True) as uow:
        return ArtifactListResponse(artifacts=uow.artifacts.list_for_run(run_id))


@router.get(
    "/artifacts/{artifact_id}",
    summary="Download one artifact",
    response_class=Response,
    responses={404: {"description": "No such artifact."}},
)
def download_artifact(context: Context, artifact_id: uuid.UUID) -> StreamingResponse:
    """Stream an artifact back.

    Always ``Content-Disposition: attachment`` and always the stored content
    type — never a type sniffed from the bytes. An artifact is attacker-supplied
    content on our origin; serving it inline would hand an uploader a stored XSS
    on the dashboard's own domain.
    """
    with context.uow_factory(read_only=True) as uow:
        artifact = uow.artifacts.get(artifact_id)
        if artifact is None:
            raise NotFoundError(f"artifact {artifact_id} does not exist")
        handle = uow.artifacts.open(artifact.sha256)

    def stream() -> object:
        try:
            while chunk := handle.read(64 * 1024):
                yield chunk
        finally:
            handle.close()

    quoted = artifact.name.replace('"', "")
    return StreamingResponse(
        stream(),  # type: ignore[arg-type]
        media_type=artifact.content_type,
        headers={
            "content-disposition": f'attachment; filename="{quoted}"',
            "content-length": str(artifact.size_bytes),
            "x-content-type-options": "nosniff",
        },
    )
