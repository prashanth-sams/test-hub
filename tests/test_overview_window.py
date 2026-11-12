"""The overview's window: an hour, a day, a quarter, or a range somebody picked.

The dashboard offers 1h, 6h and 24h next to 7d … 90d, and a custom range, so
``GET /overview`` takes a length in hours as well as days and a fixed
``since``/``until``. These pin what a window counts (the runs that started
inside it and nothing else), how its trend is bucketed (by the clock when it is
two days or shorter, by the date otherwise), what does *not* move with it (the
last-24-hours count is about now, whatever the window), and what is refused.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from test_hub.domain.models import utcnow
from test_hub.domain.status import TestStatus
from test_hub.service.coalesce import Coalescer
from test_hub.storage.repositories import SqlRunRepository
from tests.conftest import EventFactory


def run_of(
    context: Any,
    project_key: str,
    *,
    hours_ago: float,
    status: TestStatus = TestStatus.PASSED,
) -> EventFactory:
    """One finished run of ``project_key``, one test, started ``hours_ago``."""
    run = EventFactory(context.workspace_id, project_key=project_key)
    run.start = utcnow() - timedelta(hours=hours_ago)
    with context.uow_factory() as uow:
        uow.events.apply_events(
            [
                run.queued(),
                run.started(),
                run.suite(),
                run.test("test_one", status),
                run.finished(finished_at=run.start + timedelta(minutes=1)),
            ]
        )
        uow.commit()
    return run


def overview(client: Any, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/overview", params=params)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def project(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(row for row in body["projects"] if row["project_key"] == key)


# ------------------------------------------------------------------ lengths


@pytest.mark.parametrize(
    ("hours", "bucket_seconds"),
    [(1, 300), (6, 900), (24, 3600)],
)
def test_an_hour_window_counts_its_hours_and_buckets_by_the_clock(
    client: Any, context: Any, hours: int, bucket_seconds: int
) -> None:
    run_of(context, "inside", hours_ago=hours * 0.5)
    run_of(context, "outside", hours_ago=hours * 1.5)

    body = overview(client, window_hours=hours)

    assert body["total_runs"] == 1
    assert body["window_days"] == 1, "a window under a day is one day, rounded up"
    start, end = instant(body["window_start"]), instant(body["window_end"])
    assert end - start == timedelta(hours=hours)
    assert body["trend_bucket_seconds"] == bucket_seconds
    # Clock buckets start on the clock — 14:05, 14:10 — not wherever now fell.
    points = body["trend"]
    assert len(points) <= 25
    first = instant(points[0]["bucket"])
    assert first.tzinfo is not None
    assert first.timestamp() % bucket_seconds == 0
    assert first <= start < first + timedelta(seconds=bucket_seconds)
    assert sum(point["runs"] for point in points) == 1
    assert project(body, "inside")["last_run"] is not None
    assert project(body, "outside")["last_run"] is None


def test_the_default_window_is_still_thirty_daily_points(client: Any, context: Any) -> None:
    run_of(context, "steady", hours_ago=2)
    body = overview(client)
    assert body["window_days"] == 30
    assert body["trend_bucket_seconds"] == 86400
    assert len(body["trend"]) == 31
    assert len(body["trend"][0]["bucket"]) == len("2026-11-12"), "daily buckets are dates"


def test_a_window_over_two_days_is_bucketed_by_the_date(client: Any) -> None:
    body = overview(client, window_hours=72)
    assert body["window_days"] == 3
    assert body["trend_bucket_seconds"] == 86400


def test_a_year_widens_its_buckets_rather_than_multiplying_them(client: Any) -> None:
    body = overview(client, window_days=365)
    assert body["trend_bucket_seconds"] == 7 * 86400
    assert len(body["trend"]) <= 61


# ------------------------------------------------------------------- ranges


def test_a_fixed_range_counts_only_what_started_inside_it(client: Any, context: Any) -> None:
    run_of(context, "before", hours_ago=30)
    inside = run_of(context, "during", hours_ago=10, status=TestStatus.FAILED)
    run_of(context, "after", hours_ago=1)
    since = (utcnow() - timedelta(hours=20)).isoformat()
    until = (utcnow() - timedelta(hours=5)).isoformat()

    body = overview(client, since=since, until=until)

    assert body["total_runs"] == 1
    assert body["counts"]["failed"] == 1
    assert instant(body["window_start"]) == instant(since)
    assert instant(body["window_end"]) == instant(until)
    assert body["trend_bucket_seconds"] == 3600, "fifteen hours, bucketed by the hour"
    assert project(body, "during")["last_run_id"] == str(inside.run_id)
    assert project(body, "after")["last_run"] is None, "after the range is outside it"
    failing = {test["project_key"] for test in body["top_failing_tests"]}
    assert failing == {"during"}


def test_since_alone_runs_to_now(client: Any, context: Any) -> None:
    run_of(context, "today", hours_ago=3)
    body = overview(client, since=(utcnow() - timedelta(hours=5)).isoformat())
    assert body["total_runs"] == 1
    assert instant(body["window_end"]) == instant(body["generated_at"])


def test_until_alone_is_the_length_before_it(client: Any, context: Any) -> None:
    run_of(context, "early", hours_ago=60)
    run_of(context, "late", hours_ago=2)
    until = utcnow() - timedelta(days=1)
    body = overview(client, until=until.isoformat(), window_days=7)
    assert body["total_runs"] == 1
    assert instant(body["window_end"]) - instant(body["window_start"]) == timedelta(days=7)


def test_the_last_24_hours_are_counted_whatever_the_window(client: Any, context: Any) -> None:
    """`runs_24h` is about now. Folded from the window's runs instead, an hour's
    window would undercount it and last week's would make it zero."""
    run_of(context, "busy", hours_ago=3)
    run_of(context, "busy", hours_ago=20)

    assert project(overview(client, window_hours=1), "busy")["runs_24h"] == 2
    last_week = overview(
        client,
        since=(utcnow() - timedelta(days=9)).isoformat(),
        until=(utcnow() - timedelta(days=2)).isoformat(),
    )
    assert project(last_week, "busy")["runs_24h"] == 2
    assert overview(client, window_days=30)["projects"][0]["runs_24h"] == 2


