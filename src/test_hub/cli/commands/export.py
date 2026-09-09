"""``test-hub export`` — write a self-contained offline HTML report.

Two sources, one output:

* ``--input ./results`` parses report files into a throwaway database and exports
  that. Nothing touches your real store, so this is safe to run on a build agent
  against a directory of shard results.
* no ``--input`` exports what is already in the local store, honouring the same
  filters the dashboard offers.

The result is one file. Open it with the network cable pulled out; it has no
external references at all.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

import typer

from test_hub.cli.options import DataDirOption, FormatOption, settings_from_cli
from test_hub.config import Settings
from test_hub.domain.models import TestType, TriggerType
from test_hub.errors import ConfigurationError
from test_hub.logging import get_logger

logger = get_logger(__name__)

#: Files we will try to parse when walking an --input directory. Anything else is
#: skipped silently: a results folder routinely contains screenshots and logs,
#: and warning about each one would bury the warnings that matter.
_CANDIDATE_SUFFIXES = {".xml", ".json"}


def export(
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Where to write the HTML file.")
    ] = Path("test-hub-report.html"),
    input_path: Annotated[
        Path | None,
        typer.Option(
            "--input",
            "-i",
            help="Directory or file of reports to export instead of the local store.",
            exists=True,
        ),
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project key for --input reports.")
    ] = None,
    team: Annotated[str | None, typer.Option("--team")] = None,
    component: Annotated[str | None, typer.Option("--component")] = None,
    type: Annotated[TestType, typer.Option("--type", case_sensitive=False)] = TestType.OTHER,
    title: Annotated[
        str, typer.Option("--title", help="Title shown in the report.")
    ] = "Test Hub report",
    window_days: Annotated[int, typer.Option("--days", help="How much history to include.")] = 30,
    max_runs: Annotated[int, typer.Option("--max-runs")] = 200,
    format: FormatOption = None,
    data_dir: DataDirOption = None,
    open_browser: Annotated[
        bool, typer.Option("--open", help="Open the report when done.")
    ] = False,
) -> None:
    """Write a self-contained offline HTML report."""
    from test_hub.export.snapshot import ExportOptions, SnapshotExporter
    from test_hub.service.runtime import create_context

    options = ExportOptions(title=title, window_days=window_days, max_runs=max_runs)

    if input_path is not None:
        if not project:
            raise ConfigurationError(
                "--project is required with --input: report files do not carry a project key, "
                "so Test Hub cannot guess which project they belong to"
            )
        written = _export_from_files(
            input_path,
            output,
            options,
            project=project,
            team=team,
            component=component,
            test_type=type,
            explicit_format=format,
        )
    else:
        settings = settings_from_cli(data_dir=data_dir)
        context = create_context(settings)
        try:
            written = SnapshotExporter(context).export(output, options)
        finally:
            context.close()

    size_kb = written.stat().st_size / 1024
    typer.echo()
    typer.secho(f"  wrote {written}", fg=typer.colors.GREEN, bold=True)
    typer.secho(
        f"  {size_kb:,.0f} KB, self-contained — no network needed to open it",
        fg=typer.colors.BRIGHT_BLACK,
    )
    typer.echo()

    if open_browser:
        import webbrowser

        webbrowser.open(written.resolve().as_uri())


def _export_from_files(
    input_path: Path,
    output: Path,
    options: object,
    *,
    project: str,
    team: str | None,
    component: str | None,
    test_type: TestType,
    explicit_format: str | None,
) -> Path:
    """Parse a directory of reports into a temporary store and export that.

    A temporary database rather than an in-memory one: the same code path as a
    real store, so an export cannot pass while ``serve`` fails on the identical
    data.
    """
    from test_hub.export.snapshot import SnapshotExporter
    from test_hub.service.imports import ImportRequest, ImportService
    from test_hub.service.runtime import create_context

    files = _collect(input_path)
    if not files:
        raise ConfigurationError(
            f"no .xml or .json report files found under {input_path}",
            detail={"searched": str(input_path)},
        )

    with tempfile.TemporaryDirectory(prefix="test-hub-export-") as temporary:
        settings = Settings(
            data_dir=Path(temporary),
            store_raw_reports=False,  # nothing will ever read them back
        )
        context = create_context(settings, configure_logs=False)
        try:
            service = ImportService(context)
            imported = 0
            for path in files:
                request = ImportRequest(
                    project_key=project,
                    team=team,
                    component=component,
                    test_type=test_type,
                    trigger=TriggerType.LOCAL,
                    explicit_format=explicit_format,
                    filename=path.name,
                    source="urn:test-hub:cli-export",
                )
                try:
                    with path.open("rb") as handle:
                        service.import_stream(handle, request)
                    imported += 1
                    typer.secho(f"  parsed {path.name}", fg=typer.colors.BRIGHT_BLACK)
                except Exception as exc:
                    # One unreadable file in a results directory should not throw
                    # away every other file's results.
                    typer.secho(f"  skipped {path.name}: {exc}", fg=typer.colors.YELLOW)
            if imported == 0:
                raise ConfigurationError(
                    f"none of the {len(files)} candidate file(s) under {input_path} "
                    "could be parsed by an installed adapter"
                )
            return SnapshotExporter(context).export(output, options)  # type: ignore[arg-type]
        finally:
            context.close()


def _collect(input_path: Path) -> list[Path]:
    """Every candidate report file under ``input_path``, sorted for reproducibility."""
    if input_path.is_file():
        return [input_path]
    return sorted(
        path
        for path in input_path.rglob("*")
        if path.is_file() and path.suffix.lower() in _CANDIDATE_SUFFIXES
    )
