"""The retention endpoints, and the scope that guards them.

Retention is the only destructive surface in the API and the only one behind a
third scope. Most of what is asserted here is *refusal*: that a publisher token
cannot delete, that a request without confirmation does nothing, and that a
public bind does not quietly become an open delete endpoint because the operator
turned auth off.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from test_hub.api.app import create_app
from test_hub.api.security import hash_token
from test_hub.config import Settings
from test_hub.domain.models import RunContext, utcnow
from test_hub.service.imports import ImportRequest, ImportService
from test_hub.service.runtime import create_context
from test_hub.storage.models import RunRow
from tests.paths import junit

RETENTION = "/api/v1/retention"
PREVIEW = "/api/v1/retention/preview"
PRUNE = "/api/v1/retention/prune"


def seed(context: Any, *, project: str = "p", days_old: int = 90) -> None:
    with junit("simple.xml").open("rb") as handle:
        result = ImportService(context).import_stream(
            handle,
            ImportRequest(project_key=project, filename="simple.xml", context=RunContext()),
        )
    moment = utcnow() - timedelta(days=days_old)
    with context.uow_factory() as uow:
        row = uow.session.get(RunRow, result.run_id)
        row.started_at = moment
        row.created_at = moment
        uow.commit()


def client_for(settings: Settings) -> Any:
    from fastapi.testclient import TestClient

    context = create_context(settings, configure_logs=False)
    app = create_app(settings, context=context)
    return TestClient(app), context


# ------------------------------------------------------------------- the shape


def test_retention_reports_the_policy(client: Any, context: Any) -> None:
    body = client.get(RETENTION).json()
    assert body["admin"] is True, "loopback with no auth is admin"
    assert body["min_runs"] == context.settings.retention_min_runs
    assert body["units"]["h"] == 1, "the UI needs hours"
    assert body["units"]["d"] == 24


def test_preview_counts_without_deleting(client: Any, context: Any) -> None:
    seed(context)
    body = client.post(PREVIEW, json={"older_than": "30d", "keep_min": 0}).json()
    assert body["dry_run"] is True
    assert body["runs"] == 1
    assert body["deleted"]["events"] > 0
    assert client.post(PREVIEW, json={"older_than": "30d", "keep_min": 0}).json()["runs"] == 1


def test_prune_deletes(client: Any, context: Any) -> None:
    seed(context)
    body = client.post(PRUNE, json={"older_than": "30d", "keep_min": 0, "confirm": True}).json()
    assert body["dry_run"] is False
    assert body["runs"] == 1
    assert client.post(PREVIEW, json={"older_than": "30d", "keep_min": 0}).json()["runs"] == 0


def test_hours_are_accepted(client: Any, context: Any) -> None:
    """The unit the dashboard offers for "clear the last hour"."""
    seed(context, days_old=0)
    assert client.post(PREVIEW, json={"older_than": "1h", "keep_min": 0}).json()["runs"] == 0
    seed(context, project="q", days_old=1)
    assert client.post(PREVIEW, json={"older_than": "1h", "keep_min": 0}).json()["runs"] == 1


# ------------------------------------------------------------------- refusals


def test_prune_without_confirmation_does_nothing(client: Any, context: Any) -> None:
    """An API that destroys a year of history on a request somebody sent by
    accident is an API with one field missing."""
    seed(context)
    response = client.post(PRUNE, json={"older_than": "30d", "keep_min": 0})
    assert response.status_code == 422
    assert client.post(PREVIEW, json={"older_than": "30d", "keep_min": 0}).json()["runs"] == 1


def test_an_unreadable_duration_is_the_callers_fault(client: Any) -> None:
    """422, not 500: the string came from the client, and answering 500 would
    tell every monitor that Test Hub is broken."""
    response = client.post(PREVIEW, json={"older_than": "3 months"})
    assert response.status_code == 422
    assert "90d" in response.json()["message"]


def test_no_cutoff_at_all_is_refused(client: Any) -> None:
    response = client.post(PREVIEW, json={})
    assert response.status_code == 422


# --------------------------------------------------------------------- scopes


def test_a_write_token_cannot_prune(tmp_path: Any) -> None:
    """The whole reason retention has its own scope: the write token is the one
    that lives in every repository's CI settings."""
    settings = Settings(
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        api_token_hashes=[hash_token("ths_publisher")],
        auth_enabled=True,
        store_raw_reports=False,
    )
    test_client, context = client_for(settings)
    try:
        with test_client as api:
            publisher = {"Authorization": "Bearer ths_publisher"}
            # It can publish...
            assert (
                api.post("/api/v1/runs", json={"project_key": "p"}, headers=publisher).status_code
                == 201
            )
            # ...and it cannot delete.
            assert (
                api.post(PREVIEW, json={"older_than": "30d"}, headers=publisher).status_code == 403
            )
            assert (
                api.post(
                    PRUNE, json={"older_than": "30d", "confirm": True}, headers=publisher
                ).status_code
                == 403
            )
    finally:
        context.close()


def test_an_admin_token_can_prune(tmp_path: Any) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        api_token_hashes=[hash_token("ths_publisher")],
        admin_token_hashes=[hash_token("ths_admin")],
        auth_enabled=True,
        store_raw_reports=False,
    )
    test_client, context = client_for(settings)
    try:
        seed(context)
        with test_client as api:
            admin = {"Authorization": "Bearer ths_admin"}
            assert api.get(RETENTION, headers=admin).json()["admin"] is True
            body = api.post(
                PRUNE, json={"older_than": "30d", "keep_min": 0, "confirm": True}, headers=admin
            )
            assert body.status_code == 200
            assert body.json()["runs"] == 1
    finally:
        context.close()


def test_the_dashboard_is_told_it_cannot_prune(tmp_path: Any) -> None:
    """`admin: false` is what makes the settings view render an explanation
    instead of a form that would 403 on submit."""
    settings = Settings(
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        api_token_hashes=[hash_token("ths_publisher")],
        auth_enabled=True,
        store_raw_reports=False,
    )
    test_client, context = client_for(settings)
    try:
        with test_client as api:
            body = api.get(RETENTION, headers={"Authorization": "Bearer ths_publisher"}).json()
            assert body["admin"] is False
    finally:
        context.close()


def test_an_open_public_bind_is_not_an_open_delete_endpoint(tmp_path: Any) -> None:
    """`--insecure-no-auth` on a public interface opens writes by the operator's
    explicit choice. Deleting is a second choice, and it has to be made
    separately — with an admin token."""
    settings = Settings(
        data_dir=tmp_path / "data",
        host="0.0.0.0",
        auth_enabled=False,
        insecure_no_auth=True,
        store_raw_reports=False,
    )
    test_client, context = client_for(settings)
    try:
        with test_client as api:
            assert api.get(RETENTION).json()["admin"] is False
            assert api.post(PREVIEW, json={"older_than": "30d"}).status_code == 403
            assert api.post(PRUNE, json={"older_than": "30d", "confirm": True}).status_code == 403
    finally:
        context.close()


@pytest.mark.parametrize("header", ["Authorization", "X-API-Key"])
def test_both_header_styles_carry_the_admin_token(tmp_path: Any, header: str) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        api_token_hashes=[hash_token("ths_publisher")],
        admin_token_hashes=[hash_token("ths_admin")],
        auth_enabled=True,
        store_raw_reports=False,
    )
    test_client, context = client_for(settings)
    try:
        with test_client as api:
            value = "Bearer ths_admin" if header == "Authorization" else "ths_admin"
            assert api.get(RETENTION, headers={header: value}).json()["admin"] is True
    finally:
        context.close()
