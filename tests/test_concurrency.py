"""Two publishers at once must not corrupt the counts.

This is definition-of-done item 5, and it is the test most likely to catch a real
bug: SQLite serializes writers, the fold recomputes counts from rows, and any
gap between "insert the attempt" and "recount" is a place where two threads can
interleave and produce a total that never existed.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pytest

from test_hub.domain.events import EventType, RunQueuedData, RunStartedData, make_event
from test_hub.domain.identity import suite_id as derive_suite_id

# Aliased: pytest collects any module-level `test_*` name, including imports.
from test_hub.domain.identity import test_identity as compute_identity
from test_hub.domain.models import TestType, utcnow
from test_hub.domain.status import TestStatus

pytestmark = pytest.mark.slow


def result_event(
    workspace: uuid.UUID, run: uuid.UUID, publisher: int, index: int, sequence: int
) -> Any:
    from test_hub.domain.events import TestFinishedData

    suite_path = f"tests/publisher_{publisher}.py"
    name = f"test_{publisher}_{index}"
    return make_event(
        EventType.TEST_FINISHED,
        run_id=run,
        workspace_id=workspace,
        sequence=sequence,
        source=f"urn:test-hub:publisher-{publisher}",
        data=TestFinishedData(
            test_id=compute_identity(project="concurrent", suite_path=suite_path, test_name=name),
            suite_id=derive_suite_id(project="concurrent", suite_path=suite_path),
            name=name,
            suite_path=suite_path,
            status=TestStatus.PASSED if index % 4 else TestStatus.FAILED,
            duration_ms=index,
            finished_at=utcnow(),
        ),
    )


def open_run(context: Any, run_id: uuid.UUID) -> None:
    events = [
        make_event(
            EventType.RUN_QUEUED,
            run_id=run_id,
            workspace_id=context.workspace_id,
            sequence=1,
            source="urn:test-hub:setup",
            data=RunQueuedData(project_key="concurrent", test_type=TestType.INTEGRATION),
        ),
        make_event(
            EventType.RUN_STARTED,
            run_id=run_id,
            workspace_id=context.workspace_id,
            sequence=2,
            source="urn:test-hub:setup",
            data=RunStartedData(started_at=utcnow()),
        ),
    ]
    with context.uow_factory() as uow:
        uow.events.apply_events(events)
        uow.commit()


PUBLISHERS = 4
PER_PUBLISHER = 25


def test_concurrent_publishers_into_one_run_produce_exact_counts(context: Any) -> None:
    run_id = uuid.uuid4()
    open_run(context, run_id)

    def publish(publisher: int) -> int:
        applied = 0
        for index in range(PER_PUBLISHER):
            event = result_event(
                context.workspace_id, run_id, publisher, index, 100 + publisher * 1000 + index
            )
            with context.uow_factory() as uow:
                applied += uow.events.apply_events([event]).applied
                uow.commit()
        return applied

    with ThreadPoolExecutor(max_workers=PUBLISHERS) as pool:
        futures = [pool.submit(publish, publisher) for publisher in range(PUBLISHERS)]
        total_applied = sum(future.result() for future in as_completed(futures))

    expected = PUBLISHERS * PER_PUBLISHER
    assert total_applied == expected

    with context.uow_factory() as uow:
        run = uow.runs.get_run(run_id)
        detail = uow.runs.get_run_detail(run_id)
    assert run is not None and detail is not None
    assert run.counts.total == expected, "the fold lost or double-counted a result"
    assert len(detail.tests) == expected
    assert run.counts.passed + run.counts.failed == expected


def test_concurrent_publishers_into_separate_runs_do_not_interfere(context: Any) -> None:
    run_ids = [uuid.uuid4() for _ in range(PUBLISHERS)]
    for run_id in run_ids:
        open_run(context, run_id)

    def publish(publisher: int) -> None:
        run_id = run_ids[publisher]
        for index in range(PER_PUBLISHER):
            with context.uow_factory() as uow:
                uow.events.apply_events(
                    [result_event(context.workspace_id, run_id, publisher, index, 100 + index)]
                )
                uow.commit()

    with ThreadPoolExecutor(max_workers=PUBLISHERS) as pool:
        for future in as_completed([pool.submit(publish, p) for p in range(PUBLISHERS)]):
            future.result()

    with context.uow_factory() as uow:
        for run_id in run_ids:
            run = uow.runs.get_run(run_id)
            assert run is not None
            assert run.counts.total == PER_PUBLISHER


def test_the_same_batch_posted_concurrently_lands_exactly_once(context: Any) -> None:
    """The idempotency guarantee under a real race, not a sequential replay."""
    run_id = uuid.uuid4()
    open_run(context, run_id)
    batch = [
        result_event(context.workspace_id, run_id, 0, index, 100 + index) for index in range(20)
    ]

    def publish() -> tuple[int, int]:
        with context.uow_factory() as uow:
            result = uow.events.apply_events(batch)
            uow.commit()
        return result.applied, result.ignored

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = [future.result() for future in [pool.submit(publish) for _ in range(6)]]

    assert sum(applied for applied, _ in outcomes) == 20, "an event was applied twice"
    assert sum(ignored for _, ignored in outcomes) == 20 * 5

    with context.uow_factory() as uow:
        run = uow.runs.get_run(run_id)
    assert run is not None
    assert run.counts.total == 20


def test_retries_arriving_out_of_order_still_order_by_attempt_number(context: Any) -> None:
    """Attempt 2 can reach the server before attempt 1 on a lossy network."""
    from test_hub.domain.events import TestFinishedData

    run_id = uuid.uuid4()
    open_run(context, run_id)
    suite_path = "tests/test_ordering.py"

    def attempt(number: int, status: TestStatus, sequence: int) -> Any:
        return make_event(
            EventType.TEST_FINISHED,
            run_id=run_id,
            workspace_id=context.workspace_id,
            sequence=sequence,
            source="urn:test-hub:ordering",
            data=TestFinishedData(
                test_id=compute_identity(
                    project="concurrent", suite_path=suite_path, test_name="test_retry"
                ),
                suite_id=derive_suite_id(project="concurrent", suite_path=suite_path),
                name="test_retry",
                suite_path=suite_path,
                attempt_number=number,
                status=status,
                duration_ms=10,
                finished_at=utcnow(),
            ),
        )

    # Post attempt 2 first, with the lower sequence number on attempt 1.
    with context.uow_factory() as uow:
        uow.events.apply_events([attempt(2, TestStatus.PASSED, 20)])
        uow.commit()
    with context.uow_factory() as uow:
        uow.events.apply_events([attempt(1, TestStatus.FAILED, 10)])
        uow.commit()

    with context.uow_factory() as uow:
        detail = uow.runs.get_run_detail(run_id)
    assert detail is not None
    test = next(test for test in detail.tests if test.name == "test_retry")
    assert [a.attempt_number for a in test.attempts] == [1, 2]
    assert test.status is TestStatus.PASSED, "the effective status is the last attempt"
    assert test.flaky is True
