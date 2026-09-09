"""``test-hub publish`` — send a report to a remote Test Hub.

Identical options to ``ingest``, pointed at a URL instead of a directory. The
token is read from an environment variable or a file, never from a flag — see
:func:`test_hub.cli.options.read_token` for why.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

import typer

from test_hub.cli.options import (
    BranchOption,
    CommitOption,
    ComponentOption,
    EnvironmentOption,
    FormatOption,
    ProjectOption,
    TeamOption,
    TestTypeOption,
    TriggerOption,
    read_token,
)
from test_hub.cli.render import render_import_result
from test_hub.domain.models import RunContext, TestType, TriggerType


def publish(
    path: Annotated[
        Path,
        typer.Argument(help="Report file to publish.", exists=True, dir_okay=False, readable=True),
    ],
    project: ProjectOption,
    url: Annotated[
        str, typer.Option("--url", help="Base URL of the Test Hub server.", envvar="TEST_HUB_URL")
    ] = "http://127.0.0.1:8000",
    team: TeamOption = None,
    component: ComponentOption = None,
    type: TestTypeOption = TestType.OTHER,
    trigger: TriggerOption = TriggerType.UNKNOWN,
    environment: EnvironmentOption = None,
    branch: BranchOption = None,
    commit: CommitOption = None,
    framework: Annotated[str | None, typer.Option("--framework")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    pipeline_id: Annotated[str | None, typer.Option("--pipeline-id")] = None,
    pipeline_url: Annotated[str | None, typer.Option("--pipeline-url")] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags.")] = None,
    run_id: Annotated[uuid.UUID | None, typer.Option("--run-id")] = None,
    format: FormatOption = None,
    token_env: Annotated[
        str | None,
        typer.Option("--token-env", help="Environment variable holding the API token."),
    ] = "TEST_HUB_TOKEN",  # noqa: S107 - the NAME of a variable, not a token
    token_file: Annotated[
        Path | None, typer.Option("--token-file", help="File containing the API token.")
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds.")] = 60.0,
    insecure: Annotated[
        bool, typer.Option("--insecure", help="Skip TLS certificate verification.")
    ] = False,
    idempotency_key: Annotated[
        str | None,
        typer.Option(
            "--idempotency-key", help="Makes a retried publish a no-op instead of a duplicate."
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Send a report to a remote Test Hub server."""
    from test_hub.sdk import TestHubClient

    # An unset variable is not an error here: an unauthenticated local server is
    # the normal development case, and read_token only raises when the user
    # explicitly named a variable that is empty.
    token = None
    try:
        token = read_token(token_env, token_file)
    except Exception:
        if token_file is not None or token_env not in (None, "TEST_HUB_TOKEN"):
            raise

    with TestHubClient(url, token=token, timeout=timeout, verify=not insecure) as client:
        result = client.publish_report(
            path,
            project_key=project,
            run_id=run_id,
            name=name,
            team=team,
            component=component,
            test_type=type,
            trigger=trigger,
            context=RunContext(
                framework=framework,
                language=language,
                environment=environment,
                branch=branch,
                commit_sha=commit,
                pipeline_id=pipeline_id,
                pipeline_url=pipeline_url,
            ),
            tags=[tag.strip() for tag in (tags or "").split(",") if tag.strip()],
            explicit_format=format,
            idempotency_key=idempotency_key,
        )

    render_import_result(result, json_output=json_output, destination=url)
