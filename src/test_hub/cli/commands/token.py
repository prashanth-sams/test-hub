"""``test-hub token`` — create and hash API tokens.

Test Hub stores only hashes, so this command is the only moment a token exists in
plaintext. It prints it once, tells you where to put it, and does not write it to
disk. If you lose it, create another and remove the old hash.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

from test_hub.api.security import generate_token, hash_token

app = typer.Typer(no_args_is_help=True, help="Create and hash API tokens.")


@app.command("create")
def create(
    label: Annotated[str, typer.Option("--label", help="A note for your own records.")] = "ci",
    admin: Annotated[
        bool,
        typer.Option(
            "--admin",
            help="Create an administrator token, which may delete data. Separate from "
            "publisher tokens on purpose.",
        ),
    ] = False,
) -> None:
    """Generate a token and print it with the hash to configure.

    ``--admin`` produces a token for the retention endpoints. It is a *different*
    list on the server (``TEST_HUB_ADMIN_TOKEN_HASHES``) because the publisher
    token lives in every repository's CI settings and gets copied between
    projects: a credential that can post a test result must not also be able to
    delete a year of them (ADR-0002 D8).
    """
    token = generate_token()
    digest = hash_token(token)
    variable = "TEST_HUB_ADMIN_TOKEN_HASHES" if admin else "TEST_HUB_API_TOKEN_HASHES"

    typer.echo()
    typer.secho(
        f"  {'Administrator token' if admin else 'Token'} (shown once — copy it now)", bold=True
    )
    typer.secho(f"  {token}", fg=typer.colors.GREEN, bold=True)
    typer.echo()
    typer.secho("  Configure the server with the hash, not the token:", bold=True)
    typer.secho(f'  export {variable}="{digest}"', fg=typer.colors.CYAN)
    typer.echo()
    if admin:
        typer.secho("  This token may delete data:", bold=True)
        typer.secho(
            "  It unlocks the Settings page in the dashboard and the /retention endpoints. "
            "Keep it out of CI.",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho("  Publishers use the token itself:", bold=True)
        typer.secho(f'  export TEST_HUB_TOKEN="{token}"   # label: {label}', fg=typer.colors.CYAN)
        typer.secho(
            "  test-hub publish results.xml --url https://… --project p --token-env TEST_HUB_TOKEN",
            fg=typer.colors.BRIGHT_BLACK,
        )
    typer.echo()
    typer.secho(
        "  Test Hub keeps only the hash. Nothing can recover this token from the server.",
        fg=typer.colors.BRIGHT_BLACK,
    )
    typer.echo()


@app.command("hash")
def hash_command(
    token_env: Annotated[
        str,
        typer.Option("--token-env", help="Environment variable holding the token to hash."),
    ] = "TEST_HUB_TOKEN",  # noqa: S107 - the NAME of a variable, not a token
) -> None:
    """Hash an existing token so it can be configured on a server.

    Reads from the environment rather than an argument, for the same reason
    ``publish`` does: an argument is world-readable in ``ps`` and lands in shell
    history.
    """
    token = os.environ.get(token_env, "").strip()
    if not token:
        typer.secho(
            f"  environment variable {token_env} is not set or is empty",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(hash_token(token))
