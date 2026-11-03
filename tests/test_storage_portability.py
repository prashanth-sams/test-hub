"""Storage behaviour that has to mean the same thing on SQLite and PostgreSQL.

Every test here runs on SQLite by default and on PostgreSQL when
``TEST_HUB_TEST_DATABASE_URL`` is set (see ``conftest.py``). Each one pins a
place where the two backends disagree out of the box — integer width, NUL bytes,
LIKE's case rules, where NULL sorts — or a race SQLite's write lock hides and
PostgreSQL does not.
"""

from __future__ import annotations

import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest

from test_hub.domain.events import (
    ArtifactPublishedData,
    EventType,
    RunFinishedData,
    make_event,
)
from test_hub.domain.models import FailureDetail, utcnow
from test_hub.domain.status import TestStatus
from test_hub.service.imports import ImportRequest, ImportService
from test_hub.service.retention import RetentionPolicy, RetentionService
from test_hub.storage.base import RunFilter
from test_hub.storage.locks import WriteLocks
from tests.conftest import EventFactory
from tests.paths import junit


def apply(context: Any, events: list[Any], **kwargs: Any) -> Any:
    with context.uow_factory() as uow:
        result = uow.events.apply_events(events, **kwargs)
        uow.commit()
    return result


def list_runs(context: Any, **facets: Any) -> list[Any]:
    with context.uow_factory(read_only=True) as uow:
        return uow.runs.list_runs(RunFilter(workspace_id=context.workspace_id, **facets))


# --------------------------------------------------------------- integers


def test_counters_hold_64_bit_values(context: Any, events: EventFactory) -> None:
    """A publisher numbering events by epoch milliseconds must not fail the import.

    PostgreSQL's INTEGER is 32-bit; SQLite's is 64. Both values below are far
    past 2³¹ and far inside 2⁶³.
    """
    big = 2**40
    epoch_ms = 1_762_000_000_000
    artifact_id = uuid.uuid4()
    batch = [
        events.queued(),
        events.started(),
        events.suite(),
        events.test("test_runs_for_a_long_time", duration_ms=big),
        make_event(
            EventType.ARTIFACT_PUBLISHED,
            run_id=events.run_id,
            workspace_id=events.workspace_id,
            sequence=epoch_ms - 1,
            data=ArtifactPublishedData(
                artifact_id=artifact_id, name="core.dump", size_bytes=big, sha256="a" * 64
            ),
        ),
        make_event(
            EventType.RUN_FINISHED,
            run_id=events.run_id,
            workspace_id=events.workspace_id,
            sequence=epoch_ms,
            data=RunFinishedData(finished_at=utcnow()),
        ),
    ]
    apply(context, batch)

    with context.uow_factory(read_only=True) as uow:
        detail = uow.runs.get_run_detail(events.run_id)
        replayed = uow.events.list_events(events.run_id)
        artifact = uow.artifacts.get(artifact_id)
    assert detail is not None and artifact is not None
    assert detail.tests[0].attempts[0].duration_ms == big
    assert artifact.size_bytes == big
    assert max(event.sequence for event in replayed) == epoch_ms


# ---------------------------------------------------------------- text


def test_nul_bytes_in_captured_output_do_not_fail_the_import(
    context: Any, events: EventFactory
) -> None:
    """PostgreSQL refuses ``\\x00`` in text columns; a test that printed binary
    must still be recorded. The NULs are dropped, everything else is kept."""
    batch = [
        events.queued(),
        events.started(),
        events.suite(),
        events.test(
            "test_prints_binary",
            TestStatus.FAILED,
            stdout="before\x00after\x1b[31m red",
            stderr="\x00\x00",
            logs="a\x00b",
            failure=FailureDetail(type="ValueError", message="bad\x00byte"),
        ),
        events.finished(),
    ]
    apply(context, batch)

    with context.uow_factory(read_only=True) as uow:
        detail = uow.runs.get_run_detail(events.run_id)
    assert detail is not None
    attempt = detail.tests[0].attempts[0]
    assert attempt.stdout == "beforeafter\x1b[31m red", "only the NULs may go"
    assert "\x00" not in (attempt.stderr or "")
    assert attempt.logs == "ab"
    assert attempt.failure is not None and attempt.failure.message == "badbyte"


# ------------------------------------------------------------- searching


def test_search_ignores_case_on_every_backend(context: Any) -> None:
    """SQLite's LIKE ignores ASCII case and PostgreSQL's does not."""
    run = EventFactory(context.workspace_id, project_key="payments-api")
    apply(context, [run.queued(name="Nightly Checkout"), run.started(), run.finished()])

    for term in ("checkout", "CHECKOUT", "nightly", "PAYMENTS"):
        found = list_runs(context, search=term)
        assert [summary.id for summary in found] == [run.run_id], term


