"""The FastAPI application: wiring, security headers, error mapping, static assets.

Three things happen here that are worth reading before changing anything.

**Errors are mapped once.** Every route raises
:class:`~test_hub.errors.TestHubError` subclasses and never builds an HTTP
response by hand, so the JSON shape of a failure — ``{error, message, detail}`` —
is guaranteed identical across the API and matches what the CLI prints.

**Security headers are applied to everything**, including the static dashboard and
the OpenAPI docs. The CSP is restrictive by default (``default-src 'none'``),
which is only possible because the frontend ships no inline scripts, no inline
styles and no remote origins — see ADR-0001 D7. If you ever find yourself
loosening the CSP to make a feature work, the feature is wrong, not the policy.

**The dashboard is served from the wheel.** ``test_hub/web`` is packaged, mounted
read-only, and served with an ETag; there is no separate web server to run and no
build step to forget.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from test_hub.api.routes import health, imports, queries, runs, stream
from test_hub.api.security import TokenVerifier
from test_hub.config import Settings, get_settings
from test_hub.errors import TestHubError
from test_hub.logging import configure_logging, get_logger
from test_hub.service.runtime import AppContext, create_context
from test_hub.version import __version__

__all__ = ["create_app", "web_root"]

logger = get_logger(__name__)

API_PREFIX = "/api/v1"

DESCRIPTION = """
Universal test observability — every team, framework and test run in one place.

Test Hub accepts test results from any language and any framework over plain
HTTP. There is no required client library: `curl` is a first-class publisher.

* **Import a report file** — `POST /api/v1/imports` with a JUnit XML or Test Hub
  canonical JSON file and the metadata that describes the run.
* **Publish live** — `POST /api/v1/runs` to open a run, then stream
  `POST /api/v1/runs/{run_id}/events` as tests finish, then
  `POST /api/v1/runs/{run_id}/finish`.
* **Watch** — `GET /api/v1/events/stream` is Server-Sent Events with heartbeats
  and `Last-Event-ID` replay.

Every write is idempotent. Reposting an event, a batch or a whole report never
duplicates a test or moves a count.
"""


def web_root() -> Path:
    """Where the packaged dashboard lives.

    Uses ``importlib.resources`` so it resolves correctly from an installed
    wheel, a zipapp or a source checkout, rather than assuming a path relative
    to this file.
    """
    return Path(str(files("test_hub").joinpath("web")))


class _SecurityHeadersMiddleware:
    """Adds the headers that make an XSS in stored test output non-exploitable,
    and the one that stops a browser running last release's dashboard.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` because the latter
    buffers responses, which would defeat the whole point of the SSE endpoint.

    **The cache header is not a performance tweak.** ``StaticFiles`` sends an
    ``ETag`` and a ``Last-Modified`` but no ``Cache-Control``, and a response
    with a validator and no freshness directive is one a browser may cache
    *heuristically* — commonly a tenth of the file's age — serving it for hours
    without ever asking whether it changed. The dashboard is a dozen plain
    ``.js`` files at stable paths, so the symptom of that is an upgraded Test Hub
    running the previous version's JavaScript against the current version's
    markup: no error, no console message, just a feature that silently is not
    there. ``no-cache`` does not mean "do not store" — it means "revalidate
    first", so the browser still gets a 304 and the bytes still come from disk
    cache. The cost is one conditional request per asset per load; the thing it
    buys is that shipping a fix actually ships it.
    """

    def __init__(self, app: Any, settings: Settings) -> None:
        self.app = app
        self.headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
        ]
        #: Sent for the dashboard only. The API sets its own caching where it
        #: wants any, and SSE must never be cached at all.
        self.revalidate: tuple[bytes, bytes] = (b"cache-control", b"no-cache")
        if settings.csp:
            self.headers.append((b"content-security-policy", settings.csp.encode("latin-1")))

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_dashboard = path == "/" or path.startswith("/assets/")

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                existing = {key.lower() for key, _ in message.get("headers", [])}
                message.setdefault("headers", [])
                for key, value in self.headers:
                    if key not in existing:
                        message["headers"].append((key, value))
                if is_dashboard and self.revalidate[0] not in existing:
                    message["headers"].append(self.revalidate)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _install_error_handlers(app: FastAPI) -> None:
    """One place where an exception becomes an HTTP response."""

    @app.exception_handler(TestHubError)
    async def _test_hub_error(_: Request, exc: TestHubError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("%s: %s", exc.code, exc.message)
        else:
            logger.debug("%s: %s", exc.code, exc.message)
        headers = {"WWW-Authenticate": "Bearer"} if exc.http_status == 401 else None
        return JSONResponse(exc.to_dict(), status_code=exc.http_status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default shape is `{"detail": [...]}`; ours is the same shape
        # as every other error so a client needs exactly one error parser.
        return JSONResponse(
            {
                "error": "validation_error",
                "message": "the request body did not match the expected schema",
                "detail": {"errors": _jsonable_errors(exc.errors())},
            },
            status_code=422,
        )


def _jsonable_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip the non-serializable ``ctx`` Pydantic sometimes attaches."""
    cleaned: list[dict[str, Any]] = []
    for error in errors:
        item = {key: value for key, value in error.items() if key != "ctx"}
        item["loc"] = [str(part) for part in item.get("loc", ())]
        cleaned.append(item)
    return cleaned


def create_app(
    settings: Settings | None = None,
    *,
    context: AppContext | None = None,
) -> FastAPI:
    """Build the application.

    Pass ``context`` to reuse an already-built one — that is how the CLI avoids
    opening the database twice, and how tests bind the app to a temporary
    directory.
    """
    settings = settings or (context.settings if context else get_settings())
    configure_logging(settings)

    owns_context = context is None

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.context = context or create_context(settings)
        # Writes happen on worker threads; the bus needs this loop to publish onto.
        app.state.context.bus.bind_loop()
        app.state.verifier = TokenVerifier.from_settings(settings)
        app.state.settings = settings
        for warning in settings.check_bind_safety():
            logger.warning(warning)
        logger.info("Test Hub %s ready — data in %s", __version__, settings.data_dir)
        try:
            yield
        finally:
            if owns_context:
                app.state.context.close()

    app = FastAPI(
        title="Test Hub",
        version=__version__,
        description=DESCRIPTION,
        summary="Universal test observability — every team, framework and test run in one place.",
        license_info={"name": "Apache-2.0", "identifier": "Apache-2.0"},
        lifespan=lifespan,
        root_path=settings.root_path,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    _install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(imports.router, prefix=API_PREFIX)
    app.include_router(runs.router, prefix=API_PREFIX)
    app.include_router(queries.router, prefix=API_PREFIX)
    app.include_router(stream.router, prefix=API_PREFIX)

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["authorization", "content-type", "x-api-key", "x-idempotency-key"],
        )
    if settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(_SecurityHeadersMiddleware, settings=settings)

    _mount_dashboard(app)
    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the packaged dashboard at ``/``.

    Mounted last so it cannot shadow an API route, and served through an
    explicit index handler so that ``/`` and any client-side route both resolve
    to the same file.
    """
    root = web_root()
    if not root.is_dir():  # pragma: no cover - only in a broken install
        logger.warning("dashboard assets are missing from %s; serving the API only", root)
        return

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = root / "index.html"

    @app.get("/", include_in_schema=False)
    async def dashboard() -> Response:
        if not index.is_file():  # pragma: no cover
            return JSONResponse({"error": "not_found", "message": "dashboard not installed"}, 404)
        return FileResponse(index, media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        icon = root / "assets" / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=204)
