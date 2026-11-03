"""Terminal output for the CLI.

Separated from the commands so that every command reports an import the same way,
and so the ``--json`` path is a genuine alternative rather than a second
formatter that drifts. Colour goes through Typer, which already knows to drop it
when the output is not a terminal — so piping to a file gives clean text and CI
logs stay readable.
"""

from __future__ import annotations

import json
from typing import Any

import typer

from test_hub.domain.models import ImportResult, RunCounts
from test_hub.service.retention import PruneResult

__all__ = [
    "render_counts",
    "render_import_result",
    "render_prune_result",
    "render_warnings",
]

_STATUS_COLOURS = {
    "passed": typer.colors.GREEN,
    "failed": typer.colors.RED,
    "error": typer.colors.BRIGHT_RED,
    "skipped": typer.colors.YELLOW,
    "xfailed": typer.colors.MAGENTA,
    "xpassed": typer.colors.MAGENTA,
    "flaky": typer.colors.YELLOW,
    "running": typer.colors.BLUE,
}


def render_counts(counts: RunCounts) -> None:
    """One line of totals, colour-coded, zero counts omitted."""
    parts: list[str] = [typer.style(f"{counts.total} tests", bold=True)]
    for name in ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "flaky"):
        value = getattr(counts, name)
        if value:
            parts.append(typer.style(f"{value} {name}", fg=_STATUS_COLOURS.get(name)))
    rate = counts.pass_rate
    parts.append(f"{rate:.1f}% pass" if rate is not None else "pass rate —")
    typer.echo("  " + "  ".join(parts))


def render_warnings(warnings: list[Any], *, limit: int = 10) -> None:
    """Parser warnings, capped so a pathological file cannot flood the terminal."""
    if not warnings:
        return
    typer.echo()
    typer.secho(f"  {len(warnings)} parser warning(s):", fg=typer.colors.YELLOW)
    for warning in warnings[:limit]:
        location = f" [{warning.location}]" if warning.location else ""
        typer.secho(
            f"    {warning.code}{location}: {warning.message}", fg=typer.colors.BRIGHT_BLACK
        )
    if len(warnings) > limit:
        typer.secho(f"    … and {len(warnings) - limit} more", fg=typer.colors.BRIGHT_BLACK)


def render_import_result(
    result: ImportResult,
    *,
    json_output: bool = False,
    destination: str | None = None,
) -> None:
    """Report an import, either as JSON or as a short human summary."""
    if json_output:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        return

    typer.echo()
    verb = "imported" if result.created else "updated"
    typer.secho(
        f"  {verb} run {result.run_id}",
        fg=typer.colors.GREEN if result.created else typer.colors.CYAN,
        bold=True,
    )
    confidence = (
        f" (detected, {result.detection_confidence:.0%} confident)"
        if result.detection_confidence is not None
        else ""
    )
    typer.secho(f"  adapter   {result.adapter}{confidence}", fg=typer.colors.BRIGHT_BLACK)
    typer.secho(f"  project   {result.project_key}", fg=typer.colors.BRIGHT_BLACK)
    if destination:
        typer.secho(f"  stored in {destination}", fg=typer.colors.BRIGHT_BLACK)
    render_counts(result.counts)
    if result.events_ignored:
        typer.secho(
            f"  {result.events_ignored} event(s) had already been applied and were ignored",
            fg=typer.colors.BRIGHT_BLACK,
        )
    render_warnings(result.warnings)
    typer.echo()


def human_bytes(count: int) -> str:
    """Bytes in the largest unit that keeps the number readable."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"  # pragma: no cover - unreachable, kept for mypy


def render_prune_plan(result: PruneResult) -> None:
    """The plan, before anything is deleted. What `--dry-run` prints."""
    typer.echo()
    if not result.runs:
        typer.secho("  nothing to prune", fg=typer.colors.GREEN, bold=True)
        _render_prune_notes(result)
        typer.echo()
        return

    typer.secho(f"  {result.runs} run(s) would be removed", fg=typer.colors.YELLOW, bold=True)
    if result.oldest and result.newest:
        typer.secho(
            f"  spanning  {result.oldest:%Y-%m-%d} to {result.newest:%Y-%m-%d}",
            fg=typer.colors.BRIGHT_BLACK,
        )
    for key, count in sorted(result.by_project.items(), key=lambda item: (-item[1], item[0])):
        typer.secho(f"    {key:<28} {count}", fg=typer.colors.BRIGHT_BLACK)
    _render_prune_rows(result)
    _render_prune_notes(result)
    typer.echo()


def render_prune_result(
    result: PruneResult, *, json_output: bool = False, backend: str = "sqlite"
) -> None:
    """What the prune actually committed."""
    if json_output:
        typer.echo(json.dumps(result.as_dict(), indent=2))
        return
    if result.dry_run:
        render_prune_plan(result)
        return

    typer.echo()
    if not result.runs:
        typer.secho("  nothing to prune", fg=typer.colors.GREEN, bold=True)
        _render_prune_notes(result)
        typer.echo()
        return

    typer.secho(
        f"  pruned {result.runs} run(s) in {result.batches} batch(es)",
        fg=typer.colors.GREEN,
        bold=True,
    )
    if result.oldest and result.newest:
        typer.secho(
            f"  spanning  {result.oldest:%Y-%m-%d} to {result.newest:%Y-%m-%d}",
            fg=typer.colors.BRIGHT_BLACK,
        )
    _render_prune_rows(result)
    _render_prune_notes(result)

    if not result.vacuumed and result.rows and backend == "sqlite":
        # Without this line an operator who pruned to free a disk sees the file
        # unchanged and concludes the command did nothing.
        typer.secho(
            "  the database file has not shrunk; SQLite reuses the freed pages. "
            "Pass --vacuum to return them to the filesystem.",
            fg=typer.colors.BRIGHT_BLACK,
        )
    elif not result.vacuumed and result.rows:
        typer.secho(
            "  PostgreSQL reuses the freed space once it is vacuumed; autovacuum will "
            "get to it, or pass --vacuum to do it now.",
            fg=typer.colors.BRIGHT_BLACK,
        )
    typer.echo()


def _render_prune_rows(result: PruneResult) -> None:
    parts = [
        f"{result.suites} suites",
        f"{result.test_cases} tests",
        f"{result.attempts} attempts",
        f"{result.events} events",
        f"{result.artifacts} artifact rows",
    ]
    if result.idempotency_keys:
        parts.append(f"{result.idempotency_keys} idempotency keys")
    typer.secho(f"  rows      {result.rows} total", fg=typer.colors.BRIGHT_BLACK)
    typer.secho("            " + ", ".join(parts), fg=typer.colors.BRIGHT_BLACK)
    if result.blobs_unlinked or result.blobs_retained:
        freed = human_bytes(result.bytes_freed)
        shared = (
            f", {result.blobs_retained} kept (still referenced or inside the grace window)"
            if result.blobs_retained
            else ""
        )
        typer.secho(
            f"  artifacts {result.blobs_unlinked} blob(s), {freed}{shared}",
            fg=typer.colors.BRIGHT_BLACK,
        )


def _render_prune_notes(result: PruneResult) -> None:
    if result.protected:
        typer.secho(
            f"  {result.protected} run(s) held back by the per-project floor (--keep-min)",
            fg=typer.colors.BRIGHT_BLACK,
        )
    if result.skipped_live:
        typer.secho(
            f"  {result.skipped_live} run(s) past the cutoff are still queued or running "
            f"and were left alone (--include-unfinished takes them)",
            fg=typer.colors.YELLOW,
        )
