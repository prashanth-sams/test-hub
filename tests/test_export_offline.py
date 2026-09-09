"""The standalone HTML export: self-contained, offline, and not truncated.

Two of these tests exist because the bugs happened. Read the comments before
deleting one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from test_hub.domain.models import TestType
from test_hub.export.snapshot import (
    ExportOptions,
    SnapshotExporter,
    _concat,
    _neutralize,
    read_manifest,
)
from test_hub.service.imports import ImportRequest, ImportService
from tests.paths import junit


@pytest.fixture
def exported(context: Any, tmp_path: Path) -> str:
    for name in ("simple.xml", "surefire-retries.xml", "html-injection.xml"):
        with junit(name).open("rb") as handle:
            ImportService(context).import_stream(
                handle,
                ImportRequest(
                    project_key="storefront-web",
                    team="growth",
                    test_type=TestType.E2E,
                    filename=name,
                ),
            )
    output = SnapshotExporter(context).export(
        tmp_path / "report.html", ExportOptions(title="Nightly results", window_days=3650)
    )
    return output.read_text(encoding="utf-8")


def test_the_export_is_a_single_file_with_no_external_references(exported: str) -> None:
    assert 'src="assets/' not in exported
    assert 'href="assets/' not in exported
    assert "<style>" in exported
    for attribute in ("src", "href"):
        remote = re.findall(rf'{attribute}\s*=\s*"(https?://[^"]+)"', exported)
        assert remote == [], f"{attribute} points off-origin: {remote}"


def test_the_inlined_script_is_not_truncated_by_its_own_contents(exported: str) -> None:
    """Regression: a comment in the dashboard source contained `</script>`.

    HTML does not parse the contents of a script element — the only thing that
    can close it is the literal closing tag — so a `</script>` anywhere in the
    inlined JavaScript, *including inside a comment*, ends the element early and
    truncates the bundle. The exported report then loads a syntactically invalid
    script and silently renders nothing at all.
    """
    manifest = read_manifest()

    # Locate the bundle by its per-file markers rather than by tag arithmetic:
    # the dashboard's own source legitimately contains the *opening* string
    # `<script>` inside a comment, so scanning for tags finds the wrong place.
    positions = []
    for relative in manifest["js"]:
        marker = f"/* ---- {relative} ---- */"
        assert marker in exported, f"{relative} is missing from the export — bundle truncated"
        positions.append(exported.index(marker))

    assert positions == sorted(positions), "assets were inlined out of manifest order"

    # Nothing between the first and last marker may close the script element.
    bundle = exported[positions[0] : positions[-1]]
    assert "</script" not in bundle.lower()


def test_neutralize_defuses_element_breakouts() -> None:
    assert "</script" not in _neutralize("var x = '</script>';", "js")
    assert "</SCRIPT" not in _neutralize("// </SCRIPT>", "js").upper().replace("<\\/SCRIPT", "")
    assert "</style" not in _neutralize("/* </style> */", "css")


def test_concat_refuses_to_emit_a_breakout_sequence() -> None:
    """The guard rail behind the fix — if neutralization ever regresses, this fails."""
    from test_hub.api.app import web_root

    manifest = read_manifest()
    joined = _concat(web_root(), manifest["js"], kind="js")
    assert "</script" not in joined.lower()


def test_a_hostile_title_is_escaped_not_injected(context: Any, tmp_path: Path) -> None:
    """Regression: the template is `snapshot.html.j2`, and `select_autoescape`
    keyed off the `.j2` extension and decided it was not HTML."""
    with junit("simple.xml").open("rb") as handle:
        ImportService(context).import_stream(
            handle, ImportRequest(project_key="p", filename="simple.xml")
        )
    output = SnapshotExporter(context).export(
        tmp_path / "r.html", ExportOptions(title="<script>alert(1)</script>", window_days=3650)
    )
    html = output.read_text(encoding="utf-8")
    assert "<title>&lt;script&gt;alert(1)&lt;/script&gt;</title>" in html


def test_the_snapshot_data_island_cannot_be_escaped_from(exported: str) -> None:
    island = exported.split('id="test-hub-snapshot" type="application/json">')[1]
    payload = island.split("</script>")[0]
    assert "<" not in payload
    assert ">" not in payload
    parsed = json.loads(payload)
    assert parsed["kind"] == "test-hub-snapshot"


def test_the_payload_survives_the_round_trip_intact(exported: str) -> None:
    """Escaping must not corrupt the data it protects."""
    island = exported.split('id="test-hub-snapshot" type="application/json">')[1]
    parsed = json.loads(island.split("</script>")[0])
    blob = json.dumps(parsed)
    assert "<script>alert(1)</script>" in blob, "the malicious test name was mangled or lost"


def test_the_snapshot_carries_the_data_the_dashboard_needs(exported: str) -> None:
    island = exported.split('id="test-hub-snapshot" type="application/json">')[1]
    snapshot = json.loads(island.split("</script>")[0])
    assert snapshot["schema_version"] == 1
    assert snapshot["runs"], "no runs were exported"
    assert snapshot["overview"]["total_runs_all_time"] >= 1
    assert snapshot["workspace"]["slug"]
    run = snapshot["runs"][0]
    assert "suites" in run and "tests" in run


def test_the_theme_bootstrap_runs_before_paint(exported: str) -> None:
    """A dark-theme reader must never see a white flash on open."""
    head = exported[: exported.index("</head>")]
    assert "data-theme" in exported[: exported.index("<body")]
    assert "prefers-color-scheme" in head


def test_export_is_deterministic_apart_from_the_timestamp(context: Any, tmp_path: Path) -> None:
    with junit("simple.xml").open("rb") as handle:
        ImportService(context).import_stream(
            handle, ImportRequest(project_key="p", filename="simple.xml")
        )
    exporter = SnapshotExporter(context)
    options = ExportOptions(window_days=3650)
    first = exporter.export(tmp_path / "a.html", options).read_text()
    second = exporter.export(tmp_path / "b.html", options).read_text()

    strip = re.compile(r'"generated_at":\s*"[^"]+"|TEST_HUB_GENERATED_AT = "[^"]+"')
    assert strip.sub("", first) == strip.sub("", second)
