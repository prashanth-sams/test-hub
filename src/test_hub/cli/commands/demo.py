"""``test-hub demo`` — fill the store with realistic sample data.

The fastest way to judge whether Test Hub is worth adopting: run it, then
``test-hub serve``, and look at a populated dashboard rather than an empty one.
"""

from __future__ import annotations

from typing import Annotated

import typer

from test_hub.cli.options import (
    DatabaseUrlOption,
    DataDirOption,
    settings_from_cli,
    storage_location,
)


def demo(
    days: Annotated[int, typer.Option("--days", help="How far back to spread the runs.")] = 14,
    runs: Annotated[int, typer.Option("--runs", help="Runs per project.")] = 6,
    live: Annotated[
        bool, typer.Option("--live/--no-live", help="Include one run that is still going.")
    ] = True,
    data_dir: DataDirOption = None,
    database_url: DatabaseUrlOption = None,
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Delete the existing data first (on PostgreSQL: drop Test Hub's tables).",
        ),
    ] = False,
) -> None:
    """Populate the store with realistic sample data."""
    from test_hub.demo.seed import seed_demo
    from test_hub.service.runtime import create_context

    settings = settings_from_cli(data_dir=data_dir, database_url=database_url)

    if reset and settings.database_backend == "sqlite":
        database_file = settings.data_dir / "test-hub.db"
        removed = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = database_file.with_name(database_file.name + suffix)
            if candidate.exists():
                candidate.unlink()
                removed += 1
        if removed:
            typer.secho(
                f"  removed the existing database ({removed} file(s))", fg=typer.colors.YELLOW
            )
    elif reset:
        # A server has no file to delete, and dropping the whole database would
        # take anything else in it along. Drop Test Hub's own tables instead;
        # create_context below migrates them back from nothing.
        from test_hub.storage import create_database

        database = create_database(settings)
        try:
            database.ping()
            database.drop_schema()
        finally:
            database.dispose()
        typer.secho(
            f"  dropped the existing Test Hub tables in {database.display_url}",
            fg=typer.colors.YELLOW,
        )

    context = create_context(settings)
    try:
        typer.echo()
        typer.secho("  seeding sample data…", fg=typer.colors.BRIGHT_BLACK)
        result = seed_demo(context, days=days, runs_per_project=runs, include_live_run=live)
    finally:
        context.close()

    typer.echo()
    typer.secho("  sample data created", fg=typer.colors.GREEN, bold=True)
    typer.secho(
        f"  {result.projects} projects, {result.runs} runs, "
        f"{result.tests} test results, {result.attempts} attempts",
        fg=typer.colors.BRIGHT_BLACK,
    )
    typer.secho(
        f"  {result.failures} failures and {result.flaky} flaky retries, "
        f"so the red and amber states have something to show",
        fg=typer.colors.BRIGHT_BLACK,
    )
    if result.artifacts:
        typer.secho(
            f"  {result.artifacts} files attached — screenshots, request logs, "
            f"coverage reports and a trace",
            fg=typer.colors.BRIGHT_BLACK,
        )
    if result.live_run_id:
        typer.secho(f"  one run is still open: {result.live_run_id}", fg=typer.colors.BLUE)
    typer.secho(f"  stored in {storage_location(settings)}", fg=typer.colors.BRIGHT_BLACK)
    typer.echo()
    typer.secho("  next:  test-hub serve", fg=typer.colors.CYAN, bold=True)
    typer.echo()
