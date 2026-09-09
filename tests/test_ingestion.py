"""Ingestion end to end: adapters, the fold, idempotency, retries and limits."""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from test_hub.domain.models import TestType
from test_hub.domain.status import RunStatus, TestStatus
from test_hub.errors import AdapterError, PayloadTooLargeError, UnknownFormatError
from test_hub.service.imports import ImportRequest, ImportService
from tests.paths import canonical, junit


def import_file(context: Any, path: Path, **overrides: Any) -> Any:
    request = ImportRequest(
        project_key=overrides.pop("project_key", "payments-api"),
        team=overrides.pop("team", "payments"),
        test_type=overrides.pop("test_type", TestType.COMPONENT),
        filename=path.name,
        **overrides,
    )
    with path.open("rb") as handle:
        return ImportService(context).import_stream(handle, request)


# ------------------------------------------------------------------ JUnit


def test_a_simple_junit_report_imports_with_the_right_counts(context: Any) -> None:
    result = import_file(context, junit("simple.xml"))
    assert result.adapter == "junit-xml"
    assert result.created is True
    assert result.counts.total == 5
    assert result.counts.passed == 2
    assert result.counts.failed == 1
    assert result.counts.error == 1
    assert result.counts.skipped == 1
    assert result.counts.pass_rate == 50.0


def test_a_bare_testsuite_root_is_accepted(context: Any) -> None:
    """Half the world writes <testsuites>, the other half writes <testsuite>."""
    assert import_file(context, junit("bare-testsuite.xml")).counts.total == 3


def test_nested_suites_produce_a_tree(context: Any) -> None:
    result = import_file(context, junit("nested-suites.xml"))
    with context.uow_factory() as uow:
        detail = uow.runs.get_run_detail(result.run_id)
    assert detail is not None
    assert len(detail.suites) >= 2
    assert any(suite.parent_id is not None for suite in detail.suites)


def test_surefire_reruns_become_earlier_attempts_not_separate_tests(context: Any) -> None:
    """The single most important behaviour in the JUnit adapter.

    A rerun element is the *same* test trying again. Recorded as a separate test
    it would inflate the count and hide the flakiness; recorded as an overwrite
    it would lose the failure entirely.
    """
    result = import_file(context, junit("surefire-retries.xml"))
    with context.uow_factory() as uow:
        detail = uow.runs.get_run_detail(result.run_id)
    assert detail is not None

    retried = [test for test in detail.tests if len(test.attempts) > 1]
    assert retried, "expected at least one test with multiple attempts"

    flaky = [test for test in detail.tests if test.flaky]
    assert flaky, "a test that failed then passed must be flagged flaky"
    for test in flaky:
        statuses = [attempt.status for attempt in test.attempts]
        assert TestStatus.FAILED in statuses or TestStatus.ERROR in statuses
        assert test.status is TestStatus.PASSED
        assert [a.attempt_number for a in test.attempts] == sorted(
            a.attempt_number for a in test.attempts
        )
    assert result.counts.flaky >= 1
    assert result.counts.retried >= 1


def test_a_test_counts_once_however_many_times_it_was_retried(context: Any) -> None:
    result = import_file(context, junit("surefire-retries.xml"))
    with context.uow_factory() as uow:
        detail = uow.runs.get_run_detail(result.run_id)
    assert detail is not None
    assert result.counts.total == len(detail.tests)


def test_unmappable_statuses_become_unknown_and_raise_a_warning(context: Any) -> None:
    result = import_file(context, junit("weird-statuses.xml"))
    codes = {warning.code for warning in result.warnings}
    assert "unknown_status" in codes
    assert result.counts.unknown >= 1
    assert result.counts.passed < result.counts.total


def test_an_empty_suite_is_reported_not_silently_swallowed(context: Any) -> None:
    result = import_file(context, junit("empty-suite.xml"))
    assert result.counts.total == 0
    assert any(warning.code == "empty_suite" for warning in result.warnings)


def test_original_status_is_always_preserved(context: Any) -> None:
    """Normalization must never be lossy."""
    result = import_file(context, junit("weird-statuses.xml"))
    with context.uow_factory() as uow:
        detail = uow.runs.get_run_detail(result.run_id)
    assert detail is not None
    originals = {
        attempt.original_status
        for test in detail.tests
        for attempt in test.attempts
        if attempt.original_status
    }
    assert originals