def test_a_range_names_its_own_coalescing_key(client: Any, context: Any) -> None:
    """Two windows asked for at the same moment are two different questions."""
    run_of(context, "recent", hours_ago=0.5)
    run_of(context, "older", hours_ago=12)
    assert overview(client, window_hours=1)["total_runs"] == 1
    assert overview(client, window_hours=24)["total_runs"] == 2


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "params",
    [
        {"window_days": 7, "window_hours": 6},
        {"window_days": 7, "since": "2026-11-01T00:00:00Z"},
        {"window_hours": 6, "since": "2026-11-01T00:00:00Z"},
        {"since": "2026-11-05T00:00:00Z", "until": "2026-11-01T00:00:00Z"},
        {"since": "2026-11-05T00:00:00Z", "until": "2026-11-05T00:00:00Z"},
        {"since": "2020-01-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
        {"since": "2026-11-01T00:00:00"},
        {"window_hours": 0},
        {"window_hours": 10000},
    ],
)
def test_a_window_that_cannot_be_one_is_a_422(client: Any, params: dict[str, Any]) -> None:
    response = client.get("/api/v1/overview", params=params)
    assert response.status_code == 422, response.text


def test_a_window_starting_in_the_future_is_a_422(client: Any) -> None:
    future = (utcnow() + timedelta(hours=1)).isoformat()
    response = client.get("/api/v1/overview", params={"since": future})
    assert response.status_code == 422
    assert "future" in response.text


def test_an_offset_other_than_utc_is_the_same_instant(client: Any, context: Any) -> None:
    run_of(context, "ist", hours_ago=2)
    india = timezone(timedelta(hours=5, minutes=30))
    since = (utcnow() - timedelta(hours=4)).astimezone(india)
    assert "+05:30" in since.isoformat()
    body = overview(client, since=since.isoformat())
    assert instant(body["window_start"]) == since
    assert body["total_runs"] == 1


# ------------------------------------------------------------------- units


def test_clock_steps_divide_the_hour_and_the_day() -> None:
    for step in SqlRunRepository.CLOCK_STEPS:
        assert 3600 % step == 0 or step % 3600 == 0
        assert 86400 % step == 0


@pytest.mark.parametrize(
    ("span", "seconds"),
    [
        (timedelta(minutes=5), 60),
        (timedelta(minutes=30), 120),
        (timedelta(hours=1), 300),
        (timedelta(hours=6), 900),
        (timedelta(hours=24), 3600),
        (timedelta(hours=48), 7200),
        (timedelta(hours=49), 86400),
        (timedelta(days=90), 2 * 86400),
    ],
)
def test_the_trend_step_for_a_span(span: timedelta, seconds: int) -> None:
    assert SqlRunRepository.trend_step(span).total_seconds() == seconds


def test_the_shared_cache_forgets_answers_too_old_to_serve() -> None:
    """Every custom range is a new key. Results nobody can be handed again are
    dropped, or the cache would keep one overview per range ever asked for."""
    shared: Coalescer[int] = Coalescer(ttl=0.0)
    for minute in range(50):
        shared.get(("range", minute), need=1, current=1, compute=lambda: 1, reuse_finished=True)
    assert len(shared._latest) <= 1  # the one finished just now, at most
