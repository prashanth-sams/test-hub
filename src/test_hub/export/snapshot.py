"""The standalone HTML exporter — one file, no network, no server.

What comes out of ``test-hub export`` is the *same dashboard* the server serves,
with the same CSS and the same JavaScript, wrapped around a frozen data object
instead of a REST client (ADR-0001 D1). There is no second renderer to keep in
step, which is the entire reason the provider seam exists.

Two things here deserve care.

**One document, not two.** The exporter does not render a parallel template — it
reads the dashboard's own ``index.html`` and replaces three marker blocks with
their inlined contents. The offline report is therefore *literally* the live
page, and a change to the dashboard cannot be forgotten here.
``web/assets/manifest.json`` is the single ordered list of assets that both the
served page and this concatenation read.

**Embedding the data.** Test names, log lines and stack traces are attacker
controlled, and a naive ``json.dumps`` inside a ``<script>`` is a script-injection
bug waiting for a test called ``</script><img src=x onerror=alert(1)>``. HTML
does not parse the contents of a script element, so the *only* thing that can end
it early is the literal string ``</script``. :func:`embed_json` escapes every
character that could take part in one — plus the two Unicode line terminators
that are legal in JSON and illegal in JavaScript string literals.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from datetime import timedelta
from html import escape
from pathlib import Path
from typing import Any

from test_hub.domain.models import (
    OverviewSnapshot,
    Project,
    RunDetail,
    Snapshot,
    TestDetail,
    Workspace,
    utcnow,
)
from test_hub.logging import get_logger
from test_hub.service.runtime import AppContext
from test_hub.storage.base import RunFilter
from test_hub.version import __version__

__all__ = ["ExportOptions", "SnapshotExporter", "embed_json", "render_snapshot"]

logger = get_logger(__name__)

#: Characters that could begin an HTML tag, end the script element, or break a
#: JavaScript string literal. Escaped as JSON-legal ``\uXXXX`` sequences, so the
#: value the browser parses is byte-for-byte the value we serialized.
_JSON_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\u2028": "\\u2028",  # LINE SEPARATOR: legal in JSON, illegal in a JS string
    "\u2029": "\\u2029",  # PARAGRAPH SEPARATOR: likewise
}


def embed_json(data: Any) -> str:
    """Serialize ``data`` so it is safe to place inside a ``<script>`` element.

    The output is still valid JSON — ``JSON.parse`` gets exactly the original
    string back — but contains no character sequence an HTML parser can act on.
    """
    text = json.dumps(data, ensure_ascii=False, default=str)
    for character, replacement in _JSON_ESCAPES.items():
        text = text.replace(character, replacement)
    return text


class ExportOptions:
    """What to include in an export."""

    def __init__(
        self,
        *,
        title: str = "Test Hub report",
        window_days: int = 30,
        max_runs: int = 200,
        max_tests: int = 500,
        run_filter: RunFilter | None = None,
        include_artifacts: bool = False,
    ) -> None:
        self.title = title
        self.window_days = window_days
        self.max_runs = max_runs
        self.max_tests = max_tests
        self.run_filter = run_filter
        self.include_artifacts = include_artifacts


class SnapshotExporter:
    """Builds a :class:`~test_hub.domain.models.Snapshot` and renders it to HTML."""

    def __init__(self, context: AppContext) -> None:
        self._context = context

    # -------------------------------------------------------------- build

    def build(self, options: ExportOptions | None = None) -> Snapshot:
        """Materialize everything the offline report needs, in one payload."""
        options = options or ExportOptions()
        workspace_id = self._context.workspace_id

        # Read-only: a full export walks every run in the window, and holding
        # SQLite's write lock for that long would stall every publisher.
        with self._context.uow_factory(read_only=True) as uow:
            workspace = uow.runs.get_workspace(workspace_id) or Workspace(
                id=workspace_id,
                slug=self._context.settings.workspace_slug,
                name=self._context.settings.workspace_name,
            )
            projects: list[Project] = uow.runs.list_projects(workspace_id)
            overview: OverviewSnapshot = uow.runs.overview(
                workspace_id, window_days=options.window_days
            )

            run_filter = options.run_filter or RunFilter(
                workspace_id=workspace_id,
                since=utcnow() - timedelta(days=options.window_days),
                limit=options.max_runs,
            )
            run_filter.workspace_id = workspace_id
            run_filter.limit = min(run_filter.limit or options.max_runs, options.max_runs)
            summaries = uow.runs.list_runs(run_filter)

            runs: list[RunDetail] = []
            test_ids: set[uuid.UUID] = set()
            for summary in summaries:
                detail = uow.runs.get_run_detail(summary.id, comparison=True)
                if detail is None:  # pragma: no cover - deleted mid-export
                    continue
                if not options.include_artifacts:
                    detail.artifacts = []
                runs.append(detail)
                test_ids.update(test.id for test in detail.tests)

            tests: list[TestDetail] = []
            for test_id in list(test_ids)[: options.max_tests]:
                detail_test = uow.runs.get_test_detail(
                    test_id, workspace_id=workspace_id, history=20
                )
                if detail_test is not None:
                    tests.append(detail_test)

        logger.info(
            "snapshot built: %s runs, %s tests, %s projects",
            len(runs),
            len(tests),
            len(projects),
        )
        return Snapshot(
            generated_at=utcnow(),
            generator_version=__version__,
            title=options.title,
            workspace=workspace,
            projects=projects,
            overview=overview,
            runs=runs,
            tests=tests,
        )

    # ------------------------------------------------------------- render

    def export(self, output: Path, options: ExportOptions | None = None) -> Path:
        """Build a snapshot and write it as one self-contained HTML file."""
        snapshot = self.build(options)
        html = render_snapshot(snapshot)
        output = output.expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html, encoding="utf-8")
        return output


def _web_root() -> Path:
    from test_hub.api.app import web_root

    return web_root()


def read_manifest() -> dict[str, Any]:
    """The single ordered list of assets, shared by the server and the exporter."""
    manifest_path = _web_root() / "assets" / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"the dashboard asset manifest is missing from {manifest_path}; "
            "this build of test-hub is incomplete"
        )
    data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data


#: Sequences that would end the element they are being inlined into. HTML does
#: not parse the *contents* of <script> or <style> — the only thing that can
#: close them early is the literal closing tag — so neutralising these two
#: strings is sufficient, and neutralising them is mandatory.
_ELEMENT_BREAKOUTS = {
    "js": (("</script", "<\\/script"), ("<!--", "<\\!--")),
    "css": (("</style", "<\\/style"),),
}


def _neutralize(source: str, kind: str) -> str:
    """Stop inlined source from closing the element it is being inlined into.

    This is not hypothetical. ``40-components.js`` explains its escaping
    contract with a comment containing a literal ``<script>alert(1)</script>``,
    and a browser inlining that verbatim ends the script element in the middle
    of the bundle — the offline report then loads a truncated file and silently
    renders nothing. The replacements are inert everywhere they can legally
    appear: ``<\\/script`` is the same string to a JavaScript string literal and
    to a regular expression, and inside a comment it is just text.
    """
    for needle, replacement in _ELEMENT_BREAKOUTS.get(kind, ()):
        source = source.replace(needle, replacement)
        source = source.replace(needle.upper(), replacement)
    return source


def _concat(root: Path, relative_paths: Sequence[str], *, kind: str) -> str:
    """Concatenate assets in manifest order, with a marker between them.

    The markers cost a few bytes and make a 6 000-line inline block navigable
    when somebody views source to work out where a rule came from.
    """
    chunks: list[str] = []
    for relative in relative_paths:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"asset {relative} listed in the manifest does not exist")
        chunks.append(f"/* ---- {relative} ---- */\n{path.read_text(encoding='utf-8')}")
    joined = _neutralize("\n".join(chunks), kind)

    closing = "</script" if kind == "js" else "</style"
    if closing in joined.lower():
        raise ValueError(
            f"the inlined {kind} still contains {closing!r}, which would truncate the "
            "exported report; _neutralize did not cover this case"
        )
    logger.debug("inlined %s %s asset(s)", len(chunks), kind)
    return joined


def _replace_block(html: str, start: str, end: str, replacement: str, *, what: str) -> str:
    """Swap everything between two marker comments for ``replacement``.

    Raises rather than silently producing a report with no stylesheet: a missing
    marker means ``index.html`` and this exporter have drifted apart, and a
    half-inlined file that looks fine until it is opened offline is the worst
    possible failure mode.
    """
    begin = html.find(start)
    finish = html.find(end)
    if begin == -1 or finish == -1 or finish < begin:
        raise ValueError(
            f"could not find the {what} markers ({start} … {end}) in index.html; "
            "the dashboard and the exporter have drifted apart"
        )
    return html[:begin] + replacement + html[finish + len(end) :]


def render_snapshot(snapshot: Snapshot) -> str:
    """Render a snapshot into a single HTML document with everything inlined.

    Crucially, this *transforms the dashboard's own ``index.html``* rather than
    rendering a parallel template. There is no second document to keep in step:
    the offline report is the live page with its three external references
    replaced by their contents, which is the strongest possible form of
    ADR-0001 D1.
    """
    root = _web_root()
    manifest = read_manifest()
    markers = manifest.get("markers", {})

    index = root / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"the dashboard is missing from {index}")
    html = index.read_text(encoding="utf-8")

    styles = f"<style>\n{_concat(root, manifest.get('css', []), kind='css')}\n</style>"
    scripts = f"<script>\n{_concat(root, manifest.get('js', []), kind='js')}\n</script>"

    payload = snapshot.model_dump(mode="json")
    global_name = manifest.get("snapshot_global", "TEST_HUB_SNAPSHOT")
    data_block = (
        f'<script id="test-hub-snapshot" type="application/json">'
        f"{embed_json(payload)}</script>\n"
        "<script>\n"
        "/* Parsed from a data island rather than assigned inline, so the payload only\n"
        "   has to be valid JSON — never valid JavaScript. embed_json has already\n"
        "   escaped every character an HTML parser could act on. */\n"
        "(function () {\n"
        '  var node = document.getElementById("test-hub-snapshot");\n'
        "  try {\n"
        f"    window.{global_name} = JSON.parse(node.textContent);\n"
        "  } catch (error) {\n"
        f"    window.{global_name}_ERROR = String(error);\n"
        "  }\n"
        f"  window.TEST_HUB_GENERATED_AT = {json.dumps(snapshot.generated_at.isoformat())};\n"
        "})();\n"
        "</script>"
    )

    html = _replace_block(
        html,
        markers.get("styles_start", "<!-- test-hub:styles:start -->"),
        markers.get("styles_end", "<!-- test-hub:styles:end -->"),
        styles,
        what="stylesheet",
    )
    html = _replace_block(
        html,
        markers.get("scripts_start", "<!-- test-hub:scripts:start -->"),
        markers.get("scripts_end", "<!-- test-hub:scripts:end -->"),
        scripts,
        what="script",
    )

    snapshot_marker = markers.get("snapshot", "<!-- test-hub:snapshot -->")
    if snapshot_marker not in html:
        raise ValueError(f"could not find the snapshot marker ({snapshot_marker}) in index.html")
    html = html.replace(snapshot_marker, data_block, 1)

    # The title is the one piece of user-supplied text in the document chrome.
    title = escape(snapshot.title, quote=False)
    html = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", html, count=1, flags=re.S)

    if 'src="assets/' in html or 'href="assets/' in html:
        raise ValueError(
            "the exported report still references external assets; inlining did not complete"
        )
    return html
