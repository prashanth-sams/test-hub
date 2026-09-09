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
) -> None:
    """Generate a token and print it with the hash to configure."""
    token = generate_token()
    digest = hash_token(token)

    typer.echo()
    typer.secho("  Token (shown once — copy it now)", bold=True)
    typer.secho(f"  {token}", fg=typer.colors.GREEN, bold=True)
    typer.echo()
    typer.secho("  Configure the server with the hash, not the token:", bold=True)
    typer.secho(f'  export TEST_HUB_API_TOKEN_HASHES="{digest}"', fg=typer.colors.CYAN)
    typer.echo()
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
