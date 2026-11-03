"""``test-hub ingest`` — import a report into the local store.

The offline half of the pair. ``ingest`` writes straight to the local database;
``publish`` sends the same file to a remote server. They take the same options on
purpose, so moving a script from a laptop to CI is a one-word change.
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
    DatabaseUrlOption,
    DataDirOption,
    EnvironmentOption,
    FormatOption,
    ProjectOption,
    TeamOption,
    TestTypeOption,
    TriggerOption,
    settings_from_cli,
    storage_location,
)
from test_hub.cli.render import render_import_result
from test_hub.domain.models import RunContext, TestType, TriggerType


def ingest(
    path: Annotated[
        Path,
        typer.Argument(help="Report file to import.", exists=True, dir_okay=False, readable=True),
    ],
    project: ProjectOption,
    team: TeamOption = None,
    component: ComponentOption = None,
    type: TestTypeOption = TestType.OTHER,
    trigger: TriggerOption = TriggerType.LOCAL,
    environment: EnvironmentOption = None,
    branch: BranchOption = None,
    commit: CommitOption = None,
    framework: Annotated[str | None, typer.Option("--framework")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    pipeline_id: Annotated[str | None, typer.Option("--pipeline-id")] = None,
    pipeline_url: Annotated[str | None, typer.Option("--pipeline-url")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Human-readable run name.")] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags.")] = None,
    run_id: Annotated[
        uuid.UUID | None,
        typer.Option("--run-id", help="Import into an existing run instead of a new one."),
    ] = None,
    format: FormatOption = None,
    data_dir: DataDirOption = None,
    database_url: DatabaseUrlOption = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Import a report file into the local store."""
    from test_hub.service.imports import ImportRequest, ImportService
    from test_hub.service.runtime import create_context

    settings = settings_from_cli(data_dir=data_dir, database_url=database_url)
    context = create_context(settings)
    try:
        service = ImportService(context)
        request = ImportRequest(
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
            filename=path.name,
            source="urn:test-hub:cli-ingest",
        )
        with path.open("rb") as handle:
            result = service.import_stream(handle, request)
    finally:
        context.close()

    render_import_result(result, json_output=json_output, destination=storage_location(settings))
