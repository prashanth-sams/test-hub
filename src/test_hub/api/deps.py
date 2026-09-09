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

from test_hub.api.security import TokenVerifier, parse_authorization
from test_hub.errors import AuthenticationError, AuthorizationError
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
    "is_admin",
    "require_admin",
    "require_read",
    "require_write",
]


def get_context(request: Request) -> AppContext:
    """The application context for this request."""
    return request.app.state.context  # type: ignore[no-any-return]


def get_verifier(request: Request) -> TokenVerifier:
    return request.app.state.verifier  # type: ignore[no-any-return]


def get_admin_verifier(request: Request) -> TokenVerifier:
    return request.app.state.admin_verifier  # type: ignore[no-any-return]


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


def is_admin(
    request: Request,
    authorization: str | None = None,
    x_api_key: str | None = None,
) -> bool:
    """Whether this caller may run destructive maintenance.

    Two ways in, and no third:

    **An admin token.** A separate list from the write tokens, because the write
    token is the one that lives in every repository's CI settings — a credential
    that can post a test result must not also be able to delete a year of them
    (ADR-0002 D8).

    **An unauthenticated loopback instance.** The laptop case: the port is
    reachable only from this machine, and anyone who can reach it can already
    run ``test-hub prune`` in a terminal, so refusing here would protect nothing
    and only make the dashboard lie about what it can do.

    Note what is *not* on that list: an instance bound to a public interface with
    ``--insecure-no-auth``. Writes are open there by the operator's explicit
    choice, but "anyone on the network may add data" and "anyone on the network
    may destroy it" are different enough that the second needs saying out loud —
    with an admin token.
    """
    admin = get_admin_verifier(request)
    presented = parse_authorization(authorization) or (x_api_key or "").strip() or None
    if presented and admin.hashes and admin.verify(presented):
        return True
    settings = get_context(request).settings
    return not settings.auth_enabled and settings.is_loopback


def require_admin(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """Guard destructive maintenance. See :func:`is_admin` for the rules."""
    if is_admin(request, authorization, x_api_key):
        return
    raise AuthorizationError(
        "this endpoint needs an administrator token. Create one with "
        "`test-hub token create --admin` and set TEST_HUB_ADMIN_TOKEN_HASHES, "
        "or run `test-hub prune` on the host."
    )


def get_import_service(context: Context) -> ImportService:
    return ImportService(context)


def get_run_service(context: Context) -> RunService:
    return RunService(context)


def iter_context(request: Request) -> Iterator[AppContext]:  # pragma: no cover - convenience
    yield get_context(request)


Imports = Annotated[ImportService, Depends(get_import_service)]
Runs = Annotated[RunService, Depends(get_run_service)]