# ------------------------------------------------------------- malformed


def test_malformed_xml_is_refused_with_a_useful_message(context: Any) -> None:
    with pytest.raises(AdapterError) as error:
        import_file(context, junit("malformed.xml"))
    assert "well-formed" in str(error.value).lower()


@pytest.mark.parametrize("name", ["billion-laughs.xml", "xxe.xml"])
def test_entity_attacks_are_refused_before_expansion(context: Any, name: str) -> None:
    """Refused, not expanded then rejected. The difference is the whole point."""
    with pytest.raises(AdapterError) as error:
        import_file(context, junit(name))
    message = str(error.value).lower()
    assert "dtd" in message or "entit" in message


def test_an_unknown_format_lists_what_is_supported(context: Any) -> None:
    request = ImportRequest(project_key="p", filename="notes.txt", content_type="text/plain")
    with pytest.raises(UnknownFormatError) as error:
        ImportService(context).import_stream(io.BytesIO(b"just some prose"), request)
    installed = error.value.detail["installed_adapters"]
    assert {row["name"] for row in installed} >= {"junit-xml", "testhub-json"}


def test_an_empty_upload_is_rejected(context: Any) -> None:
    with pytest.raises(AdapterError, match="empty"):
        ImportService(context).import_stream(
            io.BytesIO(b""), ImportRequest(project_key="p", filename="empty.xml")
        )


def test_an_oversized_upload_is_refused_while_reading(context: Any) -> None:
    context.settings.max_report_bytes = 1024
    payload = b"<testsuites>" + b"<!-- padding -->" * 500 + b"</testsuites>"
    with pytest.raises(PayloadTooLargeError):
        ImportService(context).import_stream(
            io.BytesIO(payload), ImportRequest(project_key="p", filename="big.xml")
        )


def test_an_explicit_format_overrides_detection(context: Any) -> None:
    with pytest.raises(AdapterError):
        ImportService(context).import_stream(
            io.BytesIO(b'{"kind": "test-hub-report", "run": {}}'),
            ImportRequest(project_key="p", filename="x.json", explicit_format="junit-xml"),
        )


def test_an_unknown_explicit_format_is_rejected(context: Any) -> None:
    with pytest.raises(UnknownFormatError):
        ImportService(context).import_stream(
            io.BytesIO(b"<testsuites/>"),
            ImportRequest(project_key="p", filename="x.xml", explicit_format="nunit"),
        )


# ------------------------------------------------------------- canonical


def test_canonical_json_report_imports(context: Any) -> None:
    result = import_file(context, canonical("report.json"))
    assert result.adapter == "testhub-json"
    assert result.detection_confidence == 1.0
    assert result.counts.total > 0


def test_canonical_event_stream_imports(context: Any) -> None:
    assert import_file(context, canonical("events.json")).counts.total > 0


def test_json_that_is_not_ours_is_refused_rather_than_guessed_at(context: Any) -> None:
    """We do not advertise support for arbitrary JSON, so we do not accept it."""
    with pytest.raises((AdapterError, UnknownFormatError)):
        import_file(context, canonical("not-ours.json"))


def test_a_future_schema_version_is_refused(context: Any) -> None:
    with pytest.raises(Exception) as error:
        import_file(context, canonical("bad-version.json"))
    assert "schema_version" in str(error.value)


def test_the_two_adapters_agree_on_equivalent_input(context: Any, tmp_path: Path) -> None:
    """A JUnit file and its canonical equivalent must produce the same numbers."""
    junit_result = import_file(context, junit("simple.xml"))

    equivalent = {
        "kind": "test-hub-report",
        "schema_version": 1,
        "run": {"project_key": "payments-api"},
        "tests": [
            {"name": "a", "suite_path": "s", "status": "passed", "duration_ms": 1},
            {"name": "b", "suite_path": "s", "status": "passed", "duration_ms": 1},
            {"name": "c", "suite_path": "s", "status": "failed", "duration_ms": 1},
            {"name": "d", "suite_path": "s", "status": "error", "duration_ms": 1},
            {"name": "e", "suite_path": "s", "status": "skipped"},
        ],
    }
    path = tmp_path / "equivalent.json"
    path.write_text(json.dumps(equivalent), encoding="utf-8")
    json_result = import_file(context, path)

    for field in ("total", "passed", "failed", "error", "skipped"):
        assert getattr(json_result.counts, field) == getattr(junit_result.counts, field), field
    assert json_result.counts.pass_rate == junit_result.counts.pass_rate


