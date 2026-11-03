"""``test-hub prune`` — delete runs older than the operator wants to keep.

The only destructive command in the CLI, and the only way to delete anything at
all: there is deliberately no REST equivalent, because authentication has two
scopes and ``require_write`` is the one every CI publisher token holds. A prune
endpoint behind it would let a token that lives in a repository's CI settings
delete a year of history (ADR-0002 D8).

Nothing sweeps on its own. ``TEST_HUB_RETENTION_DAYS`` is the default cutoff for
*this command*, and a retention policy is this command in a cron entry — put at
an hour you chose, because it takes SQLite's write lock in batches and
publishers queue behind each one.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

import typer

from test_hub.cli.options import DataDirOption, settings_from_cli
from test_hub.cli.render import render_prune_result

if TYPE_CHECKING:  # pragma: no cover - typing only
    from test_hub.service.retention import PruneResult


def prune(
    older_than: Annotated[
        str | None,
        typer.Option(
            "--older-than",
            help="Delete runs older than this: 12h, 90d, 2w, 6m, 1y, or a bare number "
            "of days. m is 30 days and y is 365 — not calendar months or years.",
        ),
    ] = None,
    before: Annotated[
        datetime | None,
        typer.Option(
            "--before", help="Delete runs from before this date. UTC; midnight if no time."
        ),
    ] = None,
    project: Annotated[
        list[str] | None,
        typer.Option("--project", "-p", help="Restrict to these project keys. Repeatable."),
    ] = None,
    keep_min: Annotated[
        int | None,
        typer.Option(
            "--keep-min",
            help="Always keep this many recent runs per project, however old. 0 disables.",
        ),
    ] = None,
    by_import_date: Annotated[
        bool,
        typer.Option(
            "--by-import-date",
            help="Cut on when the run was imported instead of when it ran.",
        ),
    ] = False,
    include_unfinished: Annotated[
        bool,
        typer.Option(
            "--include-unfinished", help="Also delete queued and running runs past the cutoff."
        ),
    ] = False,
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Runs per transaction.", min=1)
    ] = 100,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would go. Deletes nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    vacuum: Annotated[
        bool,
        typer.Option("--vacuum", help="Return the freed pages to the filesystem afterwards."),
    ] = False,
    data_dir: DataDirOption = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Delete runs older than a cutoff, and the artifacts nothing else references."""
    from test_hub.service.retention import RetentionPolicy, RetentionService, resolve_cutoff
    from test_hub.service.runtime import create_context

    settings = settings_from_cli(data_dir=data_dir)
    cutoff = resolve_cutoff(
        older_than=older_than,
        before=before,
        default_days=settings.retention_days,
    )
    policy = RetentionPolicy(
        cutoff=cutoff,
        keep_min=settings.retention_min_runs if keep_min is None else keep_min,
        project_keys=tuple(project or ()),
        by_import_date=by_import_date,
        include_unfinished=include_unfinished,
        batch_size=batch_size,
        grace_seconds=settings.artifact_gc_grace_hours * 3600.0,
    )

    context = create_context(settings)
    try:
        service = RetentionService(context)
        plan = service.plan(policy)
        if dry_run:
            render_prune_result(plan, json_output=json_output)
            return
        if not plan.runs:
            render_prune_result(plan, json_output=json_output)
            return
        if not yes:
            _confirm(plan, json_output=json_output)
        result = service.prune(policy, vacuum=vacuum)
        render_prune_result(result, json_output=json_output)
    finally:
        context.close()


def _confirm(plan: PruneResult, *, json_output: bool) -> None:
    """Ask, unless there is nobody to ask.

    A cron entry that forgot ``--yes`` must fail rather than block: prompting
    into a pipe hangs the job until somebody notices, which is a worse outcome
    than a loud non-zero exit on the first scheduled run.
    """
    if json_output or not _interactive():
        raise typer.BadParameter(
            f"this would delete {plan.runs} run(s) and everything under them, and there "
            f"is no terminal to confirm at. Pass --yes to go ahead, or --dry-run to see "
            f"the plan.",
            param_hint="--yes",
        )
    render_prune_result(plan, json_output=False)
    typer.confirm(f"  delete {plan.runs} run(s)?", abort=True)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
