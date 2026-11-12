"""A write that cannot get the database answers 503, and the retry succeeds.

Under a burst of publishers SQLite's single write lock can be held longer than a
writer will wait. That used to escape as an unhandled exception — a 500 or a
dropped connection, a traceback in the log — for a request that had written
nothing and would have succeeded a moment later.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import make_url

from test_hub.config import Settings


@pytest.fixture
def impatient(tmp_path: Path, settings: Settings) -> Iterator[Any]:
    """A server whose writers wait 50 ms, and a handle on its SQLite file."""
    if settings.database_url is not None:
        pytest.skip("the write lock under test is SQLite's")
    from fastapi.testclient import TestClient

    from test_hub.api.app import create_app
    from test_hub.service.runtime import create_context

    patient = settings.model_copy(update={"sqlite_busy_timeout_ms": 50})
    context = create_context(patient, configure_logs=False)
    try:
        with TestClient(create_app(patient, context=context)) as client:
            yield client, make_url(patient.resolved_database_url).database
    finally:
        context.close()


def test_a_writer_that_cannot_get_the_lock_is_told_to_retry(impatient: Any) -> None:
    client, path = impatient
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        # Another writer — a `test-hub ingest` in a second process, say —
        # holding the lock for longer than this server's writers will wait.
        holder.execute("BEGIN IMMEDIATE")
        response = client.post("/api/v1/runs", json={"project_key": "busy"})
        assert response.status_code == 503
        assert response.json()["error"] == "storage_busy"
        assert int(response.headers["retry-after"]) > 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    # Nothing was written, and the same request now succeeds.
    assert client.post("/api/v1/runs", json={"project_key": "busy"}).status_code == 201
