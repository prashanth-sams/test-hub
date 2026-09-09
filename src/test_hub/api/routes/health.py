"""Liveness and readiness.

``/healthz`` is deliberately outside ``/api/v1``: it is infrastructure, not part
of the versioned contract, and a container healthcheck should not have to be
updated when the API version moves. It touches the database — a Test Hub that
cannot read its own store is not healthy, however well it serves HTTP.
"""

from __future__ import annotations

from fastapi import APIRouter

from test_hub.api.deps import Context
from test_hub.api.schemas import HealthResponse
from test_hub.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/healthz", response_model=HealthResponse, summary="Liveness and readiness")
def healthz(context: Context) -> HealthResponse:
    database = "ok"
    try:
        with context.uow_factory(read_only=True) as uow:
            uow.runs.get_workspace(context.workspace_id)
    except Exception as exc:  # a healthcheck must report, never raise
        logger.error("health check failed to reach the database: %s", exc)
        database = "error"
    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        database=database,
        workspace_id=context.workspace_id,
        live_subscribers=context.bus.subscriber_count,
    )