# ----------------------------------------------------------- idempotency


def test_importing_the_same_file_twice_creates_two_runs_by_default(context: Any) -> None:
    """Two genuine runs of the same suite are two runs. Only ids make them one."""
    first = import_file(context, junit("simple.xml"))
    second = import_file(context, junit("simple.xml"))
    assert first.run_id != second.run_id


def test_importing_into_the_same_run_id_is_idempotent(context: Any) -> None:
    run_id = uuid.uuid4()
    first = import_file(context, junit("simple.xml"), run_id=run_id)
    second = import_file(context, junit("simple.xml"), run_id=run_id)

    assert first.run_id == second.run_id == run_id
    assert second.events_applied == 0
    assert second.events_ignored > 0
    with context.uow_factory() as uow:
        run = uow.runs.get_run(run_id)
    assert run is not None
    assert run.counts.total == first.counts.total, "counts must not double"


def test_the_adapter_name_and_warnings_are_recorded_on_the_run(context: Any) -> None:
    result = import_file(context, junit("weird-statuses.xml"))
    with context.uow_factory() as uow:
        run = uow.runs.get_run(result.run_id)
    assert run is not None
    assert run.source_adapter == "junit-xml"
    assert run.warnings


def test_run_status_is_folded_not_taken_from_the_file(context: Any) -> None:
    result = import_file(context, junit("simple.xml"))
    with context.uow_factory() as uow:
        run = uow.runs.get_run(result.run_id)
    assert run is not None
    assert run.status is RunStatus.ERROR  # simple.xml contains one error


def test_context_metadata_from_the_caller_reaches_the_run(context: Any) -> None:
    from test_hub.domain.models import RunContext

    request = ImportRequest(
        project_key="payments-api",
        team="payments",
        component="checkout",
        context=RunContext(environment="staging", branch="main", framework="pytest"),
        filename="simple.xml",
    )
    with junit("simple.xml").open("rb") as handle:
        result = ImportService(context).import_stream(handle, request)
    with context.uow_factory() as uow:
        run = uow.runs.get_run(result.run_id)
    assert run is not None
    assert run.team == "payments"
    assert run.component == "checkout"
    assert run.context.environment == "staging"
    assert run.context.branch == "main"


# --------------------------------------------------------------- compression


def test_a_gzipped_report_is_imported_transparently(context: Any, tmp_path: Path) -> None:
    """CI systems gzip their artifacts; a `.xml.gz` should just work."""
    import gzip

    compressed = tmp_path / "results.xml.gz"
    compressed.write_bytes(gzip.compress(junit("simple.xml").read_bytes()))

    plain = import_file(context, junit("simple.xml"))
    gzipped = import_file(context, compressed)

    assert gzipped.adapter == "junit-xml"
    assert gzipped.counts.total == plain.counts.total
    assert gzipped.counts.pass_rate == plain.counts.pass_rate


def test_a_decompression_bomb_is_refused_during_expansion(context: Any) -> None:
    """A few KB on the wire that would become gigabytes in memory."""
    import gzip

    context.settings.max_decompressed_bytes = 1024
    bomb = gzip.compress(b"<testsuites>" + b"A" * (5 * 1024 * 1024) + b"</testsuites>")
    assert len(bomb) < 100_000, "the fixture should be small compressed"

    with pytest.raises(PayloadTooLargeError, match="decompression bomb"):
        ImportService(context).import_stream(
            io.BytesIO(bomb), ImportRequest(project_key="p", filename="bomb.xml.gz")
        )


def test_truncated_gzip_is_a_clear_error_not_a_crash(context: Any) -> None:
    import gzip

    truncated = gzip.compress(junit("simple.xml").read_bytes())[:40]
    with pytest.raises(AdapterError, match="gzip"):
        ImportService(context).import_stream(
            io.BytesIO(truncated), ImportRequest(project_key="p", filename="bad.gz")
        )


def test_uncompressed_input_is_untouched(context: Any) -> None:
    """The gzip check must not disturb the common path."""
    from test_hub.service.imports import decompress_bounded

    original = io.BytesIO(b"<testsuites/>")
    assert decompress_bounded(original, 1024) is original
