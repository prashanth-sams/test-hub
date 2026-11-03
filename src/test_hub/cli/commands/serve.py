"""``test-hub serve`` — start the API and the dashboard.

One Uvicorn worker, deliberately. The live-update bus is in-process (ADR-0001
D6), so a second worker would hold a second bus and half the dashboards would
miss half the events. When that limit starts to bite, the fix is a shared bus
behind the :class:`~test_hub.storage.base.EventBus` protocol, not `--workers 4`.
"""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from test_hub.cli.options import DatabaseUrlOption, DataDirOption, settings_from_cli
from test_hub.logging import configure_logging, get_logger

logger = get_logger(__name__)


def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8000,
    data_dir: DataDirOption = None,
    database_url: DatabaseUrlOption = None,
    reload: Annotated[bool, typer.Option("--reload", help="Reload on code changes.")] = False,
    insecure_no_auth: Annotated[
        bool,
        typer.Option(
            "--insecure-no-auth",
            help="Allow a non-loopback bind with no authentication. Development only.",
        ),
    ] = False,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the dashboard when the server is up.")
    ] = False,
    log_level: Annotated[
        str, typer.Option("--log-level", help="debug, info, warning or error.")
    ] = "info",
) -> None:
    """Start the API and serve the dashboard."""
    import uvicorn

    settings = settings_from_cli(
        host=host,
        port=port,
        data_dir=data_dir,
        database_url=database_url,
        insecure_no_auth=insecure_no_auth or None,
        log_level=log_level,
    )
    configure_logging(settings)

    # Refuse an unauthenticated public bind before anything is opened, so the
    # error arrives instead of a listening socket.
    for warning in settings.check_bind_safety():
        logger.warning(warning)

    settings.ensure_directories()

    from test_hub.api.app import create_app

    # A URL of http://0.0.0.0:8000 is not clickable and not reachable; print the
    # loopback name the operator will actually open.
    wildcard = ("0.0.0.0", "::")  # noqa: S104 - comparison, not a bind
    scheme_host = "localhost" if settings.host in wildcard else settings.host
    url = f"http://{scheme_host}:{settings.port}"
    typer.secho(f"  Test Hub  {url}", fg=typer.colors.CYAN, bold=True)
    typer.secho(f"  dashboard {url}/", fg=typer.colors.BRIGHT_BLACK)
    typer.secho(f"  API docs  {url}/api/docs", fg=typer.colors.BRIGHT_BLACK)
    typer.secho(f"  data      {settings.data_dir}", fg=typer.colors.BRIGHT_BLACK)
    from test_hub.storage.db import describe_database_url

    # Which database actually got chosen, masked: the URL usually holds a password.
    typer.secho(
        f"  database  {describe_database_url(settings.resolved_database_url)}",
        fg=typer.colors.BRIGHT_BLACK,
    )
    if settings.auth_enabled:
        typer.secho("  auth      bearer token required for writes", fg=typer.colors.BRIGHT_BLACK)
    elif not settings.is_loopback:
        typer.secho("  auth      DISABLED on a public interface", fg=typer.colors.RED, bold=True)
    typer.echo()

    if open_browser or settings.open_browser:
        webbrowser.open(url)

    if reload:
        # Reload needs an import string rather than an object, and it rebuilds
        # the app (and therefore the context) on every change — in a fresh
        # process that only sees the environment. Hand it the flags that chose
        # where the data lives, or `--database-url` would quietly not apply.
        os.environ["TEST_HUB_DATA_DIR"] = str(settings.data_dir)
        if settings.database_url:
            os.environ["TEST_HUB_DATABASE_URL"] = settings.database_url
        uvicorn.run(
            "test_hub.api.app:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parents[3])],
            log_level=settings.log_level,
            access_log=False,
        )
        return

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=False,
        timeout_graceful_shutdown=3,
    )
