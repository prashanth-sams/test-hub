"""``test-hub db`` — which database Test Hub is using, and whether it is ready.

The database is chosen with ``TEST_HUB_DATABASE_URL`` (or ``--database-url``):
unset is SQLite in the data directory, ``postgresql://…`` is PostgreSQL. These
two commands are how you check that the choice took::

    test-hub db check     show the database, prove it answers, compare the schema
    test-hub db upgrade   apply pending migrations now instead of on next start

``serve``, ``ingest``, ``demo`` and ``prune`` all migrate on their own, so
``upgrade`` is for when you want the schema change to be its own deliberate
step — a deploy pipeline that migrates before it rolls the new image out.
"""

from __future__ import annotations

import typer

from test_hub.cli.options import DatabaseUrlOption, DataDirOption, settings_from_cli

app = typer.Typer(
    no_args_is_help=True,
    help="Check the database Test Hub is configured to use, and migrate it.",
)


@app.command("check", help="Show which database is configured and whether it is ready.")
def check(data_dir: DataDirOption = None, database_url: DatabaseUrlOption = None) -> None:
    """Exit 0 when ready, 1 when migrations are pending, 78 when it cannot be reached."""
    from test_hub.storage import create_database

    settings = settings_from_cli(data_dir=data_dir, database_url=database_url)
    database = create_database(settings)
    try:
        typer.secho(f"  database  {database.display_url}", fg=typer.colors.CYAN)
        database.ping()
        typer.secho(f"  server    {database.server_version()}", fg=typer.colors.BRIGHT_BLACK)

        current = database.current_revision()
        head = database.head_revision()
        if current is not None and current == head:
            typer.secho(f"  schema    {current} (up to date)", fg=typer.colors.BRIGHT_BLACK)
            typer.secho("  ready", fg=typer.colors.GREEN, bold=True)
            return
        if current is None:
            typer.secho("  schema    empty", fg=typer.colors.YELLOW)
        else:
            typer.secho(f"  schema    {current}, {head} available", fg=typer.colors.YELLOW)
        typer.secho(
            "  run `test-hub db upgrade` (serve also does it on start)", fg=typer.colors.YELLOW
        )
    finally:
        database.dispose()
    raise typer.Exit(1)


@app.command("upgrade", help="Apply any pending schema migrations now.")
def upgrade(data_dir: DataDirOption = None, database_url: DatabaseUrlOption = None) -> None:
    """Bring the schema to head. Safe to run repeatedly, and from two places at once."""
    from test_hub.storage import create_database

    settings = settings_from_cli(data_dir=data_dir, database_url=database_url)
    database = create_database(settings)
    try:
        typer.secho(f"  database  {database.display_url}", fg=typer.colors.CYAN)
        database.ping()
        before = database.current_revision()
        database.upgrade()
        after = database.current_revision()
    finally:
        database.dispose()

    if before == after:
        typer.secho(f"  schema    already at {after}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  schema    {before or 'empty'} → {after}", fg=typer.colors.GREEN, bold=True)
