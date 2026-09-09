"""Retention — reading the policy, previewing a prune, and running one.

This is the one destructive surface in the API, and the only one behind a third
scope. The reasoning, in short: authentication has a read scope and a write
scope, and the write token is the one that lives in every repository's CI
settings, gets copied between projects and is the most likely to leak. A prune
behind ``require_write`` would mean any credential that can post a test result
can also delete a year of them, so retention gets its own token
(:func:`test_hub.api.deps.require_admin`, ADR-0002 D8).

An unauthenticated *loopback* instance is admin by default, because anyone who
can reach that port can already run ``test-hub prune`` in a terminal and
pretending otherwise would only make the dashboard lie about what it can do. An
instance bound to a public interface with ``--insecure-no-auth`` is not: open
writes are a choice an operator makes knowingly, and open *deletes* deserve to be
a second, separate one.

Both endpoints run the same :class:`~test_hub.service.retention.RetentionService`
the CLI runs, so there is one implementation of what a prune means. They are
``def`` rather than ``async def`` — the prune is synchronous SQLite work in
committed batches, and running it on the event loop would freeze every SSE
stream the dashboard is holding open.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from test_hub.api.deps import Context, is_admin, require_admin, require_read
from test_hub.api.schemas import PruneRequest, PruneResponse, RetentionSettingsResponse
from test_hub.errors import ConfigurationError, ValidationError
from test_hub.service.retention import (
    UNIT_HOURS,
    PruneResult,
    RetentionPolicy,
    RetentionService,
    resolve_cutoff,
)

router = APIRouter(tags=["retention"])


@router.get(
    "/retention",
    response_model=RetentionSettingsResponse,
    summary="Read the retention policy",
    dependencies=[Depends(require_read)],
)
def read_retention(
    request: Request,
    context: Context,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> RetentionSettingsResponse:
    settings = context.settings
    return RetentionSettingsResponse(
        admin=is_admin(request, authorization, x_api_key),
        retention_days=settings.retention_days,
        min_runs=settings.retention_min_runs,
        grace_hours=settings.artifact_gc_grace_hours,
        units=dict(UNIT_HOURS),
    )


def _policy(context: Context, body: PruneRequest) -> RetentionPolicy:
    settings = context.settings
    try:
        cutoff = resolve_cutoff(
            older_than=body.older_than,
            before=body.before,
            default_days=settings.retention_days,
        )
    except ConfigurationError as exc:
        # `resolve_cutoff` is shared with the CLI, where an unreadable duration
        # really is a configuration error and 78/EX_CONFIG is the right exit
        # code. Over HTTP the same string came from a client, so it is a 422 —
        # answering 500 would blame the server for the caller's typo and tell
        # every monitor that Test Hub is broken.
        raise ValidationError(exc.message, detail=exc.detail) from exc
    return RetentionPolicy(
        cutoff=cutoff,
        keep_min=settings.retention_min_runs if body.keep_min is None else body.keep_min,
        project_keys=tuple(body.project_keys),
        by_import_date=body.by_import_date,
        include_unfinished=body.include_unfinished,
        grace_seconds=settings.artifact_gc_grace_hours * 3600.0,
    )


def _response(result: PruneResult) -> PruneResponse:
    return PruneResponse.model_validate(result.as_dict())


@router.post(
    "/retention/preview",
    response_model=PruneResponse,
    summary="Count what a prune would remove",
    dependencies=[Depends(require_admin)],
    responses={403: {"description": "An administrator token is required."}},
)
def preview_prune(context: Context, body: PruneRequest) -> PruneResponse:
    """Read-only. Opens a deferred transaction, so it does not queue in front of
    a publisher the way a write would."""
    return _response(RetentionService(context).plan(_policy(context, body)))


@router.post(
    "/retention/prune",
    response_model=PruneResponse,
    summary="Delete runs older than a cutoff",
    dependencies=[Depends(require_admin)],
    responses={
        403: {"description": "An administrator token is required."},
        422: {"description": "The request did not confirm, or the cutoff was unreadable."},
    },
)
def run_prune(context: Context, body: PruneRequest) -> PruneResponse:
    """Irreversible. Deletes the runs, everything under them, and the artifact
    blobs left unreferenced."""
    if not body.confirm:
        # Cheap, and it costs a caller one field. An API that destroys a year of
        # history on a request somebody sent by accident is missing this check.
        raise ValidationError(
            "refusing to prune without confirmation; send `confirm: true` once you have "
            "previewed the result at POST /retention/preview",
            detail={"field": "confirm"},
        )
    return _response(RetentionService(context).prune(_policy(context, body)))
