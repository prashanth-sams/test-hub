"""The Python publishing SDK, against a real server.

The SDK is a convenience over an API that is designed to be usable with `curl`
(ADR-0001). These tests hold it to that: everything asserted here has a
`curl` equivalent in ``docs/api.md``, and nothing the SDK does may require a
capability the plain HTTP API lacks.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from test_hub.domain.models import FailureDetail, RunContext, TestType
from test_hub.domain.status import RunStatus, TestStatus
from test_hub.sdk import TestHubClient, TestHubClientError
from tests.paths import junit

pytestmark = pytest.mark.slow


@pytest.fixture
def sdk(live_server: Any) -> Any:
    with TestHubClient(live_server.url, retries=0) as client:
        yield client


def test_health_reports_the_workspace(sdk: TestHubClient) -> None:
    body = sdk.health()
    assert body["status"] == "ok"
    assert uuid.UUID(body["workspace_id"])


def test_publish_report_uploads_a_file(sdk: TestHubClient) -> None:
    result = sdk.publish_report(
        junit("simple.xml"),
        project_key="payments-api",
        team="payments",
        test_type=TestType.COMPONENT,
        context=RunContext(environment="staging", branch="main", framework="JUnit 5"),
        tags=["nightly"],
    )
    assert result.adapter == "junit-xml"
    assert result.counts.total == 5
    assert result.counts.pass_rate == 50.0

    detail = sdk.get_run(result.run_id)
    assert detail["run"]["team"] == "payments"
    assert detail["run"]["context"]["environment"] == "staging"
    assert "nightly" in detail["run"]["tags"]


def test_publish_report_accepts_a_string_test_type(sdk: TestHubClient) -> None:
    """Callers should not have to import an enum to publish a result."""
    result = sdk.publish_report(junit("simple.xml"), project_key="p", test_type="e2e")
    assert sdk.get_run(result.run_id)["run"]["test_type"] == "e2e"


def test_publish_report_is_idempotent_on_a_run_id(sdk: TestHubClient) -> None:
    run_id = uuid.uuid4()
    first = sdk.publish_report(junit("simple.xml"), project_key="p", run_id=run_id)
    second = sdk.publish_report(junit("simple.xml"), project_key="p", run_id=run_id)
    assert first.run_id == second.run_id == run_id
    assert second.events_applied == 0
    assert second.counts.total == first.counts.total


def test_the_live_publishing_lifecycle(sdk: TestHubClient) -> None:
    run = sdk.open_run(
        project_key="storefront-web",
        team="growth",
        test_type="e2e",
        context=RunContext(framework="Playwright", language="typescript"),
        expected_tests=3,
    )
    assert run.status is RunStatus.RUNNING
    assert run.is_live is True

    sdk.finish_test(
        run.id,
        suite="e2e/basket.spec.ts",
        name="adds an item",
        status=TestStatus.PASSED,
        duration_ms=812,
        project_key="storefront-web",
    )
    sdk.finish_test(
        run.id,
        suite="e2e/basket.spec.ts",
        name="removes an item",
        status="failed",
        duration_ms=1200,
        project_key="storefront-web",
        failure=FailureDetail(type="Error", message="locator timeout", stack_trace="at x.ts:1"),
    )
    finished = sdk.close_run(run.id)

    assert finished.status is RunStatus.FAILED
    assert finished.counts.total == 2
    assert finished.counts.failed == 1
    assert finished.is_live is False


def test_a_retry_is_reported_as_a_second_attempt(sdk: TestHubClient) -> None:
    """The SDK's shape must make the right thing the easy thing."""
    run = sdk.open_run(project_key="flaky-project", test_type="integration")
    for attempt, status in ((1, "failed"), (2, "passed")):
        sdk.finish_test(
            run.id,
            suite="tests/test_net.py",
            name="test_retries",
            status=status,
            attempt_number=attempt,
            duration_ms=50,
            project_key="flaky-project",
        )
    sdk.close_run(run.id)

    detail = sdk.get_run(run.id)
    test = detail["tests"][0]
    assert len(test["attempts"]) == 2
    assert test["status"] == "passed"
    assert test["flaky"] is True
    assert detail["run"]["counts"]["flaky"] == 1


def test_an_unknown_status_word_is_normalised_not_rejected(sdk: TestHubClient) -> None:
    run = sdk.open_run(project_key="p")
    sdk.finish_test(run.id, suite="s", name="t", status="Success", duration_ms=1, project_key="p")
    sdk.close_run(run.id)
    assert sdk.get_run(run.id)["tests"][0]["status"] == "passed"


def test_the_client_learns_the_workspace_id_and_caches_it(sdk: TestHubClient) -> None:
    """A publisher cannot invent this value, so it asks — once."""
    first = sdk.workspace_id()
    assert isinstance(first, uuid.UUID)
    assert sdk.workspace_id() is first


def test_artifacts_can_be_uploaded(sdk: TestHubClient, tmp_path: Path) -> None:
    run = sdk.open_run(project_key="p")
    shot = tmp_path / "failure.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\nnot-really-a-png")

    artifact = sdk.upload_artifact(run.id, shot, kind="screenshot")
    assert artifact.name == "failure.png"
    assert artifact.size_bytes == shot.stat().st_size
    assert len(artifact.sha256) == 64


def test_errors_carry_the_servers_structured_code(sdk: TestHubClient, tmp_path: Path) -> None:
    """A caller should be able to branch on `error.code`, not parse a message."""
    junk = tmp_path / "notes.txt"
    junk.write_text("not a test report", encoding="utf-8")

    with pytest.raises(TestHubClientError) as error:
        sdk.publish_report(junk, project_key="p")
    assert error.value.status_code == 415
    assert error.value.code == "unknown_format"
    assert error.value.detail["installed_adapters"]


def test_a_missing_run_is_a_clean_error(sdk: TestHubClient) -> None:
    with pytest.raises(TestHubClientError) as error:
        sdk.get_run(uuid.uuid4())
    assert error.value.status_code == 404
    assert error.value.code == "not_found"


def test_an_unreachable_server_says_so_rather_than_hanging(tmp_path: Path) -> None:
    with (
        TestHubClient("http://127.0.0.1:1", timeout=1.0, retries=0) as client,
        pytest.raises(TestHubClientError, match="could not reach"),
    ):
        client.health()


def test_a_token_is_sent_as_a_bearer_header(live_server: Any) -> None:
    with TestHubClient(live_server.url, token="ths_example") as client:
        assert client._client.headers["authorization"] == "Bearer ths_example"


def test_the_overview_is_readable_through_the_sdk(sdk: TestHubClient) -> None:
    sdk.publish_report(junit("simple.xml"), project_key="payments-api")
    body = sdk.overview(window_days=365)
    assert body["total_runs_all_time"] >= 1
    assert body["schema_version"] == 1


def test_the_overview_window_is_capped_by_the_api(sdk: TestHubClient) -> None:
    """A polled endpoint needs a bound. Ten years of daily buckets is not a bound."""
    with pytest.raises(TestHubClientError) as error:
        sdk.overview(window_days=3650)
    assert error.value.status_code == 422
