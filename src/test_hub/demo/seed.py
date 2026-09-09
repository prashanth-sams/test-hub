"""Realistic sample data, so an empty Test Hub can be judged before it is adopted.

Everything here is invented. The projects, the teams, the test names and the
stack traces are made up to look like software that exists without being any
software that does; nothing is copied from a real report.

The data is generated from a fixed seed, so ``test-hub demo`` twice in a row
produces the same dashboard and a screenshot in the documentation keeps matching
the product. What it deliberately includes:

* several teams and projects at different levels of health, because a dashboard
  that only ever shows green tells you nothing about whether red is legible;
* genuinely flaky tests — the same test identity failing and passing across runs,
  and failing then passing *within* one run through a retry;
* a run that is still going, so the LIVE indicator and the SSE path have
  something to show;
* failures with real-looking stack traces, long log output, and one test whose
  name contains HTML, so the escaping is exercised by simply looking at the page;
* the files a run leaves behind — screenshots, a request log, a coverage report,
  a trace — because the Screenshots, API Logs and Test Coverage pages have
  nothing to be judged by otherwise. They are generated, never captured: see
  ``test_hub.demo.evidence``.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from test_hub.demo.evidence import attach_evidence, steps_for
from test_hub.domain.events import (
    DomainEvent,
    EventType,
    RunFinishedData,
    RunQueuedData,
    RunStartedData,
    SuiteFinishedData,
    SuiteStartedData,
    TestFinishedData,
    make_event,
)
from test_hub.domain.identity import suite_id as derive_suite_id
from test_hub.domain.identity import test_identity
from test_hub.domain.models import (
    FailureDetail,
    RunContext,
    TestType,
    TriggerType,
    utcnow,
)
from test_hub.domain.status import RunStatus, TestStatus
from test_hub.logging import get_logger
from test_hub.service.runs import RunService
from test_hub.service.runtime import AppContext

__all__ = ["DemoResult", "seed_demo"]

logger = get_logger(__name__)

SEED = 20260908


@dataclass(slots=True)
class DemoProject:
    key: str
    name: str
    team: str
    component: str
    language: str
    framework: str
    test_type: TestType
    suites: tuple[tuple[str, tuple[str, ...]], ...]
    health: float
    """Roughly the share of tests that pass, before flakiness is applied."""


PROJECTS: tuple[DemoProject, ...] = (
    DemoProject(
        key="payments-api",
        name="Payments API",
        team="payments",
        component="checkout",
        language="python",
        framework="pytest",
        test_type=TestType.COMPONENT,
        suites=(
            (
                "tests/checkout/test_authorization.py",
                (
                    "test_authorizes_a_valid_card",
                    "test_declines_an_expired_card",
                    "test_declines_when_the_issuer_times_out",
                    "test_retries_a_soft_decline_once",
                    "test_records_the_scheme_response_code",
                ),
            ),
            (
                "tests/checkout/test_capture.py",
                (
                    "test_captures_the_full_amount",
                    "test_captures_a_partial_amount",
                    "test_rejects_a_capture_above_the_authorization",
                    "test_is_idempotent_on_the_same_capture_key",
                ),
            ),
            (
                "tests/checkout/test_refunds.py",
                (
                    "test_refunds_in_full",
                    "test_refunds_partially_twice",
                    "test_rejects_a_refund_after_settlement_window",
                ),
            ),
        ),
        health=0.88,
    ),
    DemoProject(
        key="ledger-worker",
        name="Ledger worker",
        team="payments",
        component="ledger",
        language="java",
        framework="JUnit 5",
        test_type=TestType.INTEGRATION,
        suites=(
            (
                "com.example.ledger.PostingTest",
                (
                    "postsADebitAndACredit",
                    "rejectsAnUnbalancedEntry",
                    "collapsesZeroValuePostings",
                    "keepsRunningBalanceInMinorUnits",
                ),
            ),
            (
                "com.example.ledger.ReconciliationTest",
                (
                    "reconcilesASettlementFile",
                    "flagsAnOrphanedPosting",
                    "handlesADuplicateSettlementRow",
                ),
            ),
        ),
        health=0.95,
    ),
    DemoProject(
        key="storefront-web",
        name="Storefront",
        team="growth",
        component="web",
        language="typescript",
        framework="Playwright",
        test_type=TestType.E2E,
        suites=(
            (
                "e2e/basket.spec.ts",
                (
                    "adds an item to the basket",
                    "updates the quantity",
                    "removes the last item and shows the empty state",
                    "keeps the basket after a reload",
                ),
            ),
            (
                "e2e/search.spec.ts",
                (
                    "finds a product by name",
                    "shows a suggestion for a misspelling",
                    "paginates past the first twenty results",
                    "renders <script>alert('xss')</script> safely in the query echo",
                ),
            ),
            (
                "e2e/checkout.spec.ts",
                (
                    "completes a card payment",
                    "shows a validation error for a bad postcode",
                    "recovers from a declined card",
                ),
            ),
        ),
        health=0.78,
    ),
    DemoProject(
        key="notification-gateway",
        name="Notification gateway",
        team="platform",
        component="delivery",
        language="go",
        framework="go test",
        test_type=TestType.UNIT,
        suites=(
            (
                "internal/dispatch",
                (
                    "TestFansOutToEveryChannel",
                    "TestDropsAnUnroutableMessage",
                    "TestRespectsQuietHours",
                    "TestBackoffIsExponential",
                    "TestDeduplicatesWithinTheWindow",
                ),
            ),
            (
                "internal/template",
                (
                    "TestRendersAKnownTemplate",
                    "TestFailsOnAMissingVariable",
                ),
            ),
        ),
        health=0.97,
    ),
    DemoProject(
        key="identity-service",
        name="Identity service",
        team="platform",
        component="auth",
        language="python",
        framework="pytest",
        test_type=TestType.API,
        suites=(
            (
                "tests/test_tokens.py",
                (
                    "test_issues_an_access_token",
                    "test_rejects_an_expired_refresh_token",
                    "test_rotates_a_refresh_token_once",
                    "test_revokes_every_token_for_a_user",
                ),
            ),
            (
                "tests/test_sessions.py",
                (
                    "test_starts_a_session",
                    "test_expires_an_idle_session",
                ),
            ),
        ),
        health=0.92,
    ),
)

#: Tests that are unreliable on purpose, so flakiness has something to detect.
FLAKY_TESTS = {
    "test_declines_when_the_issuer_times_out",
    "keeps the basket after a reload",
    "reconcilesASettlementFile",
    "TestBackoffIsExponential",
}

#: Most CI runs are the same pipeline on the same branch against the same
#: environment — that is what makes run-to-run comparison meaningful. The demo
#: has to reflect that: if every run picked a fresh branch and environment, no
#: run would ever have a comparable predecessor and the comparison panel would
#: be permanently empty, which would misrepresent the product.
MAINLINE = ("staging", "main")
VARIANTS = (
    ("production", "main"),
    ("preview", "release/24.9"),
    ("staging", "feature/basket-rework"),
)
#: Share of runs that take the mainline path.
MAINLINE_SHARE = 0.75

FAILURES: tuple[tuple[str, str, str], ...] = (
    (
        "AssertionError",
        "expected status 200 but got 502",
        '  File "tests/checkout/test_authorization.py", line 84, '
        "in test_authorizes_a_valid_card\n"
        "    assert response.status_code == 200\n"
        "AssertionError: expected status 200 but got 502\n"
        "  where response = <Response [502 Bad Gateway]>",
    ),
    (
        "TimeoutError",
        "issuer did not respond within 3000ms",
        '  File "payments/gateway/issuer.py", line 212, in authorize\n'
        "    return await self._post(payload, timeout=3.0)\n"
        '  File "payments/gateway/http.py", line 61, in _post\n'
        '    raise TimeoutError(f"issuer did not respond within {timeout*1000:.0f}ms")\n'
        "TimeoutError: issuer did not respond within 3000ms",
    ),
    (
        "java.lang.IllegalStateException",
        "unbalanced entry: debits 12500 != credits 12499",
        "\tat com.example.ledger.Posting.validate(Posting.java:88)\n"
        "\tat com.example.ledger.Posting.post(Posting.java:41)\n"
        "\tat com.example.ledger.PostingTest.rejectsAnUnbalancedEntry(PostingTest.java:57)",
    ),
    (
        "Error",
        "locator.click: Timeout 15000ms exceeded waiting for [data-test=basket-item]",
        "    at BasketPage.removeLastItem (e2e/pages/basket.ts:44:18)\n"
        "    at e2e/basket.spec.ts:62:5\n"
        "  Call log:\n"
        "    - waiting for locator('[data-test=basket-item]')\n"
        '    - locator resolved to hidden <li data-test="basket-item">…</li>',
    ),
    (
        "ValidationError",
        "1 validation error for TokenRequest\\nscope: field required",
        '  File "identity/api/tokens.py", line 39, in issue\n'
        "    request = TokenRequest.model_validate(body)\n"
        "pydantic_core._pydantic_core.ValidationError: 1 validation error for TokenRequest\n"
        "scope\n"
        "  Field required [type=missing, input_value={'grant_type': 'refresh'}]",
    ),
)

LOG_LINES = (
    "INFO  starting the case fixture",
    "DEBUG opening a connection to the test double at 127.0.0.1:5544",
    "INFO  seeding 3 accounts",
    "WARN  the issuer double returned a soft decline; retrying once",
    "DEBUG teardown removed 3 accounts",
)


@dataclass(slots=True)
class DemoResult:
    """What the seeder created, for the CLI to report."""

    projects: int = 0
    runs: int = 0
    tests: int = 0
    attempts: int = 0
    live_run_id: uuid.UUID | None = None
    failures: int = 0
    flaky: int = 0
    artifacts: int = 0


def seed_demo(
    context: AppContext,
    *,
    days: int = 14,
    runs_per_project: int = 6,
    include_live_run: bool = True,
) -> DemoResult:
    """Fill the store with sample data. Deterministic for a given seed."""
    generator = random.Random(SEED)
    result = DemoResult(projects=len(PROJECTS))
    now = utcnow()
    # Files go in through the same service a real publisher uses, so they are
    # stored content-addressed and announced as `artifact.published` events —
    # the demo is not allowed a private door into the store.
    runs_service = RunService(context)

    for project in PROJECTS:
        for index in range(runs_per_project):
            age = timedelta(
                days=(days * (runs_per_project - index - 1)) / max(1, runs_per_project),
                hours=generator.uniform(0, 6),
            )
            started = now - age
            events, stats = _build_run(
                project, started, generator, workspace_id=context.workspace_id, live=False
            )
            _apply(context, events)
            # After the run has been applied, never during: an artifact belongs
            # to a run that exists, and `add_artifact` says so.
            result.artifacts += attach_evidence(
                runs_service,
                events[0].run_id,
                events,
                framework=project.framework,
                project_key=project.key,
                started=started,
            )
            result.runs += 1
            result.tests += stats[0]
            result.attempts += stats[1]
            result.failures += stats[2]
            result.flaky += stats[3]

    if include_live_run:
        project = PROJECTS[0]
        events, _ = _build_run(
            project,
            now - timedelta(minutes=4),
            generator,
            workspace_id=context.workspace_id,
            live=True,
        )
        _apply(context, events)
        # No evidence on the live run: coverage is written when a run ends, and
        # every page that reads attachments reads finished runs only.
        result.runs += 1
        result.live_run_id = events[0].run_id
        logger.info("live demo run %s is still open", result.live_run_id)

    return result


def _apply(context: AppContext, events: list[DomainEvent]) -> None:
    with context.uow_factory() as uow:
        uow.events.apply_events(events)
        uow.commit()


def _build_run(
    project: DemoProject,
    started: datetime,
    generator: random.Random,
    *,
    workspace_id: uuid.UUID,
    live: bool,
) -> tuple[list[DomainEvent], tuple[int, int, int, int]]:
    """Build the full event stream for one demo run."""
    run_id = uuid.uuid4()
    events: list[DomainEvent] = []
    sequence = iter(range(1, 100_000))
    if generator.random() < MAINLINE_SHARE:
        environment, branch = MAINLINE
    else:
        environment, branch = generator.choice(VARIANTS)
    build = generator.randrange(1200, 1900)

    def emit(event_type: EventType, data: object, at: datetime) -> None:
        events.append(
            make_event(
                event_type,
                run_id=run_id,
                workspace_id=workspace_id,
                sequence=next(sequence),
                source="urn:test-hub:demo",
                time=at,
                data=data,  # type: ignore[arg-type]
            )
        )

    emit(
        EventType.RUN_QUEUED,
        RunQueuedData(
            project_key=project.key,
            name=f"{project.name} #{build}",
            team=project.team,
            component=project.component,
            test_type=project.test_type,
            trigger=generator.choice(
                (TriggerType.PUSH, TriggerType.PUSH, TriggerType.PULL_REQUEST, TriggerType.SCHEDULE)
            ),
            context=RunContext(
                framework=project.framework,
                language=project.language,
                environment=environment,
                branch=branch,
                commit_sha=f"{generator.randrange(16**12):012x}",
                pipeline_id=str(build),
                pipeline_url=f"https://ci.example.com/{project.key}/builds/{build}",
                ci_provider="github-actions",
                executor="ci",
                os=generator.choice(("ubuntu-24.04", "ubuntu-24.04", "macos-14")),
            ),
            tags=[project.team, environment],
        ),
        started,
    )
    emit(EventType.RUN_STARTED, RunStartedData(started_at=started), started)

    clock = started
    total_tests = total_attempts = failures = flaky_count = 0

    for suite_index, (suite_path, test_names) in enumerate(project.suites):
        if live and suite_index >= len(project.suites) - 1:
            break  # the last suite has not been reached yet — that is the point
        suite_uuid = derive_suite_id(project=project.key, suite_path=suite_path)
        suite_started = clock
        emit(
            EventType.SUITE_STARTED,
            SuiteStartedData(
                suite_id=suite_uuid,
                name=suite_path.rsplit("/", 1)[-1],
                path=suite_path,
                started_at=suite_started,
            ),
            suite_started,
        )

        for name in test_names:
            total_tests += 1
            duration = generator.randrange(40, 2600)
            clock += timedelta(milliseconds=duration + generator.randrange(5, 60))
            is_flaky = name in FLAKY_TESTS and generator.random() < 0.45
            passes = generator.random() < project.health

            if is_flaky:
                # A retry: the first attempt fails, the second passes. This is
                # where the flaky flag comes from — never from a status word.
                _emit_test(
                    emit,
                    project,
                    suite_uuid,
                    suite_path,
                    name,
                    TestStatus.FAILED,
                    duration,
                    clock,
                    generator,
                    attempt=1,
                )
                clock += timedelta(milliseconds=duration)
                _emit_test(
                    emit,
                    project,
                    suite_uuid,
                    suite_path,
                    name,
                    TestStatus.PASSED,
                    duration,
                    clock,
                    generator,
                    attempt=2,
                )
                total_attempts += 2
                flaky_count += 1
                continue

            if passes:
                status = TestStatus.PASSED
            elif generator.random() < 0.12:
                status = TestStatus.SKIPPED
            elif generator.random() < 0.2:
                status = TestStatus.ERROR
            else:
                status = TestStatus.FAILED
            if status in (TestStatus.FAILED, TestStatus.ERROR):
                failures += 1
            _emit_test(
                emit,
                project,
                suite_uuid,
                suite_path,
                name,
                status,
                duration,
                clock,
                generator,
                attempt=1,
            )
            total_attempts += 1

        emit(
            EventType.SUITE_FINISHED,
            SuiteFinishedData(
                suite_id=suite_uuid,
                finished_at=clock,
                duration_ms=int((clock - suite_started).total_seconds() * 1000),
            ),
            clock,
        )

    if not live:
        emit(
            EventType.RUN_FINISHED,
            RunFinishedData(
                finished_at=clock,
                status=RunStatus.CANCELLED if generator.random() < 0.03 else None,
                duration_ms=int((clock - started).total_seconds() * 1000),
            ),
            clock,
        )

    return events, (total_tests, total_attempts, failures, flaky_count)


def _emit_test(
    emit: object,
    project: DemoProject,
    suite_uuid: uuid.UUID,
    suite_path: str,
    name: str,
    status: TestStatus,
    duration_ms: int,
    at: datetime,
    generator: random.Random,
    *,
    attempt: int,
) -> None:
    """Emit one ``test.finished`` event, with failure detail when it did not pass."""
    failure = None
    skip_reason = None
    if status in (TestStatus.FAILED, TestStatus.ERROR):
        kind, message, trace = generator.choice(FAILURES)
        failure = FailureDetail(type=kind, message=message, stack_trace=trace)
    elif status is TestStatus.SKIPPED:
        skip_reason = generator.choice(
            ("needs the sandbox issuer", "flaky on macOS runners", "pending PAYMENTS-4821")
        )

    logs = "\n".join(generator.sample(LOG_LINES, k=generator.randrange(2, len(LOG_LINES))))
    # Steps ride in the attempt's metadata, which is where the Test Steps page
    # looks for them until there is a step model to put them in. Only the
    # browser suite publishes any — a page that is empty for four projects out
    # of five is the honest picture of where this stands.
    metadata: dict[str, object] = {}
    steps = steps_for(suite_path, name)
    if steps:
        if status in (TestStatus.FAILED, TestStatus.ERROR):
            # The failure landed somewhere: the last step is where it stopped.
            steps[-1]["status"] = status.value
        metadata["steps"] = steps
    emit(  # type: ignore[operator]
        EventType.TEST_FINISHED,
        TestFinishedData(
            test_id=test_identity(project=project.key, suite_path=suite_path, test_name=name),
            suite_id=suite_uuid,
            name=name,
            suite_path=suite_path,
            full_name=f"{suite_path}::{name}",
            file=suite_path,
            attempt_number=attempt,
            status=status,
            original_status=status.value,
            finished_at=at,
            duration_ms=duration_ms,
            failure=failure,
            skip_reason=skip_reason,
            logs=logs,
            tags=[project.team],
            worker=f"gw{generator.randrange(0, 4)}",
            metadata=metadata,
        ),
        at,
    )
