"""``POST /api/v1/imports`` — upload a report file.

The endpoint every CI system can use without a client library:

.. code-block:: shell

    curl -X POST http://localhost:8000/api/v1/imports \\
      -H "Authorization: Bearer $TEST_HUB_TOKEN" \\
      -F "file=@results.xml" \\
      -F "project=payments-api" \\
      -F "team=payments" \\
      -F "type=component"

Multipart rather than a JSON body because that is what ``curl -F``, ``requests``,
``HttpClient`` and every CI plugin already know how to send, and because it lets
the file stream instead of being base64'd into a JSON string.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile

from test_hub.api.deps import Imports, require_write
from test_hub.api.schemas import AdapterInfo, AdapterListResponse
from test_hub.domain.models import ImportResult, RunContext, TestType, TriggerType
from test_hub.service.imports import ImportRequest

router = APIRouter(tags=["ingestion"])


@router.post(
    "/imports",
    response_model=ImportResult,
    status_code=201,
    summary="Import a test report file",
    dependencies=[Depends(require_write)],
    responses={
        413: {"description": "The file is larger than TEST_HUB_MAX_REPORT_BYTES."},
        415: {"description": "No installed adapter recognised the format."},
        422: {"description": "The format was recognised but the file could not be parsed."},
    },
)
def create_import(
    service: Imports,
    file: Annotated[UploadFile, File(description="The report file.")],
    project: Annotated[str, Form(description="Project key, e.g. payments-api.")],
    team: Annotated[str | None, Form()] = None,
    component: Annotated[str | None, Form()] = None,
    type: Annotated[TestType, Form(description="Test type.")] = TestType.OTHER,
    trigger: Annotated[TriggerType, Form()] = TriggerType.UNKNOWN,
    name: Annotated[str | None, Form()] = None,
    environment: Annotated[str | None, Form()] = None,
    branch: Annotated[str | None, Form()] = None,
    commit: Annotated[str | None, Form()] = None,
    framework: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    pipeline_id: Annotated[str | None, Form()] = None,
    pipeline_url: Annotated[str | None, Form()] = None,
    ci_provider: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form(description="Comma-separated tags.")] = None,
    run_id: Annotated[uuid.UUID | None, Form(description="Reuse an existing run id.")] = None,
    format: Annotated[str | None, Form(description="Force an adapter by name.")] = None,
    x_idempotency_key: Annotated[str | None, Header()] = None,
) -> ImportResult:
    request = ImportRequest(
        project_key=project,
        run_id=run_id,
        name=name,
        team=team,
        component=component,
        test_type=type,
        trigger=trigger,
        context=RunContext(
            environment=environment,
            branch=branch,
            commit_sha=commit,
            framework=framework,
            language=language,
            pipeline_id=pipeline_id,
            pipeline_url=pipeline_url,
            ci_provider=ci_provider,
        ),
        tags=[tag.strip() for tag in (tags or "").split(",") if tag.strip()],
        explicit_format=format,
        filename=file.filename,
        content_type=file.content_type,
        source="urn:test-hub:api-import",
        idempotency_key=x_idempotency_key,
    )
    return service.import_stream(file.file, request)


@router.get(
    "/adapters",
    response_model=AdapterListResponse,
    summary="List the installed report adapters",
)
def list_adapters() -> AdapterListResponse:
    """What this server can parse.

    Worth calling before an import fails: the same list appears in the body of a
    415, but a publisher can check it up front.
    """
    from test_hub.adapters import get_registry

    return AdapterListResponse(adapters=[AdapterInfo(**row) for row in get_registry().describe()])
