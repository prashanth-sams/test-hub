"""The formulas behind every number on the dashboard.

These tests are the executable half of ``docs/metrics.md``. If a definition
changes, both must change together.
"""

from __future__ import annotations

from datetime import timedelta

from test_hub.domain.metrics import (
    effective_status,
    flake_rate,
    is_flaky_across_runs,
    is_flaky_within_run,
    percentile,
    run_duration_ms,
)
from test_hub.domain.models import RunCounts, utcnow
from test_hub.domain.status import TestStatus as S


def test_effective_status_is_the_last_attempt_not_the_best() -> None:
    assert effective_status([S.FAILED, S.PASSED]) is S.PASSED
    assert effective_status([S.PASSED, S.FAILED]) is S.FAILED
    assert effective_status([]) is S.UNKNOWN


def test_pass_rate_excludes_skipped_from_the_denominator() -> None:
    """8 passed, 1 failed, 1 skipped is 88.89%, not 80%."""
    counts = RunCounts(total=10, passed=8, failed=1, skipped=1)
    assert counts.executed == 9
    assert counts.pass_rate == 88.89


def test_xfailed_is_a_pass_and_xpassed_is_a_failure() -> None:
    counts = RunCounts(total=4, passed=1, xfailed=1, xpassed=1, failed=1)
    assert counts.pass_rate == 50.0
    assert counts.failure_rate == 50.0


def test_pass_rate_is_none_not_zero_when_nothing_executed() -> None:
    """A run that only skipped has no pass rate. Showing 0% would read as failure."""
    counts = RunCounts(total=3, skipped=3)
    assert counts.executed == 0
    assert counts.pass_rate is None
    assert counts.failure_rate is None


def test_running_and_queued_are_excluded_from_completed() -> None:
    counts = RunCounts(total=5, passed=3, running=1, queued=1)
    assert counts.completed == 3
    assert counts.pass_rate == 100.0


def test_counts_add_and_merge() -> None:
    counts = RunCounts()
    for status in (S.PASSED, S.PASSED, S.FAILED, S.SKIPPED):
        counts.add(status)
    assert (counts.total, counts.passed, counts.failed, counts.skipped) == (4, 2, 1, 1)
    merged = counts.merge(RunCounts(total=1, passed=1))
    assert merged.total == 5
    assert merged.passed == 3


def test_flaky_within_run_needs_disagreement_and_two_attempts() -> None:
    assert is_flaky_within_run([S.FAILED, S.PASSED])
    assert is_flaky_within_run([S.PASSED, S.FAILED, S.PASSED])
    assert not is_flaky_within_run([S.PASSED])
    assert not is_flaky_within_run([S.FAILED, S.FAILED])
    assert not is_flaky_within_run([S.PASSED, S.PASSED])


def test_skips_do_not_make_a_test_flaky() -> None:
    assert not is_flaky_within_run([S.SKIPPED, S.PASSED])


def test_flaky_across_runs() -> None:
    assert is_flaky_across_runs([S.PASSED, S.PASSED, S.FAILED])
    assert not is_flaky_across_runs([S.PASSED, S.PASSED])
    assert not is_flaky_across_runs([S.FAILED, S.ERROR])


def test_flake_rate_needs_at_least_two_runs() -> None:
    assert flake_rate([(S.PASSED, False)]) is None
    assert flake_rate([]) is None


def test_flake_rate_counts_disagreement_with_the_majority() -> None:
    """Three passes and one failure is one disagreement in four: 25%."""
    history = [(S.PASSED, False), (S.PASSED, False), (S.PASSED, False), (S.FAILED, False)]
    assert flake_rate(history) == 25.0


def test_flake_rate_counts_within_run_flakiness_even_when_the_result_agreed() -> None:
    """A test that passed only because it was retried is flaky, not green."""
    history = [(S.PASSED, False), (S.PASSED, True)]
    assert flake_rate(history) == 50.0


def test_flake_rate_of_a_consistently_green_test_is_zero() -> None:
    assert flake_rate([(S.PASSED, False)] * 10) == 0.0


def test_run_duration_prefers_wall_clock_over_the_sum_of_tests() -> None:
    """With parallel workers the sum can exceed the run several times over."""
    start = utcnow()
    end = start + timedelta(seconds=30)
    assert run_duration_ms(start, end, [20_000, 25_000, 18_000]) == 30_000


def test_run_duration_falls_back_to_the_sum_then_to_none() -> None:
    assert run_duration_ms(None, None, [100, 200]) == 300
    assert run_duration_ms(None, None, []) is None
    assert run_duration_ms(None, None, None) is None


def test_percentile_interpolates_and_handles_edges() -> None:
    assert percentile([10], 0.95) == 10
    assert percentile([], 0.5) is None
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert percentile([1, 2, 3, 4], 0.0) == 1
    assert percentile([1, 2, 3, 4], 1.0) == 4
