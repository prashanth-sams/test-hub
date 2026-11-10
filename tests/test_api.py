"""The REST API: contract, authentication, errors and idempotency."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from test_hub.api.app import create_app
from test_hub.api.security import generate_token, hash_token
from test_hub.config import Settings
from tests.paths import junit


def upload(client: Any, name: str = "simple.xml", **form: Any) -> Any:
    form.setdefault("project", "payments-api")
    with junit(name).open("rb") as handle:
        return client.post(
            "/api/v1/imports",
            files={"file": (name, handle, "application/xml")},
            data=form,
        )


# ---------------------------------------------------------------- health


def test_healthz_reports_version_schema_and_database(client: Any) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["schema_version"] == 1
    assert uuid.UUID(body["workspace_id"])


def test_openapi_is_published_and_versioned(client: Any) -> None:
    spec = client.get("/api/openapi.json").json()
    assert spec["info"]["title"] == "Test Hub"
    paths = spec["paths"]
    for required in (
        "/api/v1/imports",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}/events",
        "/api/v1/runs/{run_id}/finish",
        "/api/v1/runs/{run_id}/artifacts",
        "/api/v1/overview",
        "/api/v1/projects",
        "/api/v1/runs/{run_id}",
        "/api/v1/tests/{test_id}",
        "/api/v1/events/stream",
        "/healthz",
    ):
        assert required in paths, f"{required} is missing from the OpenAPI document"


# ---------------------------------------------------------------- imports


def test_importing_a_report_returns_the_documented_shape(client: Any) -> None:
    response = upload(client, team="payments", type="component", environment="staging")
    assert response.status_code == 201
    body = response.json()
    assert uuid.UUID(body["run_id"])
    assert body["adapter"] == "junit-xml"
    assert body["detection_confidence"] > 0
    assert body["counts"]["total"] == 5
    assert body["warnings"] is not None
    assert body["schema_version"] == 1


def test_the_import_response_round_trips_into_the_domain_model(client: Any) -> None:
    """A client that reads a response and validates it must not be rejected."""
    from test_hub.domain.models import ImportResult

    body = upload(client).json()
    assert ImportResult.model_validate(body).counts.pass_rate == body["counts"]["pass_rate"]


def test_an_unknown_format_returns_415_listing_the_installed_adapters(client: Any) -> None:
    response = client.post(
        "/api/v1/imports",
        files={"file": ("notes.txt", b"not a test report", "text/plain")},
        data={"project": "p"},
    )
    assert response.status_code == 415
    body = response.json()
    assert body["error"] == "unknown_format"
    assert {row["name"] for row in body["detail"]["installed_adapters"]} >= {"junit-xml"}


def test_malformed_xml_returns_422_not_500(client: Any) -> None:
    assert upload(client, "malformed.xml").status_code == 422


def test_an_entity_attack_returns_422(client: Any) -> None:
    assert upload(client, "billion-laughs.xml").status_code == 422


def test_every_error_has_the_same_shape(client: Any) -> None:
    responses = [
        client.get(f"/api/v1/runs/{uuid.uuid4()}"),
        client.post(
            "/api/v1/imports", files={"file": ("a.txt", b"x", "text/plain")}, data={"project": "p"}
        ),
        client.post("/api/v1/runs", json={"nonsense": True}),
    ]
    for response in responses:
        body = response.json()
        assert set(body) >= {"error", "message"}, body
        assert isinstance(body["error"], str)


def test_an_import_is_idempotent_on_a_supplied_run_id(client: Any) -> None:
    run_id = str(uuid.uuid4())
    first = upload(client, run_id=run_id).json()
    second = upload(client, run_id=run_id).json()
    assert first["run_id"] == second["run_id"] == run_id
    assert second["events_applied"] == 0
    assert second["events_ignored"] > 0
    assert second["counts"]["total"] == first["counts"]["total"]


# ------------------------------------------------------------ publishing


def test_the_live_publishing_lifecycle(client: Any) -> None:
    created = client.post(
        "/api/v1/runs",
        json={"project_key": "storefront-web", "test_type": "e2e", "team": "growth"},
    )
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == "running"
    assert run["is_live"] is True

    workspace = client.get("/healthz").json()["workspace_id"]
    event = {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:test",
        "type": "test.finished",
        "workspaceid": workspace,
        "runid": run["id"],
        "sequence": 10,
        "data": {
            "suite_id": str(uuid.uuid4()),
            "name": "adds an item to the basket",
            "suite_path": "e2e/basket.spec.ts",
            "status": "passed",
            "duration_ms": 812,
        },
    }
    posted = client.post(f"/api/v1/runs/{run['id']}/events", json={"events": [event]})
    assert posted.status_code == 200
    assert posted.json()["applied"] == 1
    assert posted.json()["counts"]["total"] == 1

    # The same event again changes nothing.
    again = client.post(f"/api/v1/runs/{run['id']}/events", json={"events": [event]})
    assert again.json() == {**posted.json(), "applied": 0, "ignored": 1}

    finished = client.post(f"/api/v1/runs/{run['id']}/finish", json={})
    assert finished.status_code == 200
    assert finished.json()["status"] == "passed"
    assert finished.json()["is_live"] is False


def test_creating_a_run_twice_with_one_id_returns_the_same_run(client: Any) -> None:
    run_id = str(uuid.uuid4())
    body = {"project_key": "p", "run_id": run_id}
    first = client.post("/api/v1/runs", json=body).json()
    second = client.post("/api/v1/runs", json=body).json()
    assert first["id"] == second["id"] == run_id


def test_events_for_an_unknown_run_are_404(client: Any) -> None:
    workspace = client.get("/healthz").json()["workspace_id"]
    response = client.post(
        f"/api/v1/runs/{uuid.uuid4()}/events",
        json={
            "events": [
                {
                    "specversion": "1.0",
                    "id": str(uuid.uuid4()),
                    "source": "urn:test",
                    "type": "run.started",
                    "workspaceid": workspace,
                    "runid": str(uuid.uuid4()),
                    "sequence": 1,
                    "data": {"started_at": "2026-01-01T00:00:00Z"},
                }
            ]
        },
    )
    assert response.status_code in (404, 422)


def test_an_event_for_a_different_run_than_the_url_is_rejected(client: Any) -> None:
    workspace = client.get("/healthz").json()["workspace_id"]
    run = client.post("/api/v1/runs", json={"project_key": "p"}).json()
    response = client.post(
        f"/api/v1/runs/{run['id']}/events",
        json={
            "events": [
                {
                    "specversion": "1.0",
                    "id": str(uuid.uuid4()),
                    "source": "urn:test",
                    "type": "run.started",
                    "workspaceid": workspace,
                    "runid": str(uuid.uuid4()),
                    "sequence": 1,
                    "data": {"started_at": "2026-01-01T00:00:00Z"},
                }
            ]
        },
    )
    assert response.status_code == 422


# ----------------------------------------------------------------- reads


def test_run_and_test_detail(client: Any) -> None:
    run_id = upload(client).json()["run_id"]
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["run"]["id"] == run_id
    assert detail["suites"]
    assert detail["tests"]

    test_id = detail["tests"][0]["id"]
    test_detail = client.get(f"/api/v1/tests/{test_id}").json()
    assert test_detail["test_id"] == test_id
    assert test_detail["history"]


def test_filters_are_applied(client: Any) -> None:
    upload(client, project="payments-api", team="payments")
    upload(client, project="ledger-worker", team="payments")

    assert client.get("/api/v1/runs?project=payments-api").json()["total"] == 1
    assert client.get("/api/v1/runs?team=payments").json()["total"] == 2
    assert client.get("/api/v1/runs?project=payments-api,ledger-worker").json()["total"] == 2
    assert client.get("/api/v1/runs?project=nonexistent").json()["total"] == 0


def test_runs_can_be_windowed_by_when_they_started(client: Any, context: Any) -> None:
    """`since` and `until` are what the overview's live timeline asks with: the
    runs that started inside its span, and nothing older."""
    from datetime import timedelta

    from test_hub.domain.models import utcnow
    from tests.conftest import EventFactory

    def run_started(hours_ago: float) -> str:
        run = EventFactory(context.workspace_id, project_key="windowed")
        run.start = utcnow() - timedelta(hours=hours_ago)
        with context.uow_factory() as uow:
            uow.events.apply_events([run.queued(), run.started(), run.suite(), run.test("t")])
            uow.commit()
        return str(run.run_id)

    old = run_started(3)
    recent = run_started(0.2)
    hour_ago = (utcnow() - timedelta(hours=1)).isoformat()

    since = client.get("/api/v1/runs", params={"since": hour_ago}).json()
    assert [run["id"] for run in since["runs"]] == [recent]
    assert since["total"] == 1

    until = client.get("/api/v1/runs", params={"until": hour_ago}).json()
    assert [run["id"] for run in until["runs"]] == [old]


def test_a_window_without_an_offset_is_refused(client: Any) -> None:
    """A naive timestamp would be read as UTC, which is hours from what a caller
    writing local time meant. Better a 422 than a quietly wrong window."""
    response = client.get("/api/v1/runs", params={"since": "2026-11-10T08:00:00"})
    assert response.status_code == 422
    assert client.get("/api/v1/runs", params={"since": "2026-11-10T08:00:00Z"}).status_code == 200
    assert (
        client.get("/api/v1/runs", params={"since": "2026-11-10T13:30:00+05:30"}).status_code == 200
    )


def test_facets_list_what_can_be_filtered_on(client: Any) -> None:
    upload(client, team="payments", environment="staging")
    facets = client.get("/api/v1/facets").json()
    assert "payments" in facets["teams"]
    assert "projects" in facets


def test_overview_separates_the_window_from_the_workspace(client: Any) -> None:
    upload(client)
    body = client.get("/api/v1/overview").json()
    assert body["total_runs_all_time"] >= 1
    assert isinstance(body["trend"], list)
    assert len(body["trend"]) <= 61, "the trend payload must stay bounded"


def test_artifact_download_is_always_an_attachment(client: Any) -> None:
    """Artifacts are attacker-supplied bytes on our origin; never render them inline."""
    run_id = client.post("/api/v1/runs", json={"project_key": "p"}).json()["id"]
    response = client.post(
        f"/api/v1/runs/{run_id}/artifacts",
        files={"file": ("shot.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        data={"kind": "screenshot"},
    )
    assert response.status_code == 201
    artifact = response.json()["artifact"]

    download = client.get(f"/api/v1/artifacts/{artifact['id']}")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"


def test_security_headers_are_present_on_every_response(client: Any) -> None:
    for path in ("/healthz", "/api/v1/overview", "/"):
        headers = client.get(path).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in headers["content-security-policy"]


def test_cors_is_off_by_default(client: Any) -> None:
    response = client.get("/api/v1/overview", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


# ------------------------------------------------------------------ auth


@pytest.fixture
def secured(tmp_path: Any) -> Any:
    from fastapi.testclient import TestClient

    from test_hub.service.runtime import create_context

    token = generate_token()
    settings = Settings(
        data_dir=tmp_path / "secure",
        api_token_hashes=[hash_token(token)],
        store_raw_reports=False,
    )
    context = create_context(settings, configure_logs=False)
    app = create_app(settings, context=context)
    with TestClient(app) as client:
        yield client, token
    context.close()


def test_writes_require_a_token_when_auth_is_on(secured: Any) -> None:
    client, token = secured
    assert client.post("/api/v1/runs", json={"project_key": "p"}).status_code == 401
    assert (
        client.post(
            "/api/v1/runs", json={"project_key": "p"}, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/runs", json={"project_key": "p"}, headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 201
    )


def test_an_x_api_key_header_is_accepted_because_ci_systems_are_awkward(secured: Any) -> None:
    client, token = secured
    assert (
        client.post(
            "/api/v1/runs", json={"project_key": "p"}, headers={"X-API-Key": token}
        ).status_code
        == 201
    )


def test_a_401_carries_www_authenticate(secured: Any) -> None:
    client, _ = secured
    response = client.post("/api/v1/runs", json={"project_key": "p"})
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"] == "unauthorized"


def test_reads_stay_open_by_default_even_with_auth_on(secured: Any) -> None:
    """The common deployment: anyone may look, only CI may write."""
    client, _ = secured
    assert client.get("/api/v1/overview").status_code == 200
    assert client.get("/healthz").status_code == 200
