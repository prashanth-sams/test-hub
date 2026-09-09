"""Status normalization: the table, the fallback, and what must never happen."""

from __future__ import annotations

import pytest

from test_hub.domain.status import (
    RUN_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    RunStatus,
    TestStatus,
    is_failureish,
    is_successful,
    normalize_status,
    worst_status,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("passed", TestStatus.PASSED),
        ("PASSED", TestStatus.PASSED),
        ("  Pass  ", TestStatus.PASSED),
        ("success", TestStatus.PASSED),
        ("Successful", TestStatus.PASSED),
        ("ok", TestStatus.PASSED),
        ("failed", TestStatus.FAILED),
        ("FAILURE", TestStatus.FAILED),
        ("assertion-failure", TestStatus.FAILED),
        ("error", TestStatus.ERROR),
        ("broken", TestStatus.ERROR),
        ("skipped", TestStatus.SKIPPED),
        ("not run", TestStatus.SKIPPED),
        ("not_run", TestStatus.SKIPPED),
        ("Ignored", TestStatus.SKIPPED),
        ("xfail", TestStatus.XFAILED),
        ("expected failure", TestStatus.XFAILED),
        ("expectedFailure", TestStatus.XFAILED),
        ("xpassed", TestStatus.XPASSED),
        ("unexpected success", TestStatus.XPASSED),
        ("cancelled", TestStatus.CANCELLED),
        ("canceled", TestStatus.CANCELLED),
        ("aborted", TestStatus.CANCELLED),
        ("running", TestStatus.RUNNING),
        ("in progress", TestStatus.RUNNING),
        ("queued", TestStatus.QUEUED),
    ],
)
def test_known_dialects_normalize(raw: str, expected: TestStatus) -> None:
    assert normalize_status(raw) is expected


@pytest.mark.parametrize("raw", ["", "   ", None, "wat", "purple", "0", "!!!"])
def test_unknown_words_become_unknown_not_passed(raw: str | None) -> None:
    """The important half of the contract.

    An unrecognised word must never be optimistically read as a pass — that would
    turn an unsupported dialect into a silently green dashboard.
    """
    assert normalize_status(raw) is TestStatus.UNKNOWN


def test_separators_and_case_are_ignored() -> None:
    for spelling in ("not-run", "not_run", "NotRun", "  NOT RUN  ", "not.run"):
        assert normalize_status(spelling) is TestStatus.SKIPPED


def test_default_can_be_overridden_but_is_explicit() -> None:
    assert normalize_status("nonsense", default=TestStatus.ERROR) is TestStatus.ERROR


def test_xfailed_counts_as_success_xpassed_does_not() -> None:
    """A test that failed as declared is fine; an expectation that stopped holding is news."""
    assert is_successful(TestStatus.XFAILED)
    assert not is_successful(TestStatus.XPASSED)
    assert is_failureish(TestStatus.XPASSED)
    assert not is_failureish(TestStatus.SKIPPED)


def test_worst_status_folds_by_severity() -> None:
    assert worst_status([TestStatus.PASSED, TestStatus.SKIPPED]) is TestStatus.PASSED
    assert worst_status([TestStatus.PASSED, TestStatus.FAILED]) is TestStatus.FAILED
    assert worst_status([TestStatus.FAILED, TestStatus.ERROR]) is TestStatus.ERROR
    assert worst_status([TestStatus.RUNNING, TestStatus.PASSED]) is TestStatus.RUNNING


def test_empty_fold_is_unknown_not_passed() -> None:
    """A suite with no tests has no outcome. Calling that green is a lie."""
    assert worst_status([]) is TestStatus.UNKNOWN


def test_status_enums_serialize_as_their_value() -> None:
    assert f"{TestStatus.XFAILED}" == "xfailed"
    assert f"{RunStatus.RUNNING}" == "running"


def test_terminal_sets_are_coherent() -> None:
    assert TestStatus.RUNNING not in TERMINAL_STATUSES
    assert TestStatus.QUEUED not in TERMINAL_STATUSES
    assert TestStatus.PASSED in TERMINAL_STATUSES
    assert RunStatus.RUNNING not in RUN_TERMINAL_STATUSES
    assert RunStatus.PASSED in RUN_TERMINAL_STATUSES
