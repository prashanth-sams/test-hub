"""Shared fixtures.

Every fixture that touches state builds it in a temporary directory and tears it
down afterwards, so the suite never reads or writes the developer's real Test Hub
data directory. That is not paranoia: the default data directory is a real place
with real results in it, and a test that truncates it would be a very bad day.

**Running on PostgreSQL.** Set ``TEST_HUB_TEST_DATABASE_URL`` to a database the
suite may create schemas in, and every test built on the ``settings`` fixture —
the storage, service, API, concurrency and retention tests — runs against
PostgreSQL instead of SQLite, each in a throwaway schema of its own::

    TEST_HUB_TEST_DATABASE_URL=postgresql://test_hub:test_hub@localhost/test_hub pytest

Tests marked ``postgres`` exercise behaviour only PostgreSQL has, and are skipped
when the variable is unset.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from test_hub.config import Settings, reset_settings_cache
from test_hub.domain.events import (
    EventType,
    RunFinishedData,
    RunQueuedData,
    RunStartedData,
    SuiteStartedData,
    TestFinishedData,
    make_event,
)
from test_hub.domain.identity import suite_id as derive_suite_id
from test_hub.domain.identity import test_identity
from test_hub.domain.models import FailureDetail, RunContext, TestType, utcnow
from test_hub.domain.status import TestStatus

FIXTURES = Path(__file__).parent / "fixtures"

#: The PostgreSQL server the suite may use, or None to stay on SQLite.
POSTGRES_TEST_URL = os.environ.get("TEST_HUB_TEST_DATABASE_URL", "").strip() or None


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Skip PostgreSQL-only tests, visibly, when no server was configured."""
    if POSTGRES_TEST_URL:
        return
    skip = pytest.mark.skip(reason="set TEST_HUB_TEST_DATABASE_URL to run PostgreSQL tests")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@contextmanager
def postgres_schema(base_url: str) -> Iterator[str]:
    """A fresh, empty schema on ``base_url``, and a URL whose search_path is it.

    A schema rather than a database because ``CREATE DATABASE`` costs a second
    or so and needs a privilege the test role may not have; a schema per test
    keeps the tests exactly as isolated as a temporary SQLite file does.
    """
    import sqlalchemy as sa
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from test_hub.config import normalize_database_url

    schema = f"th_test_{uuid.uuid4().hex[:12]}"
    url = make_url(normalize_database_url(base_url))
    admin = sa.create_engine(url, poolclass=NullPool)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with admin.begin() as connection:
            # Bounded, so a connection some test leaked mid-transaction fails
            # this teardown loudly instead of hanging the suite.
            connection.exec_driver_sql("SET LOCAL lock_timeout = '15s'")
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


@pytest.fixture(autouse=True)
def _isolate_settings() -> Iterator[None]:
    """Never let a cached Settings leak between tests."""
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def database_url() -> Iterator[str | None]:
    """``None`` (SQLite in the data directory), or a throwaway PostgreSQL schema."""
    if POSTGRES_TEST_URL is None:
        yield None
        return
    with postgres_schema(POSTGRES_TEST_URL) as url:
        yield url


@pytest.fixture
def postgres_url() -> Iterator[str]:
    """A throwaway PostgreSQL schema, for tests of PostgreSQL-only behaviour."""
    if POSTGRES_TEST_URL is None:  # pragma: no cover - the marker skips first
        pytest.skip("set TEST_HUB_TEST_DATABASE_URL to run PostgreSQL tests")
    with postgres_schema(POSTGRES_TEST_URL) as url:
        yield url


@pytest.fixture
def settings(tmp_path: Path, database_url: str | None) -> Settings:
    """Settings pointed at a throwaway directory (and database)."""
    return Settings(
        data_dir=tmp_path / "data",
        database_url=database_url,
        host="127.0.0.1",
        auth_enabled=False,
        store_raw_reports=False,
        sse_heartbeat_seconds=0.05,
    )


@pytest.fixture
def context(settings: Settings) -> Iterator[Any]:
    """A fully wired AppContext on a temporary database."""
    from test_hub.service.runtime import create_context

    ctx = create_context(settings, configure_logs=False)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def client(context: Any) -> Iterator[Any]:
    """A TestClient bound to the temporary context."""
    from fastapi.testclient import TestClient

    from test_hub.api.app import create_app

    app = create_app(context.settings, context=context)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURES


