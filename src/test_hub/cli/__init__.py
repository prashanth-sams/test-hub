"""The ``test-hub`` command line.

Six commands, each a thin wrapper over the service layer — nothing here contains
business logic, so anything you can do from the terminal you can also do over
HTTP and from Python::

    test-hub serve      start the API and the dashboard
    test-hub ingest     import a report into the local store
    test-hub publish    send a report to a remote Test Hub
    test-hub export     write a self-contained offline HTML report
    test-hub demo       fill the store with realistic sample data
    test-hub token      create and hash API tokens
    test-hub formats    list the installed report adapters

One rule shows up everywhere: **tokens are never taken as arguments.** A token on
a command line lands in shell history, in ``ps`` output for every user on the
box, and in the CI log if the step is echoed. ``--token-env`` and ``--token-file``
are the supported ways in.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from test_hub.errors import TestHubError
from test_hub.version import SCHEMA_VERSION, __version__

__all__ = ["app", "main"]

app = typer.Typer(
    name="test-hub",
    help="Universal test observability — every team, framework and test run in one place.",
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"test-hub {__version__} (canonical schema v{SCHEMA_VERSION})")
        raise typer.Exit


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Test Hub."""


def _register() -> None:
    """Attach the sub-commands.

    Imported lazily and inside a function so that ``test-hub --help`` does not
    pay for importing FastAPI, SQLAlchemy and every adapter. On a cold cache that
    is the difference between a command that feels instant and one that does not.
    """
    from test_hub.cli.commands import demo, export, formats, ingest, publish, serve, token

    app.command("serve", help="Start the API and serve the dashboard.")(serve.serve)
    app.command("ingest", help="Import a report file into the local store.")(ingest.ingest)
    app.command("publish", help="Send a report to a remote Test Hub server.")(publish.publish)
    app.command("export", help="Write a self-contained offline HTML report.")(export.export)
    app.command("demo", help="Populate the store with realistic sample data.")(demo.demo)
    app.command("formats", help="List the installed report adapters.")(formats.formats)
    app.add_typer(token.app, name="token", help="Create and hash API tokens.")


def main() -> None:
    """Console-script entry point.

    Catches :class:`~test_hub.errors.TestHubError` and turns it into a clean
    message plus that error's own exit code, so a CI step can branch on *why* it
    failed rather than only on the fact that it did.
    """
    _register()
    try:
        app()
    except TestHubError as exc:
        typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
        for key, value in exc.detail.items():
            typer.secho(f"  {key}: {value}", fg=typer.colors.BRIGHT_BLACK, err=True)
        raise SystemExit(exc.exit_code) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        raise SystemExit(130) from None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())  # type: ignore[func-returns-value]
