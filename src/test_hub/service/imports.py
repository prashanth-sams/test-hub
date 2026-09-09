"""Importing a report file — the path shared by the CLI, the REST API and the SDK.

``test-hub ingest results.xml``, ``POST /api/v1/imports`` and
``test-hub publish`` all end up here, and all of them get the same
:class:`~test_hub.domain.models.ImportResult` back. That is not a coincidence,
it is the point: a format that works locally works over HTTP, and a warning you
see in the terminal is the same warning the API returns.

The sequence is always the same:

1. Read the upload into a bounded, seekable buffer — bounded because
   auto-detection needs to rewind, and a stream we cannot rewind would force us
   to hold the whole file in memory anyway. The limit is
   ``Settings.max_report_bytes`` and it is enforced while reading, not after.
2. Pick an adapter — ``--format`` if the caller named one, otherwise scored
   detection across every installed adapter.
3. Parse to domain events. The adapter never touches the database.
4. Apply the events in one transaction, idempotently.
5. Optionally keep the original file as a ``raw_report`` artifact, so a parser
   bug can be re-run against the exact bytes that caused it.
6. Publish what was applied to the live bus.
"""

from __future__ import annotations

import io
import uuid
import zlib
from dataclasses import dataclass
from typing import IO, Any

from test_hub.adapters import select_adapter
from test_hub.adapters.base import ParseContext
from test_hub.domain.events import DomainEvent
from test_hub.domain.identity import content_digest
from test_hub.domain.models import (
    Artifact,
    ArtifactKind,
    ImportResult,
    RunContext,
    RunCounts,
    TestType,
    TriggerType,
    utcnow,
)
from test_hub.errors import AdapterError, PayloadTooLargeError, TestHubError
from test_hub.logging import get_logger
from test_hub.service.runtime import AppContext

__all__ = ["ImportRequest", "ImportService", "decompress_bounded", "read_bounded"]

logger = get_logger(__name__)

_CHUNK = 256 * 1024

#: gzip's magic number. CI systems gzip their artifacts routinely, so a
#: `results.xml.gz` arriving at the import endpoint is ordinary rather than
#: exotic.
_GZIP_MAGIC = b"\x1f\x8b"


def read_bounded(stream: IO[bytes], max_bytes: int, *, what: str = "report") -> io.BytesIO:
    """Read at most ``max_bytes`` into memory, refusing anything larger.

    Refusing *while* reading matters: checking a Content-Length header is
    advisory, and reading first and measuring afterwards is exactly the denial
    of service the limit exists to prevent.
    """
    buffer = io.BytesIO()
    total = 0
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"{what} exceeds the {max_bytes} byte limit "
                f"(TEST_HUB_MAX_REPORT_BYTES); split the upload or raise the limit",
                detail={"limit_bytes": max_bytes},
            )
        buffer.write(chunk)
    buffer.seek(0)
    return buffer


def decompress_bounded(buffer: io.BytesIO, max_bytes: int) -> io.BytesIO:
    """Transparently gunzip an upload, refusing one that expands too far.

    A gzip stream can be a decompression bomb: a few kilobytes on the wire that
    become gigabytes in memory, which is why the ceiling
    (``TEST_HUB_MAX_DECOMPRESSED_BYTES``) is enforced *during* the expansion and
    not after it. Anything that is not gzip is returned untouched.

    Only gzip is handled, deliberately. A zip or a tar is an archive of many
    files, and "which of these is the report?" is a question with no good
    default; those are rejected as unknown formats instead of guessed at.
    """
    head = buffer.read(2)
    buffer.seek(0)
    if head != _GZIP_MAGIC:
        return buffer

    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    out = io.BytesIO()
    total = 0
    try:
        while True:
            chunk = buffer.read(_CHUNK)
            if not chunk:
                break
            expanded = decompressor.decompress(chunk, max_bytes - total + 1)
            total += len(expanded)
            if total > max_bytes:
                raise PayloadTooLargeError(
                    f"the gzipped report expands past the {max_bytes} byte limit "
                    f"(TEST_HUB_MAX_DECOMPRESSED_BYTES); it may be a decompression bomb",
                    detail={"limit_bytes": max_bytes},
                )
            out.write(expanded)
        tail = decompressor.flush()
        total += len(tail)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"the gzipped report expands past the {max_bytes} byte limit "
                f"(TEST_HUB_MAX_DECOMPRESSED_BYTES); it may be a decompression bomb",
                detail={"limit_bytes": max_bytes},
            )
        out.write(tail)
    except zlib.error as exc:
        raise AdapterError(f"this file begins as gzip but does not decompress: {exc}") from exc

    # zlib does not treat a truncated stream as an error — it returns whatever it
    # managed to inflate and stops. Left unchecked, half a report would reach the
    # adapter and fail as an unrecognised format, sending whoever uploaded it
    # looking in entirely the wrong place.
    if not decompressor.eof:
        raise AdapterError(
            "this gzip stream ends early — the upload was truncated or the file is corrupt"
        )

    logger.debug("gunzipped %s bytes into %s", buffer.getbuffer().nbytes, total)
    out.seek(0)
    buffer.seek(0)
    return out