def test_tag_filters_match_whole_tags_ignoring_case(context: Any) -> None:
    """``_`` is a legal tag character and a LIKE wildcard; it must match itself."""
    underscore = EventFactory(context.workspace_id, project_key="tags")
    hyphen = EventFactory(context.workspace_id, project_key="tags")
    apply(context, [underscore.queued(tags=["nightly_build"]), underscore.started()])
    apply(context, [hyphen.queued(tags=["nightly-build"]), hyphen.started()])

    assert [run.id for run in list_runs(context, tags=["nightly_build"])] == [underscore.run_id]
    assert [run.id for run in list_runs(context, tags=["NIGHTLY-BUILD"])] == [hyphen.run_id]


def test_runs_that_never_started_sort_after_the_ones_that_did(context: Any) -> None:
    """Newest first, nulls last. PostgreSQL puts NULL first in a DESC sort."""
    started = EventFactory(context.workspace_id)
    apply(context, [started.queued(), started.started()])
    # Created later, so it would lead if the never-started run sorted first.
    queued = EventFactory(context.workspace_id)
    apply(context, [queued.queued()])

    assert [run.id for run in list_runs(context)] == [started.run_id, queued.run_id]


# ------------------------------------------------------------ the fold


def test_a_new_run_folds_a_whole_life_in_one_batch(context: Any, events: EventFactory) -> None:
    """Start, fail, retry and pass — one batch, for a run that did not exist.

    Everything this batch creates is found again through the batch's own state,
    not through a query (the run is brand new, so there is nothing older to
    find). This pins that the shortcut still sees every row it made.
    """
    from test_hub.domain.events import TestStartedData
    from test_hub.domain.identity import suite_id as derive_suite_id
    from test_hub.domain.identity import test_identity as derive_test_id

    suite_path, name = "tests/test_core.py", "test_flaky"
    test_id = derive_test_id(project=events.project_key, suite_path=suite_path, test_name=name)
    started = make_event(
        EventType.TEST_STARTED,
        run_id=events.run_id,
        workspace_id=events.workspace_id,
        sequence=100,
        data=TestStartedData(
            test_id=test_id,
            suite_id=derive_suite_id(project=events.project_key, suite_path=suite_path),
            name=name,
            suite_path=suite_path,
            started_at=events.start,
        ),
    )
    batch = [
        events.queued(),
        events.started(),
        events.suite(),
        started,
        events.test(name, TestStatus.FAILED, attempt=1),
        events.test(name, TestStatus.PASSED, attempt=2),
        events.test("test_steady"),
        events.finished(),
    ]
    apply(context, sorted(batch, key=lambda event: event.sequence))

    with context.uow_factory(read_only=True) as uow:
        detail = uow.runs.get_run_detail(events.run_id)
    assert detail is not None
    flaky = next(test for test in detail.tests if test.name == name)
    assert [attempt.attempt_number for attempt in flaky.attempts] == [1, 2]
    assert flaky.flaky is True
    assert detail.run.counts.total == 2
    assert detail.run.counts.flaky == 1


def test_the_same_artifact_announced_twice_in_one_batch_is_one_row(
    context: Any, events: EventFactory
) -> None:
    artifact_id = uuid.uuid4()

    def announce(sequence: int, name: str) -> Any:
        return make_event(
            EventType.ARTIFACT_PUBLISHED,
            run_id=events.run_id,
            workspace_id=events.workspace_id,
            sequence=sequence,
            data=ArtifactPublishedData(
                artifact_id=artifact_id, name=name, size_bytes=10, sha256="b" * 64
            ),
        )

    apply(context, [events.queued(), announce(50, "first.png"), announce(51, "renamed.png")])

    with context.uow_factory(read_only=True) as uow:
        artifacts = uow.artifacts.list_for_run(events.run_id)
    assert [(artifact.id, artifact.name) for artifact in artifacts] == [
        (artifact_id, "renamed.png")
    ]


def test_an_import_does_not_cost_a_statement_per_test(context: Any) -> None:
    """A 50-test import once issued ~270 SQL statements — five per test, each
    autoflushing the last test's rows one INSERT at a time. A round trip each is
    what made PostgreSQL slower than SQLite. Counted, not timed, so this is
    deterministic; the bound leaves room without letting the old shape back."""
    import sqlalchemy as sa

    cases = "".join(
        f'<testcase classname="pkg.Suite{i % 5}" name="test_{i}" time="0.01"/>' for i in range(50)
    )
    report = f'<?xml version="1.0"?><testsuite name="s" tests="50">{cases}</testsuite>'.encode()
    statements: list[str] = []

    def count(*args: Any, **kwargs: Any) -> None:
        statements.append(args[2])

    sa.event.listen(context.database.engine, "before_cursor_execute", count)
    try:
        result = ImportService(context).import_stream(
            io.BytesIO(report), ImportRequest(project_key="counted", filename="r.xml")
        )
    finally:
        sa.event.remove(context.database.engine, "before_cursor_execute", count)

    assert result.counts.total == 50
    assert len(statements) <= 40, f"{len(statements)} statements for one 50-test import"


