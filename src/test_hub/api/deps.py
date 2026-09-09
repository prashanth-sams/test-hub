"""FastAPI dependencies — the bridge from the app's state to the service layer.

The :class:`~test_hub.service.runtime.AppContext` is created once in the
lifespan handler and hung off ``app.state``. Everything else reaches it through
these dependencies, which keeps the routes free of any knowledge about how the
context was built and lets a test swap the whole thing for a temporary one.

Repository work is synchronous (SQLAlchemy, SQLite), so every route that touches
the database is declared ``def`` rather than ``async def``. FastAPI then runs it
in a worker thread and the event loop — which is also serving SSE streams — is
never blocked by a slow query. Getting this backwards is the classic way to make
a live dashboard stutter whenever somebody imports a large report.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request

from test_hub.api.security import TokenVerifier
from test_hub.errors import AuthenticationError
from test_hub.service.imports import ImportService
from test_hub.service.runs import RunService
from test_hub.service.runtime import AppContext

__all__ = [
    "Context",
    "Imports",
    "Runs",
    "get_context",
    "get_import_service",
    "get_run_service",
    "require_read",
    "require_write",
]


def get_context(request: Request) -> AppContext:
    """The application context for this request."""
    return request.app.state.context  # type: ignore[no-any-return]


def get_verifier(request: Request) -> TokenVerifier:
    return request.app.state.verifier  # type: ignore[no-any-return]


Context = Annotated[AppContext, Depends(get_context)]


def require_write(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """Guard every mutating endpoint.

    ``X-API-Key`` is accepted alongside ``Authorization: Bearer`` because a
    surprising number of CI systems make it hard to set an Authorization header
    on a generic HTTP step. It is the same token and the same check.
    """
    verifier = get_verifier(request)
    if not verifier.enabled:
        return
    if authorization:
        verifier.require(authorization)
        return
    if x_api_key and verifier.verify(x_api_key.strip()):
        return
    raise AuthenticationError(
        "a valid bearer token is required; send it as `Authorization: Bearer <token>` "
        "or `X-API-Key: <token>`"
    )


def require_read(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """Guard read endpoints — only when the operator asked for that.

    Reads are open by default even with auth on, because the common deployment
    is "anyone in the team may look, only CI may write". Set
    ``TEST_HUB_REQUIRE_AUTH_FOR_READS=true`` to close them too.
    """
    context = get_context(request)
    if not context.settings.require_auth_for_reads:
        return
    require_write(request, authorization, x_api_key)


def get_import_service(context: Context) -> ImportService:
    return ImportService(context)


def get_run_service(context: Context) -> RunService:
    return RunService(context)


def iter_context(request: Request) -> Iterator[AppContext]:  # pragma: no cover - convenience
    yield get_context(request)


Imports = Annotated[ImportService, Depends(get_import_service)]
Runs = Annotated[RunService, Depends(get_run_service)]
