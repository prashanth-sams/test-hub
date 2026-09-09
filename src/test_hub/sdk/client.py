"""HTTP client for publishing to a Test Hub server.

Everything it does is something ``curl`` could do — that is the constraint the
API was designed under, and this module is held to it. If a capability appears
here that cannot be expressed as a plain HTTP request, the API is wrong.

Retries are built in for the failures that are worth retrying (connection
errors, 429, 5xx) and deliberately absent for the ones that are not (4xx: a
malformed request will be just as malformed the second time). Retried requests
carry the same ``X-Idempotency-Key``, so a timeout that actually succeeded on the
server does not produce a duplicate run.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import IO, Any

import httpx

from test_hub.domain.events import (
    DomainEvent,
    EventType,
    TestFinishedData,
    make_event,
)
from test_hub.domain.identity import test_identity
from test_hub.domain.models import (
    Artifact,
    ArtifactKind,
    FailureDetail,
    ImportResult,
    RunContext,
    TestRun,
    TestType,
    TriggerType,
    utcnow,
)
from test_hub.domain.status import RunStatus, TestStatus, normalize_status
from test_hub.version import __version__

__all__ = ["TestHubClient", "TestHubClientError"]

_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_DEFAULT_RETRIES = 3


class TestHubClientError(RuntimeError):
    """A request to the server failed.

    Carries the server's structured error when there was one, so a caller can
    branch on ``error.code`` rather than parsing a message.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.detail = detail or {}


