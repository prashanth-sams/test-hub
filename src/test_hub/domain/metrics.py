"""How every number on the dashboard is calculated.

This module is the single implementation of the definitions written down in
``docs/metrics.md``. Nothing else in the codebase is allowed to compute a pass
rate, a flake rate or a percentile — if a view needs one, it calls in here, so
the number in the snapshot and the number in the live API can never disagree.

Definitions, in short:

pass rate
    ``(passed + xfailed) / executed`` where ``executed`` excludes skipped and
    cancelled tests, and each test is counted once through its *effective*
    attempt (its last). ``None`` when nothing executed.

failure rate
    ``(failed + error + xpassed) / executed``.

duration
    A run's duration is wall-clock (``finished_at - started_at``) when both
    timestamps are known, because parallel workers make the sum of test
    durations meaningless. It falls back to the sum of attempt durations when
    they are not. A test's duration is the sum over its attempts: retries are
    time the run actually spent.

flaky (within a run)
    The test both failed and succeeded across its attempts in that one run.

flaky (across runs)
    Within the trailing window, the test produced *both* a successful and a
    failed effective outcome without its identity changing — plus any run in
    which it was flaky within the run. Expressed as ``flake_rate``: the
    percentage of runs in the window in which the test was flaky within that run
    or disagreed with the window's majority outcome.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from test_hub.domain.status import (
    TestStatus,
    is_failureish,
    is_successful,
)

__all__ = [
    "aggregate_counts",
    "effective_status",
    "flake_rate",
    "is_flaky_across_runs",
    "is_flaky_within_run",
    "percentile",
    "run_duration_ms",
]


def effective_status(attempt_statuses: Sequence[TestStatus]) -> TestStatus:
    """The status a test contributes to the counts: its last attempt's.

    Last, not best and not worst. A test that failed and then passed on retry is
    green *and* flaky; a test that passed and then failed on a rerun is red. The
    retry history is never thrown away, so both facts stay available.
    """
    if not attempt_statuses:
        return TestStatus.UNKNOWN
    return attempt_statuses[-1]


def is_flaky_within_run(attempt_statuses: Sequence[TestStatus]) -> bool:
    """True when one test both succeeded and failed inside a single run.

    Requires at least two attempts by definition; a single attempt cannot
    disagree with itself.
    """
    if len(attempt_statuses) < 2:
        return False
    saw_success = any(is_successful(status) for status in attempt_statuses)
    saw_failure = any(is_failureish(status) for status in attempt_statuses)
    return saw_success and saw_failure


def is_flaky_across_runs(effective_statuses: Iterable[TestStatus]) -> bool:
    """True when one test identity both passed and failed across a window of runs."""
    saw_success = False
    saw_failure = False
    for status in effective_statuses:
        saw_success = saw_success or is_successful(status)
        saw_failure = saw_failure or is_failureish(status)
        if saw_success and saw_failure:
            return True
    return False


def flake_rate(
    per_run: Sequence[tuple[TestStatus, bool]],
) -> float | None:
    """Percentage of runs in which a test behaved flakily.

    ``per_run`` is one ``(effective_status, flaky_within_that_run)`` pair per run,
    oldest first. A run counts as flaky for this test when it was flaky within
    the run, or when its effective outcome disagreed with the window's majority
    outcome. Returns ``None`` for fewer than two runs — one data point cannot
    describe consistency.
    """
    considered = [
        (status, within)
        for status, within in per_run
        if status not in (TestStatus.SKIPPED, TestStatus.CANCELLED, TestStatus.UNKNOWN)
    ]
    if len(considered) < 2:
        return None

    successes = sum(1 for status, _ in considered if is_successful(status))
    failures = sum(1 for status, _ in considered if is_failureish(status))
    majority_is_success = successes >= failures

    disagreements = 0
    for status, within_run in considered:
        agreed = is_successful(status) if majority_is_success else is_failureish(status)
        if within_run or not agreed:
            disagreements += 1
    return round(100.0 * disagreements / len(considered), 2)


def run_duration_ms(
    started_at: datetime | None,
    finished_at: datetime | None,
    attempt_durations_ms: Iterable[int] | None = None,
) -> int | None:
    """A run's duration in milliseconds.

    Wall-clock when both ends are known — with parallel workers the sum of test
    durations can exceed the run several times over and is not a duration anyone
    means. Falls back to the sum of attempt durations, and to ``None`` when
    neither is available.
    """
    if started_at is not None and finished_at is not None:
        delta = (finished_at - started_at).total_seconds() * 1000.0
        return max(0, round(delta))
    if attempt_durations_ms is not None:
        total = sum(attempt_durations_ms)
        return int(total) if total else None
    return None


def percentile(values: Sequence[float | int], fraction: float) -> float | None:
    """Linear-interpolated percentile, ``fraction`` in ``[0, 1]``.

    Written out rather than pulled from ``statistics`` so the trend numbers are
    identical on every supported Python and reproducible by hand from the API's
    raw values.
    """
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def aggregate_counts(statuses: Iterable[TestStatus]) -> dict[str, int]:
    """Fold effective statuses into the raw count fields of ``RunCounts``.

    Returns a plain dict so that :mod:`test_hub.domain.models` — which this
    module must not import, to keep the dependency acyclic — can build the model.
    """
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status.value] = counts.get(status.value, 0) + 1
        counts["total"] = counts.get("total", 0) + 1
    return counts
