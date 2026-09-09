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

__all__ = ["render_counts", "render_import_result", "render_warnings"]

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