def test_a_batch_into_an_existing_run_batches_its_inserts(
    context: Any, events: EventFactory
) -> None:
    """A run that already exists still has to be searched, one lookup per test.
    What must not come back is each lookup autoflushing the previous test's rows
    as single INSERTs: 50 tests cost 258 statements that way and 111 without."""
    import sqlalchemy as sa

    apply(context, [events.queued(), events.started(), events.suite()])
    batch = [events.test(f"test_{i}") for i in range(50)]
    statements: list[str] = []

    def count(*args: Any, **kwargs: Any) -> None:
        statements.append(args[2])

    sa.event.listen(context.database.engine, "before_cursor_execute", count)
    try:
        apply(context, batch)
    finally:
        sa.event.remove(context.database.engine, "before_cursor_execute", count)

    assert len(list_runs(context)) == 1
    assert len(statements) <= 160, f"{len(statements)} statements for 50 tests"


# ------------------------------------------------------------ concurrency


def test_many_pipelines_of_a_new_project_can_report_at_once(context: Any) -> None:
    """Every first batch tries to create the project. Exactly one row may result,
    and no publisher may fail because another one won."""
    publishers = 8
    barrier = threading.Barrier(publishers)

    def publish(index: int) -> None:
        run = EventFactory(context.workspace_id, project_key="brand-new")
        batch = [run.queued(), run.started(), run.suite(), run.test(f"test_{index}")]
        barrier.wait(timeout=10)
        apply(context, batch)

    with ThreadPoolExecutor(max_workers=publishers) as pool:
        list(pool.map(publish, range(publishers)))

    with context.uow_factory(read_only=True) as uow:
        projects = [p.key for p in uow.runs.list_projects(context.workspace_id)]
    assert projects.count("brand-new") == 1
    assert len(list_runs(context, project_keys=["brand-new"])) == publishers


def test_a_retried_import_racing_the_original_lands_once(context: Any) -> None:
    """A CI job that times out and retries sends the same idempotency key with a
    fresh run id while the first request may still be running. One import may
    land; every other copy must be answered from it, not fail and not repeat."""
    report = junit("simple.xml").read_bytes()
    copies = 5
    barrier = threading.Barrier(copies)

    def send(_: int) -> Any:
        barrier.wait(timeout=10)
        return ImportService(context).import_stream(
            io.BytesIO(report),
            ImportRequest(project_key="retried", filename="simple.xml", idempotency_key="job-42"),
        )

    with ThreadPoolExecutor(max_workers=copies) as pool:
        results = list(pool.map(send, range(copies)))

    applied = sorted(result.events_applied for result in results)
    assert applied[:-1] == [0] * (copies - 1), "an import with the same key landed twice"
    assert applied[-1] > 0
    assert len(list_runs(context, project_keys=["retried"])) == 1


def test_write_locks_are_only_taken_where_they_are_needed(context: Any) -> None:
    """SQLite's BEGIN IMMEDIATE already serializes writers; PostgreSQL needs asking."""
    with context.uow_factory() as uow:
        assert WriteLocks(uow.session).enabled is (context.database.backend == "postgresql")


# --------------------------------------------------------------- retention


def test_prune_can_vacuum_on_every_backend(context: Any, events: EventFactory) -> None:
    """SQLite VACUUMs the file; PostgreSQL VACUUM (ANALYZE)s Test Hub's tables.
    Both refuse to run inside a transaction, which is the part worth testing."""
    apply(context, events.simple_run())
    policy = RetentionPolicy(cutoff=utcnow() + timedelta(days=1), keep_min=0)

    result = RetentionService(context).prune(policy, vacuum=True)

    assert result.runs == 1
    assert result.vacuumed is True
    assert list_runs(context) == []


@pytest.mark.parametrize("rerun", [False, True])
def test_migrating_twice_is_harmless(context: Any, rerun: bool) -> None:
    """Every entry point migrates on start; on PostgreSQL under an advisory lock."""
    head = context.database.head_revision()
    if rerun:
        context.database.upgrade()
    assert context.database.current_revision() == head == "0003_postgresql_bigint"