@dataclass(slots=True)
class ImportRequest:
    """Everything a caller can say about a report beyond its bytes."""

    project_key: str
    run_id: uuid.UUID | None = None
    name: str | None = None
    team: str | None = None
    component: str | None = None
    test_type: TestType = TestType.OTHER
    trigger: TriggerType = TriggerType.UNKNOWN
    context: RunContext | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    explicit_format: str | None = None
    filename: str | None = None
    content_type: str | None = None
    source: str = "urn:test-hub:import"
    idempotency_key: str | None = None
    store_raw: bool | None = None


class ImportService:
    """Turns an uploaded report into a stored run."""

    def __init__(self, context: AppContext) -> None:
        self._context = context
        self._settings = context.settings

    def import_stream(self, stream: IO[bytes], request: ImportRequest) -> ImportResult:
        """Import one report. The only public entry point of this module."""
        started = utcnow()
        uploaded = read_bounded(stream, self._settings.max_report_bytes)
        raw_size = uploaded.getbuffer().nbytes
        if raw_size == 0:
            raise AdapterError("the uploaded report is empty")

        # Adapters see plain bytes; whether the upload arrived compressed is this
        # layer's problem, not theirs.
        buffer = decompress_bounded(uploaded, self._settings.max_decompressed_bytes)
        if buffer is not uploaded:
            logger.debug("import %s arrived gzipped", request.filename)

        adapter, confidence = select_adapter(
            buffer,
            filename=request.filename,
            content_type=request.content_type,
            explicit_format=request.explicit_format,
        )
        buffer.seek(0)

        run_id = request.run_id or uuid.uuid4()
        parse_context = ParseContext(
            workspace_id=self._context.workspace_id,
            project_key=request.project_key,
            run_id=run_id,
            name=request.name,
            team=request.team,
            component=request.component,
            test_type=request.test_type,
            trigger=request.trigger,
            context=request.context or RunContext(),
            tags=list(request.tags or []),
            metadata=dict(request.metadata or {}),
            source=request.source,
            filename=request.filename,
            max_text_bytes=self._settings.max_text_field_bytes,
        )

        try:
            events: list[DomainEvent] = list(adapter.parse(buffer, parse_context))
        except TestHubError:
            raise
        except Exception as exc:  # an adapter bug must not read as a server crash
            logger.exception("adapter %s failed to parse %s", adapter.name, request.filename)
            raise AdapterError(
                f"the {adapter.name} adapter could not parse this file: {exc}",
                detail={"adapter": adapter.name, "filename": request.filename},
            ) from exc

        if not events:
            raise AdapterError(
                f"the {adapter.name} adapter parsed the file but found no test results in it",
                detail={"adapter": adapter.name},
            )

        # The adapter is free to pick its own run id (a canonical-JSON report may
        # carry one), so take the id the events actually agreed on rather than the
        # one we suggested.
        run_id = events[0].run_id

        with self._context.uow_factory() as uow:
            result = uow.events.apply_events(events, idempotency_key=request.idempotency_key)
            warnings = parse_context.warnings.to_list()
            uow.runs.annotate_run(
                run_id,
                source_adapter=adapter.name,
                warnings=warnings,
            )
            self._maybe_store_raw(uow, run_id, buffer, request, raw_size)
            uow.commit()

        self._context.bus.publish_nowait(self._context.workspace_id, result.emitted)

        with self._context.uow_factory(read_only=True) as uow:
            run = uow.runs.get_run(run_id)

        duration_ms = int((utcnow() - started).total_seconds() * 1000)
        logger.info(
            "imported %s with %s: %s events applied, %s ignored, run %s",
            request.filename or "<stream>",
            adapter.name,
            result.applied,
            result.ignored,
            run_id,
        )
        return ImportResult(
            run_id=run_id,
            project_key=request.project_key,
            adapter=adapter.name,
            format=adapter.name,
            detection_confidence=round(confidence, 3),
            created=result.created_run,
            counts=run.counts if run else RunCounts(),
            suites_imported=result.suites_touched,
            tests_imported=result.tests_touched,
            attempts_imported=result.attempts_created,
            events_applied=result.applied,
            events_ignored=result.ignored,
            warnings=warnings,
            duration_ms=duration_ms,
        )

    def _maybe_store_raw(
        self,
        uow: Any,
        run_id: uuid.UUID,
        buffer: io.BytesIO,
        request: ImportRequest,
        size: int,
    ) -> Artifact | None:
        """Keep the original upload, unless the operator turned that off.

        Content-addressed, so re-importing the same file a hundred times from a
        hundred shards costs one copy on disk.
        """
        store_raw = request.store_raw
        if store_raw is None:
            store_raw = self._settings.store_raw_reports
        if not store_raw:
            return None

        buffer.seek(0)
        digest = content_digest(buffer.getvalue())
        buffer.seek(0)
        stored = uow.artifacts.put(buffer, max_bytes=self._settings.max_report_bytes)
        buffer.seek(0)
        artifact = Artifact(
            id=uuid.uuid4(),
            run_id=run_id,
            kind=ArtifactKind.RAW_REPORT,
            name=request.filename or "report",
            content_type=request.content_type or "application/octet-stream",
            size_bytes=size,
            sha256=stored.sha256 if stored else digest,
            metadata={"imported_at": utcnow().isoformat()},
        )
        recorded: Artifact = uow.artifacts.record(artifact)
        return recorded
