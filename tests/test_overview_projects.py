"""Each project's latest run on the overview — what the status-by-project row draws.

``GET /overview`` answers for every project at once, and that is the point of
``ProjectHealth.last_run``: a dashboard that shows every project's current status
must not need a request per project to do it. These pin what the field is (the
newest run inside the window, finished or still going, wherever it was run
from), what it is not (anything outside the window), and what it costs (no SQL
statement per project).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from test_hub.domain.models import TriggerType, utcnow
from test_hub.domain.status import TestStatus
from tests.conftest import EventFactory


def apply(context: Any, events: list[Any]) -> None:
    with context.uow_factory() as uow:
        uow.events.apply_events(events)
        uow.commit()


def run_of(
    context: Any,
    project_key: str,
    *,
    hours_ago: float,
    statuses: tuple[TestStatus, ...] = (TestStatus.PASSED,),
    trigger: TriggerType = TriggerType.PUSH,
    finish: bool = True,
) -> EventFactory:
    """One run of ``project_key`` that started ``hours_ago``, one test per status."""
    run = EventFactory(context.workspace_id, project_key=project_key)
    run.start = utcnow() - timedelta(hours=hours_ago)
    events = [run.queued(trigger=trigger), run.started(), run.suite()]
    events += [run.test(f"test_{index}", status) for index, status in enumerate(statuses)]
    if finish:
        events.append(run.finished(finished_at=run.start + timedelta(minutes=2)))
    apply(context, events)
    return run


def project(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(row for row in body["projects"] if row["project_key"] == key)


def test_the_latest_run_is_the_newest_one_in_the_window(client: Any, context: Any) -> None:
    run_of(context, "alpha", hours_ago=48, statuses=(TestStatus.PASSED, TestStatus.FAILED))
    newest = run_of(context, "alpha", hours_ago=1, statuses=(TestStatus.PASSED,) * 3)

    row = project(client.get("/api/v1/overview").json(), "alpha")

    latest = row["last_run"]
    assert latest["id"] == str(newest.run_id) == row["last_run_id"]
    assert latest["status"] == row["last_status"] == "passed"
    assert latest["is_live"] is False
    assert latest["counts"]["total"] == 3
    assert latest["counts"]["passed"] == 3
    assert latest["counts"]["pass_rate"] == 100.0
    # The window's pass rate still counts both runs: the latest run is a second
    # answer beside it, not a replacement for it.
    assert row["pass_rate"] == 80.0


def test_a_run_still_in_flight_is_the_projects_current_status(client: Any, context: Any) -> None:
    run_of(context, "beta", hours_ago=24, statuses=(TestStatus.PASSED,) * 4)
    live = run_of(
        context,
        "beta",
        hours_ago=0.1,
        statuses=(TestStatus.PASSED, TestStatus.FAILED),
        trigger=TriggerType.LOCAL,
        finish=False,
    )

    latest = project(client.get("/api/v1/overview").json(), "beta")["last_run"]
    assert latest["id"] == str(live.run_id)
    assert latest["is_live"] is True
    assert latest["status"] == "running"
    # Not every run comes from CI, and the one that ran on a laptop is still
    # this project's status — the trigger says where it came from.
    assert latest["trigger"] == "local"
    assert (latest["counts"]["total"], latest["counts"]["failed"]) == (2, 1)

    # A result arriving moves the number on the very next read — which is all a
    # dashboard refetching on a live event needs from this endpoint.
    apply(context, [live.test("test_late", TestStatus.ERROR)])
    latest = project(client.get("/api/v1/overview").json(), "beta")["last_run"]
    assert (latest["counts"]["total"], latest["counts"]["error"]) == (3, 1)


def test_a_project_that_has_not_run_inside_the_window_has_no_latest_run(
    client: Any, context: Any
) -> None:
    run_of(context, "gamma", hours_ago=40 * 24)

    row = project(client.get("/api/v1/overview", params={"window_days": 30}).json(), "gamma")
    assert row["last_run"] is None
    assert row["last_run_id"] is None

    wider = project(client.get("/api/v1/overview", params={"window_days": 60}).json(), "gamma")
    assert wider["last_run"]["id"] == wider["last_run_id"]


def test_the_latest_runs_cost_no_statement_per_project(context: Any) -> None:
    """Twenty-odd projects is past what the overview's own list of recent runs
    holds, so some latest runs have to be loaded — in one query, not one each.
    Counted, not timed, so the bound is deterministic."""

    def statements_for_overview() -> int:
        statements: list[str] = []

        def count(*args: Any, **kwargs: Any) -> None:
            statements.append(args[2])

        sa.event.listen(context.database.engine, "before_cursor_execute", count)
        try:
            with context.uow_factory(read_only=True) as uow:
                snapshot = uow.runs.overview(context.workspace_id)
        finally:
            sa.event.remove(context.database.engine, "before_cursor_execute", count)
        assert all(row.last_run is not None for row in snapshot.projects)
        return len(statements)

    for index in range(2):
        run_of(context, f"project-{index:02d}", hours_ago=index + 1)
    few = statements_for_overview()

    for index in range(2, 30):
        run_of(context, f"project-{index:02d}", hours_ago=index + 1)
    many = statements_for_overview()

    assert many <= few + 1, f"{few} statements for 2 projects, {many} for 30"
