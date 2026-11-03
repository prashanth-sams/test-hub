"""The demo store has to be able to exercise the evidence pages.

Screenshots, API Logs and Test Coverage are pages whose whole subject is what a
run attached to itself. If ``test-hub demo`` produces no attachments, all three
render as "nothing was captured" and anyone judging the product concludes the
feature is missing. These tests are what keeps that from happening quietly.
"""

from __future__ import annotations

import struct
import uuid
import zlib
from typing import Any

from test_hub.demo.evidence import coverage_report, request_log, screenshot_png, steps_for
from test_hub.demo.seed import seed_demo
from test_hub.domain.models import ArtifactKind, utcnow

# ------------------------------------------------------------------ the PNG


def _png_header(data: bytes) -> tuple[int, int]:
    """Width and height straight out of the IHDR chunk."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_the_generated_screenshot_is_a_valid_png() -> None:
    """It is written by hand, with no image library — so it is checked by hand."""
    data = screenshot_png("e2e/basket.spec.ts::adds an item", failed=True)
    width, height = _png_header(data)
    assert (width, height) == (480, 300)
    assert data[-12:] == struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND")), (
        "the file does not end with a well-formed IEND"
    )

    # The pixels have to survive the round trip: one filter byte per row, then
    # three bytes per pixel. A wrong stride is a PNG that some decoders open and
    # others reject, which is the worst way for this to fail.
    start = data.index(b"IDAT") + 4
    length = struct.unpack(">I", data[start - 8 : start - 4])[0]
    raw = zlib.decompress(data[start : start + length])
    assert len(raw) == height * (1 + width * 3)
    assert set(raw[:: 1 + width * 3]) == {0}, "every row must declare filter type 0"
    assert len(data) < 50_000, "a demo screenshot should not be a large file"


def test_screenshots_are_stable_and_distinct() -> None:
    """Stable, because content-addressed storage dedupes identical bytes and a
    picture that changed on every seeding would defeat that. Distinct, because a
    gallery where every capture is the same picture demonstrates nothing."""
    first = screenshot_png("e2e/search.spec.ts::finds a product", failed=False)
    again = screenshot_png("e2e/search.spec.ts::finds a product", failed=False)
    other = screenshot_png("e2e/checkout.spec.ts::completes a payment", failed=False)
    failing = screenshot_png("e2e/search.spec.ts::finds a product", failed=True)
    assert first == again
    assert first != other
    assert first != failing


# ------------------------------------------------------------------- files


def test_the_coverage_report_is_the_one_that_toolchain_writes() -> None:
    cobertura = coverage_report("payments-api", "coverage.xml").decode()
    jacoco = coverage_report("ledger-worker", "jacoco.xml").decode()
    assert cobertura.startswith("<?xml") and "<coverage" in cobertura
    assert "line-rate" in cobertura
    assert "<report" in jacoco and 'type="LINE"' in jacoco


def test_the_request_log_reads_like_a_log() -> None:
    text = request_log("identity-service", utcnow()).decode()
    lines = text.strip().splitlines()
    assert len(lines) > 10
    assert any(" -> 422 " in line for line in lines), "no failing request to look at"
    assert any(" -> 200 " in line for line in lines)


def test_only_the_browser_suite_publishes_steps() -> None:
    """The Test Steps page has to be legible both ways round, so the sample data
    has to contain both: a suite that publishes steps and suites that do not."""
    assert steps_for("e2e/basket.spec.ts", "adds an item to the basket")
    assert steps_for("tests/checkout/test_capture.py", "test_captures_the_full_amount") == []

    steps = steps_for("e2e/checkout.spec.ts", "completes a card payment")
    assert str(steps[0]["name"]).startswith("Given ")
    assert all(isinstance(step["duration_ms"], int) for step in steps)


# ------------------------------------------------------------------ seeding


def test_the_demo_attaches_evidence_of_every_kind(context: Any) -> None:
    result = seed_demo(context, days=4, runs_per_project=2, include_live_run=False)
    assert result.artifacts > 0, "the demo attached nothing to look at"

    with context.uow_factory(read_only=True) as uow:
        runs = uow.runs.list_runs(_all_runs(context.workspace_id))
        kinds: set[ArtifactKind] = set()
        by_run: dict[uuid.UUID, int] = {}
        for summary in runs:
            artifacts = uow.artifacts.list_for_run(summary.id)
            by_run[summary.id] = len(artifacts)
            kinds.update(artifact.kind for artifact in artifacts)

    assert ArtifactKind.SCREENSHOT in kinds, "the Screenshots page would be empty"
    assert ArtifactKind.COVERAGE in kinds, "the Test Coverage page would be empty"
    assert ArtifactKind.LOG in kinds, "the API Logs page would have no files"
    # …and a suite that attaches nothing, because that state has to be seen too.
    assert any(count == 0 for count in by_run.values())


def test_a_screenshot_belongs_to_a_test_that_ended_red(context: Any) -> None:
    """A capture is evidence of a failure. Attaching one to a test that passed on
    retry would caption a red screenshot with a green status."""
    seed_demo(context, days=4, runs_per_project=2, include_live_run=False)

    with context.uow_factory(read_only=True) as uow:
        for summary in uow.runs.list_runs(_all_runs(context.workspace_id)):
            detail = uow.runs.get_run_detail(summary.id, comparison=False)
            assert detail is not None
            statuses = {test.id: test.status for test in detail.tests}
            for artifact in detail.artifacts:
                if artifact.kind is not ArtifactKind.SCREENSHOT:
                    continue
                assert artifact.test_id is not None, "a capture with no test to caption it"
                assert str(statuses[artifact.test_id]) in {"failed", "error"}


def test_the_demo_publishes_steps_for_the_browser_suite(context: Any) -> None:
    seed_demo(context, days=4, runs_per_project=2, include_live_run=False)

    found = 0
    with context.uow_factory(read_only=True) as uow:
        for summary in uow.runs.list_runs(_all_runs(context.workspace_id)):
            detail = uow.runs.get_run_detail(summary.id, comparison=False)
            assert detail is not None
            for test in detail.tests:
                for attempt in test.attempts:
                    steps = attempt.metadata.get("steps")
                    if steps:
                        found += 1
                        assert test.suite_path.startswith("e2e/")
                        assert all("name" in step for step in steps)
    assert found, "no attempt carries steps, so the Test Steps page has nothing"


def _all_runs(workspace_id: uuid.UUID) -> Any:
    from test_hub.storage.base import RunFilter

    return RunFilter(workspace_id=workspace_id, limit=200)