@dataclass
class LiveServer:
    """A real Uvicorn server on a real port."""

    url: str
    context: Any

    def client(self, **kwargs: Any) -> Any:
        import httpx

        return httpx.Client(base_url=self.url, timeout=10.0, **kwargs)


@pytest.fixture
def live_server(context: Any) -> Iterator[LiveServer]:
    """Boot the app under Uvicorn in a background thread.

    ``TestClient`` runs the application through a portal on the calling thread,
    which cannot interleave a long-lived streaming response with concurrent
    requests from another thread — so Server-Sent Events cannot be tested
    through it. A real server can, and it is what users actually run.
    """
    import socket
    import threading
    import time

    import uvicorn

    from test_hub.api.app import create_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    app = create_app(context.settings, context=context)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover - CI under heavy load
        pytest.fail("the test server did not start within 15 seconds")

    try:
        yield LiveServer(url=f"http://127.0.0.1:{port}", context=context)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class EventFactory:
    """Builds coherent event streams without the ceremony.

    Used by the storage, service and API tests so that a change to the event
    contract breaks one helper rather than forty test bodies.
    """

    def __init__(self, workspace_id: uuid.UUID, project_key: str = "demo-project") -> None:
        self.workspace_id = workspace_id
        self.project_key = project_key
        self.run_id = uuid.uuid4()
        self._sequence = 0
        self.start = utcnow() - timedelta(minutes=5)

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    def _make(self, event_type: EventType, data: Any, **kwargs: Any) -> Any:
        return make_event(
            event_type,
            run_id=self.run_id,
            workspace_id=self.workspace_id,
            sequence=self._next(),
            source=kwargs.pop("source", "urn:test-hub:tests"),
            data=data,
            **kwargs,
        )

    def queued(self, **overrides: Any) -> Any:
        payload = {
            "project_key": self.project_key,
            "name": "test run",
            "team": "quality",
            "component": "core",
            "test_type": TestType.UNIT,
            "context": RunContext(framework="pytest", language="python", environment="ci"),
        }
        payload.update(overrides)
        return self._make(EventType.RUN_QUEUED, RunQueuedData(**payload))

    def started(self) -> Any:
        return self._make(EventType.RUN_STARTED, RunStartedData(started_at=self.start))

    def suite(self, path: str = "tests/test_core.py") -> Any:
        return self._make(
            EventType.SUITE_STARTED,
            SuiteStartedData(
                suite_id=derive_suite_id(project=self.project_key, suite_path=path),
                name=path.rsplit("/", 1)[-1],
                path=path,
                started_at=self.start,
            ),
        )

    def test(
        self,
        name: str,
        status: TestStatus = TestStatus.PASSED,
        *,
        suite_path: str = "tests/test_core.py",
        attempt: int = 1,
        duration_ms: int = 100,
        failure: FailureDetail | None = None,
        **extra: Any,
    ) -> Any:
        return self._make(
            EventType.TEST_FINISHED,
            TestFinishedData(
                test_id=test_identity(
                    project=self.project_key, suite_path=suite_path, test_name=name
                ),
                suite_id=derive_suite_id(project=self.project_key, suite_path=suite_path),
                name=name,
                suite_path=suite_path,
                attempt_number=attempt,
                status=status,
                original_status=status.value,
                finished_at=self.start + timedelta(milliseconds=duration_ms),
                duration_ms=duration_ms,
                failure=failure,
                **extra,
            ),
        )

    def finished(self, **overrides: Any) -> Any:
        payload: dict[str, Any] = {"finished_at": utcnow()}
        payload.update(overrides)
        return self._make(EventType.RUN_FINISHED, RunFinishedData(**payload))

    def simple_run(self) -> list[Any]:
        """queued → started → suite → 3 passes, 1 failure, 1 skip → finished."""
        return [
            self.queued(),
            self.started(),
            self.suite(),
            self.test("test_one"),
            self.test("test_two"),
            self.test("test_three"),
            self.test(
                "test_four",
                TestStatus.FAILED,
                failure=FailureDetail(type="AssertionError", message="1 != 2"),
            ),
            self.test("test_five", TestStatus.SKIPPED),
            self.finished(),
        ]


@pytest.fixture
def events(context: Any) -> EventFactory:
    return EventFactory(context.workspace_id)
