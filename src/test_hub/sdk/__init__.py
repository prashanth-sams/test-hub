"""The Python publishing SDK.

A convenience, never a requirement. The REST API is designed so that ``curl`` is
a first-class publisher (ADR-0001, and every example in ``docs/api.md`` proves
it); this client exists because a Python test framework plugin should not have to
hand-roll multipart uploads and retry logic.

Two ways to use it::

    # 1. Upload a finished report
    with TestHubClient("http://localhost:8000", token=os.environ["TEST_HUB_TOKEN"]) as client:
        result = client.publish_report("results.xml", project_key="payments-api")

    # 2. Stream results as they happen
    with TestHubClient(url, token=token) as client:
        run = client.open_run(project_key="payments-api", test_type="e2e")
        client.finish_test(run.id, suite="tests/checkout.py", name="test_pay",
                           status="passed", duration_ms=812)
        client.close_run(run.id)

Both paths are idempotent: pass ``idempotency_key``, retry freely.
"""

from __future__ import annotations

from test_hub.sdk.client import TestHubClient, TestHubClientError

__all__ = ["TestHubClient", "TestHubClientError"]
