"""Read-only units of work, and why they exist.

SQLite serializes writers. Test Hub takes the write lock up front
(``BEGIN IMMEDIATE``) so two publishers queue instead of deadlocking on a lock
upgrade — but that means *any* transaction holds the write lock for its whole
life, including one that only ever reads. A retention plan walks every run in
the store to count what a prune would remove; holding the write lock for that
would stall every publisher until it finished.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from test_hub.service.imports import ImportRequest, ImportService
from test_hub.storage.base import RunFilter
from tests.paths import junit

pytestmark = pytest.mark.slow


def seed(context: Any) -> None:
    with junit("simple.xml").open("rb") as handle:
        ImportService(context).import_stream(
            handle, ImportRequest(project_key="p", filename="simple.xml")
        )


def write_from_another_thread(context: Any, label: str, timeout: float = 6.0) -> float | None:
    """Try to write while the caller holds a transaction. Returns seconds, or None."""
    outcome: list[float | None] = []

    def attempt() -> None:
        started = time.perf_counter()
        try:
            with context.uow_factory() as uow:
                uow.runs.ensure_project(context.workspace_id, key=f"probe-{label}", name="probe")
                uow.commit()
            outcome.append(time.perf_counter() - started)
        except Exception:
            outcome.append(None)

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=timeout)
    return outcome[0] if outcome else None


def test_a_read_only_scope_does_not_block_a_writer(context: Any) -> None:
    context.settings.sqlite_busy_timeout_ms = 1000
    seed(context)

    with context.uow_factory(read_only=True) as reader:
        reader.runs.list_runs(RunFilter(workspace_id=context.workspace_id))
        reader.runs.overview(context.workspace_id)
        elapsed = write_from_another_thread(context, "readonly")

    assert elapsed is not None, "a writer was blocked by a read-only transaction"
    assert elapsed < 1.0


def test_a_writing_scope_does_block_a_writer(context: Any) -> None:
    """The counterpart, so the test above is measuring something real."""
    context.settings.sqlite_busy_timeout_ms = 300
    seed(context)

    with context.uow_factory() as writer:
        writer.runs.list_runs(RunFilter(workspace_id=context.workspace_id))
        elapsed = write_from_another_thread(context, "writing")

    assert elapsed is None, "the write lock was not held — BEGIN IMMEDIATE may have regressed"


def test_a_read_only_scope_refuses_to_commit(context: Any) -> None:
    """Silently allowing it would take the write lock after all."""
    with context.uow_factory(read_only=True) as uow, pytest.raises(RuntimeError, match="read-only"):
        uow.commit()


def test_reads_still_return_the_same_data(context: Any) -> None:
    seed(context)
    with context.uow_factory() as writing:
        writing_runs = writing.runs.list_runs(RunFilter(workspace_id=context.workspace_id))
    with context.uow_factory(read_only=True) as reading:
        reading_runs = reading.runs.list_runs(RunFilter(workspace_id=context.workspace_id))
    assert [r.id for r in writing_runs] == [r.id for r in reading_runs]
    assert writing_runs[0].counts.total == reading_runs[0].counts.total


def test_the_longest_read_uses_a_read_only_scope(context: Any) -> None:
    """A retention plan counts what a prune would delete by walking the whole
    store. It is the longest read left in the product; assert the scope it opens
    rather than assuming it."""
    seed(context)
    from datetime import timedelta

    from test_hub.domain.models import utcnow
    from test_hub.service.retention import RetentionPolicy, RetentionService

    opened: list[bool] = []
    original = context.uow_factory

    def spy(*, read_only: bool = False) -> Any:
        opened.append(read_only)
        return original(read_only=read_only)

    object.__setattr__(context, "uow_factory", spy)
    try:
        RetentionService(context).plan(RetentionPolicy(cutoff=utcnow() - timedelta(days=1)))
    finally:
        object.__setattr__(context, "uow_factory", original)

    assert opened and all(opened), "the retention plan opened a writing transaction"
