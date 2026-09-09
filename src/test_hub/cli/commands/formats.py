"""``test-hub formats`` — what this installation can parse.

Also the answer to "why did my file not import": the same list appears in the
error when auto-detection fails, so the terminal and the API agree.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer


def formats(
    json_output: Annotated[bool, typer.Option("--json", help="Print as JSON.")] = False,
) -> None:
    """List the installed report adapters."""
    from test_hub.adapters import get_registry

    rows = get_registry().describe()
    if json_output:
        typer.echo(json.dumps(rows, indent=2))
        return

    typer.echo()
    typer.secho(f"  {len(rows)} adapter(s) installed", bold=True)
    typer.echo()
    for row in rows:
        typer.secho(f"  {row['name']}", fg=typer.colors.CYAN, bold=True, nl=False)
        typer.secho(f"  {row.get('display_name', '')}", fg=typer.colors.BRIGHT_BLACK)
        if row.get("description"):
            typer.echo(f"      {row['description']}")
        extensions = ", ".join(row.get("extensions", [])) or "—"
        typer.secho(f"      extensions: {extensions}", fg=typer.colors.BRIGHT_BLACK)
        typer.echo()
    typer.secho(
        "  Force one with --format <name>. Add more by installing an adapter package.",
        fg=typer.colors.BRIGHT_BLACK,
    )
    typer.echo()
