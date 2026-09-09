"""Canonical test and run statuses, and the normalization that produces them.

Every framework spells its outcomes differently. Test Hub stores exactly one
normalized status per attempt, and *always* keeps the original string beside it
in ``TestAttempt.original_status`` so nothing is lost in translation.

The normalization is deliberately a lookup table rather than a heuristic. A
string we do not recognise becomes :data:`TestStatus.UNKNOWN` and raises a parser
warning; it never becomes ``passed`` by accident.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "RUN_TERMINAL_STATUSES",
    "STATUS_ALIASES",
    "TERMINAL_STATUSES",
    "RunStatus",
    "TestStatus",
    "is_failureish",
    "is_successful",
    "normalize_status",
    "worst_status",
]


class TestStatus(StrEnum):
    """The canonical outcome of a single test attempt.

    The set is closed. Adding a member is a wire-contract change and needs a
    ``SCHEMA_VERSION`` bump, because dashboards and adapters both switch on it.
    """

    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    XFAILED = "xfailed"
    XPASSED = "xpassed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    """The state of a whole run.

    ``PASSED``/``FAILED``/``ERROR`` are derived by folding the attempts of the
    run; ``QUEUED``/``RUNNING`` come from the event stream; ``CANCELLED`` is
    explicit.
    """

    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


#: Attempt statuses after which no further result is expected for that attempt.
TERMINAL_STATUSES: frozenset[TestStatus] = frozenset(
    {
        TestStatus.PASSED,
        TestStatus.FAILED,
        TestStatus.ERROR,
        TestStatus.SKIPPED,
        TestStatus.XFAILED,
        TestStatus.XPASSED,
        TestStatus.CANCELLED,
        TestStatus.UNKNOWN,
    }
)

#: Run statuses after which the run is closed.
RUN_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.PASSED, RunStatus.FAILED, RunStatus.ERROR, RunStatus.CANCELLED}
)


def _aliases() -> dict[str, TestStatus]:
    """Build the alias table.

    Grouped by the framework family that spells it that way, so that adding a
    dialect is an obvious one-line change and a reviewer can see where a word
    came from. Keys are matched case-insensitively with surrounding whitespace,
    hyphens and underscores collapsed — see :func:`normalize_status`.
    """

    table: dict[str, TestStatus] = {}

    def add(status: TestStatus, *words: str) -> None:
        for word in words:
            table[word] = status

    # Generic / JUnit / xUnit / TAP
    add(TestStatus.PASSED, "passed", "pass", "success", "successful", "succeeded", "ok", "green")
    add(TestStatus.FAILED, "failed", "fail", "failure", "failures", "assertionfailure", "red")
    add(TestStatus.ERROR, "error", "errored", "errors", "broken", "exception", "crashed")
    add(
        TestStatus.SKIPPED,
        "skipped",
        "skip",
        "ignored",
        "notrun",
        "notexecuted",
        "pending",
        "disabled",
        "excluded",
        "todo",
        "undefined",
    )
    add(
        TestStatus.CANCELLED,
        "cancelled",
        "canceled",
        "aborted",
        "interrupted",
        "timedout",
        "timeout",
        "terminated",
    )
    add(TestStatus.QUEUED, "queued", "scheduled", "waiting", "notstarted", "created")
    add(TestStatus.RUNNING, "running", "started", "inprogress", "executing")

    # pytest / unittest expectations
    add(
        TestStatus.XFAILED,
        "xfailed",
        "xfail",
        "expectedfailure",
        "expectedfail",
        "knownfailure",
        "expectedtofail",
    )
    add(
        TestStatus.XPASSED,
        "xpassed",
        "xpass",
        "unexpectedsuccess",
        "unexpectedpass",
        "unexpectedlypassed",
    )

    # .NET (TRX / NUnit) — mapped now so a future adapter package inherits it.
    add(TestStatus.PASSED, "passedbutrunaborted")
    add(TestStatus.ERROR, "notrunnable", "notexecutederror")
    add(TestStatus.SKIPPED, "inconclusive", "notimplemented", "warning")
    add(TestStatus.CANCELLED, "aborted", "blocked")

    # JavaScript (Jest / Playwright / Mocha / Cucumber)
    add(TestStatus.PASSED, "expected")
    add(TestStatus.FAILED, "unexpected")
    add(TestStatus.SKIPPED, "ambiguous")
    add(TestStatus.XFAILED, "flaky")  # Playwright: passed on retry — see note below.

    return table


#: Normalized-word → canonical status. See :func:`normalize_status` for how the
#: input word is normalized before lookup.
STATUS_ALIASES: dict[str, TestStatus] = _aliases()

# Note on Playwright's "flaky": it means "failed then passed on retry". Test Hub
# models that as two attempts (one failed, one passed) and derives flakiness from
# the attempts — see `test_hub.domain.metrics.is_flaky`. The alias exists only so
# a report that carries the word verbatim in a status column does not become
# UNKNOWN; adapters that can see the attempts should emit the attempts instead.


def _key(raw: str) -> str:
    """Collapse a raw status word into its lookup key."""
    return "".join(ch for ch in raw.strip().lower() if ch.isalnum())


def normalize_status(raw: str | None, *, default: TestStatus = TestStatus.UNKNOWN) -> TestStatus:
    """Map a framework's status word onto a :class:`TestStatus`.

    ``None``, an empty string or an unrecognised word yields ``default``
    (``UNKNOWN``). Callers that care — every adapter does — should compare the
    result against ``UNKNOWN`` and record a parser warning, so that an unknown
    dialect surfaces as a warning on the import rather than as a silently wrong
    pass rate.

    >>> normalize_status("Success")
    <TestStatus.PASSED: 'passed'>
    >>> normalize_status("expected failure")
    <TestStatus.XFAILED: 'xfailed'>
    >>> normalize_status("weird")
    <TestStatus.UNKNOWN: 'unknown'>
    """
    if raw is None:
        return default
    key = _key(raw)
    if not key:
        return default
    return STATUS_ALIASES.get(key, default)


def is_successful(status: TestStatus) -> bool:
    """True for statuses that count towards the pass rate's numerator.

    ``xfailed`` counts as success: the test failed exactly as it was declared to.
    ``xpassed`` does not: an expectation that no longer holds is a signal.
    """
    return status in (TestStatus.PASSED, TestStatus.XFAILED)


def is_failureish(status: TestStatus) -> bool:
    """True for statuses that count as a failure for the failure rate."""
    return status in (TestStatus.FAILED, TestStatus.ERROR, TestStatus.XPASSED)


#: Severity order used to fold many attempt statuses into one run status. Later
#: entries win.
_SEVERITY: tuple[TestStatus, ...] = (
    TestStatus.UNKNOWN,
    TestStatus.SKIPPED,
    TestStatus.XFAILED,
    TestStatus.PASSED,
    TestStatus.XPASSED,
    TestStatus.QUEUED,
    TestStatus.RUNNING,
    TestStatus.CANCELLED,
    TestStatus.FAILED,
    TestStatus.ERROR,
)


def worst_status(statuses: object) -> TestStatus:
    """Fold an iterable of statuses into the most severe one.

    Used to give a suite or a run a single colour. An empty iterable is
    ``UNKNOWN`` — a suite with no tests has no outcome, it is not a pass.
    """
    worst = TestStatus.UNKNOWN
    worst_rank = -1
    for status in statuses:  # type: ignore[attr-defined]
        rank = _SEVERITY.index(status)
        if rank > worst_rank:
            worst, worst_rank = status, rank
    return worst
