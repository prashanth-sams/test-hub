"""PostgreSQL-only behaviour: what replaced SQLite's single write lock.

Skipped unless ``TEST_HUB_TEST_DATABASE_URL`` points at a server. The rest of
the suite already runs against PostgreSQL in that mode; these tests cover what
only PostgreSQL has — writers that genuinely run in parallel, the per-run lock
that keeps the fold correct anyway, and the startup race between two containers.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from test_hub.config import Settings
from test_hub.service.runtime import create_context
from test_hub.storage import create_database
from test_hub.storage.base import RunFilter
from tests.conftest import EventFactory

pytestmark = [pytest.mark.postgres, pytest.mark.slow]

#: Every column migration 0003 widens.
COUNTERS = {
    ("runs", "duration_ms"),
    ("suites", "duration_ms"),
    ("test_cases", "duration_ms"),
    ("test_cases", "attempt_count"),
    ("attempts", "attempt_number"),
    ("attempts", "duration_ms"),
    ("events", "sequence"),
    ("artifacts", "size_bytes"),
}


def apply(context: Any, events: list[Any]) -> None:
    with context.uow_factory() as uow:
        uow.events.apply_events(events)
        uow.commit()


def timed_in_thread(work: Any, timeout: float = 10.0) -> tuple[threading.Thread, list[float]]:
    """Start ``work`` on a thread; the list gets its finish time when it is done."""
    finished: list[float] = []

    def target() -> None:
        work()
        finished.append(time.monotonic())

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, finished


def test_writers_to_different_runs_do_not_wait_for_each_other(context: Any) -> None:
    """The reason to pick PostgreSQL: on SQLite the second write would queue."""
    held = EventFactory(context.workspace_id)
    free = EventFactory(context.workspace_id)
    apply(context, [held.queued(), held.started()])
    apply(context, [free.queued(), free.started()])

    with context.uow_factory() as holder:
        holder.events.apply_events([held.test("test_held_open")])  # uncommitted
        started = time.monotonic()
        thread, finished = timed_in_thread(lambda: apply(context, [free.test("test_free")]))
        thread.join(timeout=10)
        assert finished, "a writer to another run waited for an open transaction"
        assert finished[0] - started < 3.0
        holder.commit()


def test_writers_to_the_same_run_take_turns(context: Any) -> None:
    """…but two batches for one run must not interleave, or the recount lies."""
    run = EventFactory(context.workspace_id)
    apply(context, [run.queued(), run.started()])

    with context.uow_factory() as holder:
        holder.events.apply_events([run.test("test_first")])
        thread, finished = timed_in_thread(lambda: apply(context, [run.test("test_second")]))
        time.sleep(0.5)
        assert not finished, "a second writer to the same run did not wait its turn"
        committed = time.monotonic()
        holder.commit()
    thread.join(timeout=10)

    assert finished and finished[0] >= committed
    with context.uow_factory(read_only=True) as uow:
        summary = uow.runs.list_runs(RunFilter(workspace_id=context.workspace_id))[0]
    assert summary.counts.total == 2


def test_two_instances_starting_on_an_empty_database_both_come_up(
    postgres_url: str, tmp_path: Path
) -> None:
    """Two containers, one fresh database: one migrates, the other waits and finds head."""
    barrier = threading.Barrier(2)

    def start(index: int) -> str | None:
        settings = Settings(data_dir=tmp_path / f"instance-{index}", database_url=postgres_url)
        barrier.wait(timeout=10)
        context = create_context(settings, configure_logs=False)
        try:
            return context.database.current_revision()
        finally:
            context.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = list(pool.map(start, range(2)))
    assert revisions == ["0003_postgresql_bigint", "0003_postgresql_bigint"]


def test_counters_are_bigint(context: Any) -> None:
    with context.database.engine.connect() as connection:
        inspector = sa.inspect(connection)
        widths = {
            (table, column["name"]): type(column["type"]).__name__
            for table in {table for table, _ in COUNTERS}
            for column in inspector.get_columns(table)
        }
    assert {key: widths[key] for key in COUNTERS} == dict.fromkeys(COUNTERS, "BIGINT")


def test_reset_drops_only_test_hubs_own_tables(postgres_url: str, tmp_path: Path) -> None:
    """`demo --reset` on a server must not take someone else's table with it."""
    database = create_database(Settings(data_dir=tmp_path, database_url=postgres_url))
    try:
        database.upgrade()
        with database.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE someone_elses (id integer)")

        database.drop_schema()
        with database.engine.connect() as connection:
            assert set(sa.inspect(connection).get_table_names()) == {"someone_elses"}

        database.upgrade()
        assert database.current_revision() == database.head_revision()
    finally:
        database.dispose()


def test_db_check_reports_postgresql(postgres_url: str, tmp_path: Path) -> None:
    from test_hub.cli import _register
    from test_hub.cli import app as cli_app

    _register()
    env = {key: value for key, value in os.environ.items() if not key.startswith("TEST_HUB_")}
    env |= {
        "TEST_HUB_DATA_DIR": str(tmp_path / "data"),
        "TEST_HUB_DATABASE_URL": postgres_url,
        "NO_COLOR": "1",
    }
    runner = CliRunner()

    assert runner.invoke(cli_app, ["db", "upgrade"], env=env).exit_code == 0
    result = runner.invoke(cli_app, ["db", "check"], env=env)
    assert result.exit_code == 0, result.output
    assert "PostgreSQL" in result.output
    assert "postgresql+psycopg://" in result.output
    assert "ready" in result.output