class TestHubClient:
    """A connection to a Test Hub server."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        token: str | None = None,
        timeout: float = 60.0,
        verify: bool = True,
        retries: int = _DEFAULT_RETRIES,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = max(0, retries)
        headers = {"user-agent": f"test-hub-sdk/{__version__}", "accept": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=self.base_url, timeout=timeout, verify=verify, headers=headers
        )
        self._owns_client = client is None
        self._sequences: dict[uuid.UUID, int] = {}
        self._workspace_id: uuid.UUID | None = None

    # ------------------------------------------------------------ plumbing

    def __enter__(self) -> TestHubClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a request, retrying only what is safe to retry."""
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._client.request(
                    method, path, json=json, data=data, files=files, headers=headers
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(_backoff(attempt))
                continue

            if response.status_code in _RETRY_STATUSES and attempt < self.retries:
                time.sleep(_backoff(attempt, response))
                continue
            return _unwrap(response)

        raise TestHubClientError(f"could not reach {self.base_url}: {last_error}") from last_error

    # ------------------------------------------------------------- imports

    def publish_report(
        self,
        path: str | Path | IO[bytes],
        *,
        project_key: str,
        run_id: uuid.UUID | None = None,
        name: str | None = None,
        team: str | None = None,
        component: str | None = None,
        test_type: TestType | str = TestType.OTHER,
        trigger: TriggerType | str = TriggerType.UNKNOWN,
        context: RunContext | None = None,
        tags: list[str] | None = None,
        explicit_format: str | None = None,
        filename: str | None = None,
        idempotency_key: str | None = None,
    ) -> ImportResult:
        """Upload a report file and get back the parsed result."""
        context = context or RunContext()
        form: dict[str, Any] = {
            "project": project_key,
            "type": str(TestType(test_type) if isinstance(test_type, str) else test_type),
            "trigger": str(TriggerType(trigger) if isinstance(trigger, str) else trigger),
        }
        optional = {
            "team": team,
            "component": component,
            "name": name,
            "environment": context.environment,
            "branch": context.branch,
            "commit": context.commit_sha,
            "framework": context.framework,
            "language": context.language,
            "pipeline_id": context.pipeline_id,
            "pipeline_url": str(context.pipeline_url) if context.pipeline_url else None,
            "ci_provider": context.ci_provider,
            "tags": ",".join(tags) if tags else None,
            "run_id": str(run_id) if run_id else None,
            "format": explicit_format,
        }
        form.update({key: value for key, value in optional.items() if value})

        headers = {"x-idempotency-key": idempotency_key} if idempotency_key else None

        if isinstance(path, (str, Path)):
            file_path = Path(path)
            with file_path.open("rb") as handle:
                payload = self._request(
                    "POST",
                    "/api/v1/imports",
                    data=form,
                    files={"file": (filename or file_path.name, handle, _guess_type(file_path))},
                    headers=headers,
                )
        else:
            payload = self._request(
                "POST",
                "/api/v1/imports",
                data=form,
                files={"file": (filename or "report", path, "application/octet-stream")},
                headers=headers,
            )
        return ImportResult.model_validate(payload)

    # ----------------------------------------------------------- live runs

    def open_run(
        self,
        *,
        project_key: str,
        run_id: uuid.UUID | None = None,
        name: str | None = None,
        team: str | None = None,
        component: str | None = None,
        test_type: TestType | str = TestType.OTHER,
        trigger: TriggerType | str = TriggerType.UNKNOWN,
        context: RunContext | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_tests: int | None = None,
    ) -> TestRun:
        """Open a run to publish results into."""
        body = {
            "project_key": project_key,
            "run_id": str(run_id) if run_id else None,
            "name": name,
            "team": team,
            "component": component,
            "test_type": str(TestType(test_type) if isinstance(test_type, str) else test_type),
            "trigger": str(TriggerType(trigger) if isinstance(trigger, str) else trigger),
            "context": (context or RunContext()).model_dump(mode="json", exclude_none=True),
            "tags": tags or [],
            "metadata": metadata or {},
            "expected_tests": expected_tests,
        }
        payload = self._request(
            "POST", "/api/v1/runs", json={k: v for k, v in body.items() if v is not None}
        )
        run = TestRun.model_validate(payload)
        self._sequences[run.id] = 2
        return run

    def send_events(
        self,
        run_id: uuid.UUID,
        events: list[DomainEvent],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Post a batch of already-built events."""
        headers = {"x-idempotency-key": idempotency_key} if idempotency_key else None
        return self._request(  # type: ignore[no-any-return]
            "POST",
            f"/api/v1/runs/{run_id}/events",
            json={"events": [event.model_dump(mode="json", by_alias=True) for event in events]},
            headers=headers,
        )

    def finish_test(
        self,
        run_id: uuid.UUID,
        *,
        suite: str,
        name: str,
        status: TestStatus | str,
        class_name: str | None = None,
        params: str | None = None,
        duration_ms: int | None = None,
        attempt_number: int = 1,
        failure: FailureDetail | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        logs: str | None = None,
        skip_reason: str | None = None,
        worker: str | None = None,
        project_key: str | None = None,
        suite_uuid: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Report one finished test attempt.

        The convenience method a framework plugin actually wants. Retries are
        expressed by calling it again with ``attempt_number`` incremented — never
        by re-reporting attempt 1 with a different outcome.
        """
        from test_hub.domain.identity import suite_id as derive_suite_id

        normalized = status if isinstance(status, TestStatus) else normalize_status(str(status))
        project = project_key or "unknown"
        event = make_event(
            EventType.TEST_FINISHED,
            run_id=run_id,
            workspace_id=self.workspace_id(),
            sequence=self._next_sequence(run_id),
            source="urn:test-hub:sdk",
            data=TestFinishedData(
                test_id=test_identity(
                    project=project,
                    suite_path=suite,
                    test_name=name,
                    class_name=class_name,
                    params=params,
                ),
                suite_id=suite_uuid or derive_suite_id(project=project, suite_path=suite),
                name=name,
                class_name=class_name,
                suite_path=suite,
                params=params,
                attempt_number=attempt_number,
                status=normalized,
                original_status=str(status),
                finished_at=utcnow(),
                duration_ms=duration_ms,
                failure=failure,
                stdout=stdout,
                stderr=stderr,
                logs=logs,
                skip_reason=skip_reason,
                worker=worker,
            ),
        )
        return self.send_events(run_id, [event], idempotency_key=idempotency_key)

    def close_run(
        self,
        run_id: uuid.UUID,
        *,
        status: RunStatus | str | None = None,
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TestRun:
        """Close a run. The final status is folded server-side from the results."""
        body: dict[str, Any] = {"finished_at": utcnow().isoformat()}
        if status is not None:
            body["status"] = str(RunStatus(status) if isinstance(status, str) else status)
        if duration_ms is not None:
            body["duration_ms"] = duration_ms
        if metadata:
            body["metadata"] = metadata
        payload = self._request("POST", f"/api/v1/runs/{run_id}/finish", json=body)
        self._sequences.pop(run_id, None)
        return TestRun.model_validate(payload)

    def upload_artifact(
        self,
        run_id: uuid.UUID,
        path: str | Path,
        *,
        kind: ArtifactKind | str = ArtifactKind.OTHER,
        test_id: uuid.UUID | None = None,
        name: str | None = None,
    ) -> Artifact:
        """Attach a file to a run."""
        file_path = Path(path)
        form: dict[str, Any] = {"kind": str(ArtifactKind(kind) if isinstance(kind, str) else kind)}
        if test_id:
            form["test_id"] = str(test_id)
        if name:
            form["name"] = name
        with file_path.open("rb") as handle:
            payload = self._request(
                "POST",
                f"/api/v1/runs/{run_id}/artifacts",
                data=form,
                files={"file": (file_path.name, handle, _guess_type(file_path))},
            )
        return Artifact.model_validate(payload["artifact"])

    # -------------------------------------------------------------- reads

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")  # type: ignore[no-any-return]

    def overview(self, **params: Any) -> dict[str, Any]:
        query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
        return self._request(  # type: ignore[no-any-return]
            "GET", f"/api/v1/overview{'?' + query if query else ''}"
        )

    def get_run(self, run_id: uuid.UUID) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/runs/{run_id}")  # type: ignore[no-any-return]

    def _next_sequence(self, run_id: uuid.UUID) -> int:
        """Client-side sequence allocation for a run this client opened."""
        current = self._sequences.get(run_id, 1)
        self._sequences[run_id] = current + 1
        return current

    def workspace_id(self) -> uuid.UUID:
        """The server's workspace id, fetched once and cached.

        Events carry the workspace they belong to, and the server rejects a
        mismatch rather than silently filing results under the wrong tenant. A
        client cannot invent this value, so it asks — ``/healthz`` returns it and
        is unauthenticated, which keeps this cheap even before a token is set.
        """
        if self._workspace_id is None:
            payload = self.health()
            raw = payload.get("workspace_id")
            if not raw:
                raise TestHubClientError(
                    f"{self.base_url} did not report a workspace id; is it a Test Hub server?"
                )
            self._workspace_id = uuid.UUID(str(raw))
        return self._workspace_id


def _backoff(attempt: int, response: httpx.Response | None = None) -> float:
    """Exponential backoff, honouring ``Retry-After`` when the server sent one."""
    if response is not None:
        header: str | None = response.headers.get("retry-after")
        if header:
            try:
                seconds = float(header)
            except (TypeError, ValueError):
                seconds = None
            if seconds is not None:
                return min(30.0, seconds)
    return min(8.0, 0.5 * (2.0**attempt))


def _guess_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".xml": "application/xml",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".txt": "text/plain",
        ".log": "text/plain",
        ".zip": "application/zip",
        ".html": "text/html",
    }.get(suffix, "application/octet-stream")


def _unwrap(response: httpx.Response) -> Any:
    """Turn a response into data, or into a :class:`TestHubClientError`."""
    if response.is_success:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - a proxy returning HTML
            raise TestHubClientError(
                f"expected JSON from {response.url} but got {response.headers.get('content-type')}"
            ) from exc

    code: str | None = None
    detail: dict[str, Any] = {}
    message = f"{response.status_code} {response.reason_phrase}"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = payload.get("error")
        message = payload.get("message", message)
        detail = payload.get("detail", {}) or {}
    raise TestHubClientError(message, status_code=response.status_code, code=code, detail=detail)
