"""Options shared by more than one command, and the settings overlay they build.

Kept together so that ``--project`` means the same thing, and reads the same in
``--help``, whichever command you type it after. The function at the bottom is
the only place CLI flags are allowed to become :class:`~test_hub.config.Settings`
— flags override the environment, the environment overrides the ``.env`` file,
and nothing else may reorder that.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import typer

from test_hub.config import Settings, get_settings
from test_hub.domain.models import TestType, TriggerType
from test_hub.errors import ConfigurationError

__all__ = [
    "BranchOption",
    "CommitOption",
    "ComponentOption",
    "DataDirOption",
    "EnvironmentOption",
    "FormatOption",
    "ProjectOption",
    "TeamOption",
    "TestTypeOption",
    "TriggerOption",
    "read_token",
    "settings_from_cli",
]

ProjectOption = Annotated[
    str, typer.Option("--project", "-p", help="Project key, e.g. payments-api.")
]
TeamOption = Annotated[str | None, typer.Option("--team", help="Team that owns this run.")]
ComponentOption = Annotated[
    str | None, typer.Option("--component", help="Component or service under test.")
]
TestTypeOption = Annotated[
    TestType, typer.Option("--type", help="Test type.", case_sensitive=False)
]
TriggerOption = Annotated[
    TriggerType, typer.Option("--trigger", help="What caused this run.", case_sensitive=False)
]
EnvironmentOption = Annotated[
    str | None, typer.Option("--environment", "--env", help="Environment the tests ran against.")
]
BranchOption = Annotated[str | None, typer.Option("--branch", help="Source branch.")]
CommitOption = Annotated[str | None, typer.Option("--commit", help="Commit SHA.")]
FormatOption = Annotated[
    str | None,
    typer.Option(
        "--format",
        "-f",
        help="Force a report adapter by name instead of auto-detecting. See `test-hub formats`.",
    ),
]
DataDirOption = Annotated[
    Path | None,
    typer.Option(
        "--data-dir",
        help="Where Test Hub keeps its database and artifacts.",
        envvar="TEST_HUB_DATA_DIR",
    ),
]


def read_token(token_env: str | None, token_file: Path | None) -> str | None:
    """Resolve a token from an environment variable or a file. Never from a flag.

    A token passed as ``--token secret`` is visible in shell history and in
    ``ps`` to every other user on the machine, and CI systems routinely echo the
    command they are about to run. There is deliberately no such flag.
    """
    if token_file is not None:
        if not token_file.is_file():
            raise ConfigurationError(f"token file {token_file} does not exist")
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ConfigurationError(f"token file {token_file} is empty")
        return token
    if token_env:
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise ConfigurationError(
                f"environment variable {token_env} is not set or is empty; "
                f"export it before running, or use --token-file"
            )
        return token
    return None


def settings_from_cli(**overrides: Any) -> Settings:
    """Build settings with CLI flags layered on top of the environment."""
    supplied = {key: value for key, value in overrides.items() if value is not None}
    if not supplied:
        return get_settings()
    base = get_settings().model_dump()
    base.update(supplied)
    return Settings(**base)
