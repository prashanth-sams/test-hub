"""Test Hub's own JSON format — the reference publisher contract.

This adapter is the answer to "my framework has no JUnit writer, how do I get
results in?". It accepts two shapes, and both of them must say so out loud with
a ``kind`` discriminator. That requirement is the whole design: a format that
claimed "any JSON object with a ``tests`` key" would swallow coverage reports,
ESLint output and half of ``package.json``, and every one of those would arrive
as a silently wrong run.

Shape 1 — ``test-hub-report``
----------------------------

The friendly form, written by hand or by a twenty-line script::

    {
      "kind": "test-hub-report",
      "schema_version": 1,
      "run": {
        "id": "6c0e6b4e-...",              // optional; re-importing the same
                                           // id updates that run instead of
                                           // creating a second one
        "name": "nightly e2e",
        "team": "checkout",
        "component": "checkout-service",
        "test_type": "e2e",                // TestType; unknown values warn
        "trigger": "schedule",             // TriggerType
        "status": "failed",                // advisory only, the server folds
        "started_at": "2026-04-02T01:15:00Z",
        "finished_at": "2026-04-02T01:19:42Z",
        "tags": ["nightly"],
        "metadata": {"shard": "3/8"},
        "context": {
          "framework": "playwright", "framework_version": "1.44.0",
          "language": "javascript", "environment": "staging",
          "branch": "main", "commit_sha": "8f21c0d",
          "ci_provider": "github-actions",
          "pipeline_url": "https://ci.example.test/runs/91"
        }
      },
      "suites": [
        {"path": "specs/checkout", "name": "checkout", "duration_ms": 91000},
        {"path": "specs/checkout/payment", "name": "payment",
         "parent_path": "specs/checkout"}
      ],
      "tests": [
        {
          "name": "declines an expired card",
          "class_name": "CheckoutPayment",
          "suite_path": "specs/checkout/payment",
          "params": {"currency": "EUR"},   // dict, list or pre-rendered string
          "status": "failed",              // any word normalize_status knows
          "duration_ms": 812,
          "file": "specs/checkout/payment.spec.ts",
          "owner": "payments",
          "tags": ["smoke"], "markers": ["slow"],
          "worker": "worker-2",
          "failure": {"type": "AssertionError", "message": "...",
                      "stack_trace": "...", "expected": "402", "actual": "500"},
          "stdout": "...", "stderr": "...", "logs": "...",
          "skip_reason": null,
          "metadata": {"retry_policy": "2x"}
        }
      ]
    }

``suites`` is optional: any ``suite_path`` a test names is created implicitly.
Suite and test identities are *derived*, never carried — ``suite_id`` from
:func:`test_hub.domain.identity.suite_id` and the test identity from
:func:`test_hub.domain.identity.test_identity` — so a publisher in any language
that follows ``docs/identity`` gets the same UUIDs Test Hub does.

Retries, two ways
~~~~~~~~~~~~~~~~~

Either nest them::

    {"name": "flaky checkout", "suite_path": "specs/checkout",
     "attempts": [
       {"status": "failed", "duration_ms": 4100, "failure": {...}},
       {"status": "failed", "duration_ms": 3900, "failure": {...}},
       {"status": "passed", "duration_ms": 4300}
     ]}

or repeat the test with an increasing ``attempt_number``::

    {"name": "flaky checkout", "suite_path": "specs/checkout",
     "attempt_number": 1, "status": "failed"}
    {"name": "flaky checkout", "suite_path": "specs/checkout",
     "attempt_number": 2, "status": "passed"}

Both produce one test with two attempts, because entries are grouped by test
identity — the *last* attempt is the effective outcome and the disagreement
between attempts is what makes the test flaky. Attempt numbers are re-sequenced
densely from 1 and a warning says so if the file's own numbering was gappy.

The expansion is::

    run.queued -> run.started -> suite.started* -> test.finished* ->
    suite.finished* -> run.finished

in that block order — every suite is announced before any result arrives, which
is what a dashboard subscribing mid-import needs in order to draw the tree.

Shape 2 — ``test-hub-events``
-----------------------------

The raw form, for a publisher that already speaks the event contract::

    {"kind": "test-hub-events", "schema_version": 1,
     "events": [ {CloudEvents envelope}, ... ]}

Each event is validated as a :class:`~test_hub.domain.events.DomainEvent` and
passed through unchanged, **sequence numbers preserved** — they are the file's
statement about ordering and this adapter has no better information. Missing
``run_id``/``workspace_id``/``source`` are filled from the import request;
a ``workspace_id`` that disagrees with the importing workspace is overridden
(with a warning), because a file must never be able to write into another
tenant.

Versioning
----------

``schema_version`` is refused, not coerced, when this build does not speak it.
An older Test Hub reading a newer file cannot know which fields changed meaning,
and a wrong pass rate is worse than a failed import.

Idempotency
-----------

Event ids are UUIDv5 over ``(source, run_id, sequence, type)`` rather than
random, so importing the same file twice against the same ``run.id`` applies
zero new events instead of doubling the run.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import IO, Any

from pydantic import ValidationError as PydanticValidationError

from test_hub.adapters.base import SNIFF_BYTES, BaseAdapter, ParseContext, WarningCollector
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
from test_hub.domain.identity import (
    TEST_HUB_NAMESPACE,
    normalize_params,
    test_identity,
)
from test_hub.domain.identity import suite_id as suite_identity
from test_hub.domain.models import (
    FailureDetail,
    RunContext,
    TestType,
    TriggerType,
    utcnow,
)
from test_hub.domain.status import RunStatus, TestStatus, normalize_status
from test_hub.errors import AdapterError, ValidationError
from test_hub.version import SCHEMA_VERSION

__all__ = ["EVENTS_KIND", "REPORT_KIND", "CanonicalJsonAdapter"]

#: The two values ``kind`` may take.
REPORT_KIND = "test-hub-report"
EVENTS_KIND = "test-hub-events"

#: ``LongText``/``ShortText`` in :mod:`test_hub.domain.models`. Adapters must
#: clip to these or the model rejects the event they just built.
_LONG_TEXT_LIMIT = 64_000
_SHORT_TEXT_LIMIT = 512

#: ``Slug`` in :mod:`test_hub.domain.models`. The event payloads type these
#: fields as plain ``str``, but ``TestRun.team``/``component``/``tags`` and
#: ``TestCase.tags`` are ``Slug``s — so a value that sails through event
#: validation can still blow up when the server folds the event into a row.
#: Adapters have to be the ones that check.
_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\- ]*$")

_KIND_RE = re.compile(rb'"kind"\s*:\s*"(test-hub-report|test-hub-events)"')
_RUN_KEY_RE = re.compile(rb'"run"\s*:\s*\{')
_TESTS_KEY_RE = re.compile(rb'"tests"\s*:\s*\[')


def _clip(value: object, limit: int) -> tuple[str | None, bool]:
    """Coerce to text and cut it to ``limit`` characters.

    Returns the truncation flag as well, because a cut stack trace that does not
    say it was cut is how a debugging session gets wasted.
    """
    if value is None:
        return None, False
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _event_id(source: str, run_id: uuid.UUID, sequence: int, event_type: EventType) -> uuid.UUID:
    """Deterministic event id, so a re-import of the same file is a no-op."""
    return uuid.uuid5(TEST_HUB_NAMESPACE, f"{source}\x1f{run_id}\x1f{sequence}\x1f{event_type}")


def _as_mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _as_str_list(value: object, limit: int = 64) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, Sequence):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return []
    return [item.strip()[:_SHORT_TEXT_LIMIT] for item in candidates if item.strip()][:limit]


class CanonicalJsonAdapter(BaseAdapter):
    """Reads ``test-hub-report`` and ``test-hub-events`` JSON documents."""

    name = "testhub-json"
    display_name = "Test Hub JSON"
    extensions = (".json",)
    content_types = (
        "application/json",
        "text/json",
        "application/vnd.test-hub+json",
    )
    #: Above JUnit's: when a file somehow looks like both, ours is the more
    #: specific claim.
    priority = 100
    description = "Test Hub's own report and raw event JSON (kind: test-hub-report/-events)."

    # ------------------------------------------------------------- detect

    def detect(self, stream: IO[bytes], filename: str | None, content_type: str | None) -> float:
        """Score the input. Never claims plain JSON without a ``kind``."""
        hint = self.hint_score(filename, content_type)
        try:
            head = self.sniff(stream, SNIFF_BYTES)
        except Exception:  # pragma: no cover - unreadable stream
            return hint
        stripped = head.lstrip(b"\xef\xbb\xbf").lstrip()
        if not stripped:
            return hint
        if not stripped.startswith(b"{"):
            # A JSON array, or not JSON at all. Neither shape is ours.
            return 0.0
        if _KIND_RE.search(head):
            return 1.0
        try:
            document = json.loads(stripped.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError):
            # Truncated at SNIFF_BYTES, which is the normal case for a real
            # report: fall back to looking for the two keys textually.
            if _RUN_KEY_RE.search(head) and _TESTS_KEY_RE.search(head):
                return 0.6
            return hint
        if isinstance(document, dict):
            if document.get("kind") in (REPORT_KIND, EVENTS_KIND):
                return 1.0
            if isinstance(document.get("run"), dict) and isinstance(document.get("tests"), list):
                return 0.6
        return hint

    # -------------------------------------------------------------- parse

    def parse(self, stream: IO[bytes], context: ParseContext) -> Iterator[DomainEvent]:
        document = self._load(stream)
        self._check_version(document)
        kind = document.get("kind")
        if kind == REPORT_KIND:
            yield from _ReportExpander(context).expand(document)
        elif kind == EVENTS_KIND:
            yield from _EventPassthrough(context).expand(document)
        else:
            raise AdapterError(
                "this is JSON, but not a Test Hub document: the top-level object must carry "
                f'"kind": "{REPORT_KIND}" or "kind": "{EVENTS_KIND}". '
                f"Found kind={kind!r}.",
                detail={"kind": kind, "adapter": self.name},
            )

    @staticmethod
    def _load(stream: IO[bytes]) -> dict[str, Any]:
        raw = stream.read()
        if not raw:
            raise AdapterError("the document is empty")
        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise AdapterError(f"the document is not valid UTF-8: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"the document is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
            ) from exc
        if not isinstance(document, dict):
            raise AdapterError(
                'a Test Hub JSON document must be a JSON object with a "kind" key, '
                f"not a {type(document).__name__}"
            )
        return document

    @staticmethod
    def _check_version(document: Mapping[str, Any]) -> None:
        version = document.get("schema_version", SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValidationError(
                f"schema_version must be an integer, got {version!r}",
                detail={"schema_version": version},
            )
        if version != SCHEMA_VERSION:
            raise ValidationError(
                f"this build speaks Test Hub schema_version {SCHEMA_VERSION}, but the document "
                f"declares {version}. Upgrade Test Hub (a newer document) or re-export the "
                f"report with a publisher that targets version {SCHEMA_VERSION}.",
                detail={"document_schema_version": version, "supported": SCHEMA_VERSION},
            )


# --------------------------------------------------------------- shape 1


class _ReportExpander:
    """Turns one ``test-hub-report`` document into an ordered event stream.

    A class rather than a function because expansion needs a handful of pieces
    of per-parse state — the sequence counter lives on the context, but the
    "have I already warned about naive timestamps" flag and the derived run
    identity do not.
    """

    def __init__(self, context: ParseContext) -> None:
        self.context = context
        self.warnings = context.warnings
        self.project = context.project_key
        self.text_limit = min(context.max_text_bytes, _LONG_TEXT_LIMIT)
        self._warned: set[str] = set()
        self._context_cache: RunContext | None = None
        self.run_id = context.run_id
        self.source = context.source

    # ----------------------------------------------------------- helpers

    def warn_once(self, code: str, message: str, **detail: Any) -> None:
        if code in self._warned:
            return
        self._warned.add(code)
        self.warnings.add(code, message, **detail)

    def slug(self, value: object, field: str, location: str) -> str | None:
        """Keep a value only if it can survive being stored as a ``Slug``."""
        text, _ = _clip(value, 120)
        if text is None:
            return None
        if not _SLUG_RE.match(text):
            self.warnings.add(
                "invalid_slug",
                f"{field}={text!r} is not a usable key (letters, digits, dot, dash, "
                "underscore and space only); it was dropped",
                location=location,
            )
            return None
        return text

    def slugs(self, values: Sequence[str], field: str, location: str) -> list[str]:
        return [kept for kept in (self.slug(value, field, location) for value in values) if kept]

    def datetime_at(self, value: object, location: str) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                self.warnings.add(
                    "bad_timestamp",
                    f"could not read {text!r} as an ISO-8601 timestamp; ignoring it",
                    location=location,
                )
                return None
        if parsed.tzinfo is None:
            self.warn_once(
                "naive_timestamp",
                "the report contains timestamps with no UTC offset; they are read as UTC",
                location=location,
            )
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    def duration_at(self, block: Mapping[str, Any], location: str) -> int | None:
        """Milliseconds from ``duration_ms``, or seconds from ``duration``."""
        raw = block.get("duration_ms")
        scale = 1.0
        if raw is None:
            raw = block.get("duration")
            scale = 1000.0
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = float(raw) * scale
        except (TypeError, ValueError):
            self.warnings.add(
                "bad_duration", f"could not read {raw!r} as a duration", location=location
            )
            return None
        if value < 0:
            self.warnings.add(
                "bad_duration", f"negative duration {raw!r} clamped to 0", location=location
            )
            return 0
        return round(value)

    def status_at(self, raw: object, location: str) -> tuple[TestStatus, str | None]:
        original = None if raw is None else str(raw)[:_SHORT_TEXT_LIMIT]
        if original is None or not original.strip():
            self.warnings.add(
                "missing_status",
                "an attempt has no status; recording it as unknown",
                location=location,
            )
            return TestStatus.UNKNOWN, original
        status = normalize_status(original)
        if status is TestStatus.UNKNOWN:
            self.warnings.add(
                "unknown_status",
                f"status {original!r} is not a word Test Hub knows; recorded as unknown",
                location=location,
                status=original,
            )
        return status, original

    def failure_at(self, block: object, location: str) -> FailureDetail | None:
        if not isinstance(block, Mapping):
            return None
        type_text, _ = _clip(block.get("type"), _SHORT_TEXT_LIMIT)
        message, cut_message = _clip(block.get("message"), self.text_limit)
        stack, cut_stack = _clip(block.get("stack_trace") or block.get("stack"), self.text_limit)
        expected, cut_expected = _clip(block.get("expected"), self.text_limit)
        actual, cut_actual = _clip(block.get("actual"), self.text_limit)
        truncated = cut_message or cut_stack or cut_expected or cut_actual
        if truncated:
            self.warnings.add(
                "text_truncated",
                f"failure text was longer than {self.text_limit} characters and was cut",
                location=location,
            )
        if not any((type_text, message, stack, expected, actual)):
            return None
        return FailureDetail(
            type=type_text,
            message=message,
            stack_trace=stack,
            expected=expected,
            actual=actual,
            truncated=truncated or bool(block.get("truncated")),
        )

    def text_at(self, block: Mapping[str, Any], key: str, location: str) -> str | None:
        text, cut = _clip(block.get(key), self.text_limit)
        if cut:
            self.warnings.add(
                "text_truncated",
                f"{key} was longer than {self.text_limit} characters and was cut",
                location=location,
            )
        return text

    def emit(
        self,
        event_type: EventType,
        data: Any,
        *,
        subject: str | None = None,
        time: datetime | None = None,
    ) -> DomainEvent:
        sequence = self.context.next_sequence()
        return make_event(
            event_type,
            run_id=self.run_id,
            workspace_id=self.context.workspace_id,
            sequence=sequence,
            data=data,
            source=self.source,
            event_id=_event_id(self.source, self.run_id, sequence, event_type),
            time=time or utcnow(),
            subject=subject,
        )

    # ------------------------------------------------------------ expand

    def expand(self, document: Mapping[str, Any]) -> Iterator[DomainEvent]:
        run_block = document.get("run")
        if run_block is not None and not isinstance(run_block, Mapping):
            raise AdapterError('"run" must be a JSON object')
        run_block = _as_mapping(run_block)

        declared_id = _coerce_uuid(run_block.get("id"))
        if declared_id is not None:
            self.run_id = declared_id
        file_project = run_block.get("project_key")
        if isinstance(file_project, str) and file_project and file_project != self.project:
            self.warnings.info(
                "project_key_override",
                f"the file says project {file_project!r}; the import said "
                f"{self.project!r} and the import wins",
            )

        tests = document.get("tests")
        if tests is not None and not isinstance(tests, list):
            raise AdapterError('"tests" must be a JSON array')
        entries = _as_list(tests)

        suites = self._collect_suites(document, entries)
        groups = self._group_tests(entries, suites)

        started_at = self.datetime_at(run_block.get("started_at"), "run") or utcnow()
        finished_at = self.datetime_at(run_block.get("finished_at"), "run")
        run_duration = self.duration_at(run_block, "run")

        yield self.emit(
            EventType.RUN_QUEUED,
            self._run_queued(run_block, expected=len(groups)),
            subject=self.project,
            time=started_at,
        )
        yield self.emit(
            EventType.RUN_STARTED,
            RunStartedData(
                started_at=started_at,
                project_key=self.project,
                context=self._run_context(run_block),
            ),
            subject=self.project,
            time=started_at,
        )

        for suite in suites.values():
            yield self.emit(
                EventType.SUITE_STARTED,
                SuiteStartedData(
                    suite_id=suite.identity,
                    name=suite.name,
                    path=suite.path,
                    parent_id=suite.parent_id,
                    started_at=suite.started_at,
                    metadata=suite.metadata,
                ),
                subject=suite.path,
                time=suite.started_at or started_at,
            )

        for group in groups:
            yield from self._test_events(group)

        for suite in suites.values():
            yield self.emit(
                EventType.SUITE_FINISHED,
                SuiteFinishedData(
                    suite_id=suite.identity,
                    finished_at=suite.finished_at,
                    duration_ms=suite.duration_ms,
                ),
                subject=suite.path,
                time=suite.finished_at or started_at,
            )

        if run_duration is None and finished_at is not None:
            run_duration = max(0, round((finished_at - started_at).total_seconds() * 1000))
        if finished_at is None:
            finished_at = (
                started_at + timedelta(milliseconds=run_duration)
                if run_duration is not None
                else utcnow()
            )
        yield self.emit(
            EventType.RUN_FINISHED,
            RunFinishedData(
                finished_at=finished_at,
                status=self._run_status(run_block),
                duration_ms=run_duration,
                metadata={},
            ),
            subject=self.project,
            time=finished_at,
        )

    # ------------------------------------------------------------- run

    def _run_queued(self, run_block: Mapping[str, Any], *, expected: int) -> RunQueuedData:
        metadata = dict(self.context.metadata)
        metadata.update(_as_mapping(run_block.get("metadata")))
        tags = self.slugs(
            list(dict.fromkeys([*self.context.tags, *_as_str_list(run_block.get("tags"))])),
            "tag",
            "run",
        )
        return RunQueuedData(
            project_key=self.project,
            name=self.context.name or _clip(run_block.get("name"), _SHORT_TEXT_LIMIT)[0],
            team=self.context.team or self.slug(run_block.get("team"), "team", "run"),
            component=(
                self.context.component or self.slug(run_block.get("component"), "component", "run")
            ),
            test_type=self._enum(
                TestType, run_block.get("test_type"), self.context.test_type, TestType.OTHER, "run"
            ),
            trigger=self._enum(
                TriggerType,
                run_block.get("trigger"),
                self.context.trigger,
                TriggerType.UNKNOWN,
                "run",
            ),
            context=self._run_context(run_block),
            tags=tags,
            metadata=metadata,
            expected_tests=expected or None,
        )

    def _enum(
        self,
        enum_type: type[Any],
        raw: object,
        from_request: Any,
        default: Any,
        location: str,
    ) -> Any:
        """Request value wins; the file only fills in a field left at its default."""
        if from_request != default:
            return from_request
        if raw is None:
            return default
        text = str(raw).strip().lower().replace("-", "_")
        try:
            return enum_type(text)
        except ValueError:
            self.warnings.add(
                "unknown_enum",
                f"{enum_type.__name__} {raw!r} is not a value Test Hub knows; using {default}",
                location=location,
            )
            return default

    def _run_context(self, run_block: Mapping[str, Any]) -> RunContext:
        """Merge the file's context under the importer's — the human wins.

        Cached: ``run.queued`` and ``run.started`` both carry it, and building it
        twice would report every dropped field twice.
        """
        if self._context_cache is not None:
            return self._context_cache
        file_context = _as_mapping(run_block.get("context"))
        merged: dict[str, Any] = {}
        request = self.context.context
        for field in RunContext.model_fields:
            chosen = getattr(request, field, None)
            if chosen is None:
                chosen = file_context.get(field)
            if chosen is not None:
                merged[field] = chosen
        self._context_cache = _best_effort_context(merged, self.warnings, "run.context")
        return self._context_cache

    def _run_status(self, run_block: Mapping[str, Any]) -> RunStatus | None:
        raw = run_block.get("status")
        if raw is None:
            return None
        text = str(raw).strip().lower()
        try:
            return RunStatus(text)
        except ValueError:
            mapped = normalize_status(text)
            if mapped is TestStatus.CANCELLED:
                return RunStatus.CANCELLED
            self.warnings.info(
                "advisory_run_status",
                f"run status {raw!r} is not a RunStatus; the server will fold the attempts instead",
                location="run",
            )
            return None

    # ----------------------------------------------------------- suites

    def _collect_suites(
        self, document: Mapping[str, Any], entries: Sequence[Any]
    ) -> dict[str, _Suite]:
        suites: dict[str, _Suite] = {}
        declared = document.get("suites")
        if declared is not None and not isinstance(declared, list):
            raise AdapterError('"suites" must be a JSON array')
        for index, item in enumerate(_as_list(declared)):
            block = _as_mapping(item)
            location = f"suites[{index}]"
            path = _clip(block.get("path") or block.get("name"), _SHORT_TEXT_LIMIT)[0]
            if not path:
                self.warnings.add(
                    "missing_suite_path",
                    "a declared suite has neither path nor name",
                    location=location,
                )
                continue
            parent_path = block.get("parent_path") or block.get("parent")
            suites[path] = _Suite(
                path=path,
                name=_clip(block.get("name") or path, _SHORT_TEXT_LIMIT)[0] or path,
                identity=suite_identity(project=self.project, suite_path=path),
                parent_id=(
                    suite_identity(project=self.project, suite_path=str(parent_path))
                    if parent_path
                    else None
                ),
                started_at=self.datetime_at(block.get("started_at"), location),
                finished_at=self.datetime_at(block.get("finished_at"), location),
                duration_ms=self.duration_at(block, location),
                metadata=_as_mapping(block.get("metadata")),
            )

        for index, item in enumerate(entries):
            block = _as_mapping(item)
            path = self._suite_path(block, f"tests[{index}]", warn=False)
            if path not in suites:
                suites[path] = _Suite(
                    path=path,
                    name=path.rsplit("/", 1)[-1] or path,
                    identity=suite_identity(project=self.project, suite_path=path),
                )
        return suites

    def _suite_path(self, block: Mapping[str, Any], location: str, *, warn: bool = True) -> str:
        raw = block.get("suite_path") or block.get("suite")
        path = _clip(raw, _SHORT_TEXT_LIMIT)[0]
        if path:
            return path
        fallback = _clip(block.get("class_name") or block.get("classname"), _SHORT_TEXT_LIMIT)[0]
        if warn:
            self.warnings.add(
                "missing_suite_path",
                f"a test has no suite_path; grouping it under {fallback or 'tests'!r}",
                location=location,
            )
        return fallback or "tests"

    # ------------------------------------------------------------ tests

    def _group_tests(
        self, entries: Sequence[Any], suites: Mapping[str, _Suite]
    ) -> list[_TestGroup]:
        """Fold entries into one group per test identity, ordered by first sight."""
        groups: dict[tuple[str, str, str, str], _TestGroup] = {}
        order: list[tuple[str, str, str, str]] = []

        for index, item in enumerate(entries):
            if not isinstance(item, Mapping):
                self.warnings.add(
                    "bad_test_entry",
                    f"tests[{index}] is a {type(item).__name__}, not an object; skipped",
                    location=f"tests[{index}]",
                )
                continue
            location = f"tests[{index}]"
            name = _clip(item.get("name") or item.get("test_name"), _SHORT_TEXT_LIMIT)[0]
            if not name:
                self.warnings.add(
                    "missing_test_name",
                    "a test entry has no name; skipped",
                    location=location,
                )
                continue
            class_name = _clip(item.get("class_name") or item.get("classname"), _SHORT_TEXT_LIMIT)[
                0
            ]
            suite_path = self._suite_path(item, location)
            raw_params = item.get("params")
            params = normalize_params(raw_params) if raw_params is not None else ""
            key = (suite_path, class_name or "", name, params)

            group = groups.get(key)
            if group is None:
                group = _TestGroup(
                    name=name,
                    class_name=class_name,
                    suite_path=suite_path,
                    params=params or None,
                    identity=test_identity(
                        project=self.project,
                        suite_path=suite_path,
                        test_name=name,
                        class_name=class_name,
                        params=raw_params,
                    ),
                    full_name=_clip(item.get("full_name"), _SHORT_TEXT_LIMIT)[0],
                    file=_clip(item.get("file"), _SHORT_TEXT_LIMIT)[0],
                    owner=_clip(item.get("owner"), _SHORT_TEXT_LIMIT)[0],
                    tags=self.slugs(_as_str_list(item.get("tags")), "tag", location),
                    markers=_as_str_list(item.get("markers")),
                    suite=suites[suite_path],
                )
                groups[key] = group
                order.append(key)

            nested = item.get("attempts")
            if isinstance(nested, list) and nested:
                for offset, raw_attempt in enumerate(nested):
                    attempt = _as_mapping(raw_attempt)
                    number = _coerce_int(attempt.get("attempt_number"), offset + 1)
                    group.attempts.append((number, attempt, f"{location}.attempts[{offset}]"))
                declared_status = item.get("status")
                last_status = _as_mapping(nested[-1]).get("status")
                if declared_status is not None and last_status != declared_status:
                    self.warnings.info(
                        "status_disagrees",
                        f"the entry's status {declared_status!r} differs from its last attempt "
                        f"{last_status!r}; the last attempt wins",
                        location=location,
                    )
            else:
                number = _coerce_int(item.get("attempt_number"), len(group.attempts) + 1)
                group.attempts.append((number, dict(item), location))

        return [groups[key] for key in order]

    def _test_events(self, group: _TestGroup) -> Iterator[DomainEvent]:
        ordered = sorted(enumerate(group.attempts), key=lambda pair: (pair[1][0], pair[0]))
        numbers = [attempt[0] for _, attempt in ordered]
        if numbers != list(range(1, len(numbers) + 1)):
            self.warnings.add(
                "attempt_renumbered",
                f"attempt numbers {numbers} for {group.name!r} were not 1..n; "
                "they were re-sequenced in the order given",
                location=group.suite_path,
            )
        suite = group.suite
        for position, (_, (_, attempt, location)) in enumerate(ordered, start=1):
            status, original = self.status_at(attempt.get("status"), location)
            started = self.datetime_at(attempt.get("started_at"), location)
            finished = self.datetime_at(attempt.get("finished_at"), location)
            duration = self.duration_at(attempt, location)
            if duration is None and started and finished:
                duration = max(0, round((finished - started).total_seconds() * 1000))
            yield self.emit(
                EventType.TEST_FINISHED,
                TestFinishedData(
                    test_id=group.identity,
                    suite_id=suite.identity,
                    name=group.name,
                    class_name=group.class_name,
                    suite_path=group.suite_path,
                    params=group.params,
                    full_name=group.full_name,
                    file=group.file,
                    owner=group.owner,
                    tags=group.tags,
                    markers=group.markers,
                    attempt_number=position,
                    status=status,
                    original_status=original,
                    started_at=started,
                    finished_at=finished,
                    duration_ms=duration,
                    failure=self.failure_at(attempt.get("failure"), location),
                    stdout=self.text_at(attempt, "stdout", location),
                    stderr=self.text_at(attempt, "stderr", location),
                    logs=self.text_at(attempt, "logs", location),
                    skip_reason=_clip(attempt.get("skip_reason"), _SHORT_TEXT_LIMIT)[0],
                    worker=_clip(attempt.get("worker"), _SHORT_TEXT_LIMIT)[0],
                    metadata=_as_mapping(attempt.get("metadata")),
                ),
                subject=group.full_name or f"{group.suite_path}::{group.name}",
                time=finished or utcnow(),
            )


# --------------------------------------------------------------- shape 2


class _EventPassthrough:
    """Validates a raw ``test-hub-events`` document and hands it straight on."""

    def __init__(self, context: ParseContext) -> None:
        self.context = context
        self.warnings = context.warnings

    def expand(self, document: Mapping[str, Any]) -> Iterator[DomainEvent]:
        raw_events = document.get("events")
        if not isinstance(raw_events, list):
            raise AdapterError('a "test-hub-events" document must carry an "events" array')
        if not raw_events:
            raise AdapterError('the "events" array is empty')

        run_id: uuid.UUID | None = None
        overridden = 0
        for index, item in enumerate(raw_events):
            if not isinstance(item, Mapping):
                raise AdapterError(f"events[{index}] is not an object")
            payload = dict(item)
            payload.pop("workspaceid", None)
            payload["workspace_id"] = str(self.context.workspace_id)
            declared_workspace = item.get("workspace_id") or item.get("workspaceid")
            if declared_workspace and str(declared_workspace) != str(self.context.workspace_id):
                overridden += 1
            if not (payload.get("run_id") or payload.get("runid")):
                payload["run_id"] = str(self.context.run_id)
            payload.setdefault("source", self.context.source)

            try:
                event = DomainEvent.model_validate(payload)
            except Exception as exc:
                raise AdapterError(f"events[{index}] is not a valid Test Hub event: {exc}") from exc

            if run_id is None:
                run_id = event.run_id
            elif event.run_id != run_id:
                raise AdapterError(
                    "every event in one document must belong to the same run; "
                    f"events[{index}] is for run {event.run_id} but the document started "
                    f"with run {run_id}"
                )
            yield event

        if overridden:
            self.warnings.add(
                "workspace_override",
                f"{overridden} event(s) named a different workspace; they were re-homed into "
                "the importing workspace",
            )


# ------------------------------------------------------------- structures


class _Suite:
    """A suite discovered in a report, declared or implied by a test."""

    __slots__ = (
        "duration_ms",
        "finished_at",
        "identity",
        "metadata",
        "name",
        "parent_id",
        "path",
        "started_at",
    )

    def __init__(
        self,
        *,
        path: str,
        name: str,
        identity: uuid.UUID,
        parent_id: uuid.UUID | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.name = name
        self.identity = identity
        self.parent_id = parent_id
        self.started_at = started_at
        self.finished_at = finished_at
        self.duration_ms = duration_ms
        self.metadata = metadata or {}


class _TestGroup:
    """Every attempt reported for one test identity, in one report."""

    __slots__ = (
        "attempts",
        "class_name",
        "file",
        "full_name",
        "identity",
        "markers",
        "name",
        "owner",
        "params",
        "suite",
        "suite_path",
        "tags",
    )

    def __init__(
        self,
        *,
        name: str,
        class_name: str | None,
        suite_path: str,
        params: str | None,
        identity: uuid.UUID,
        full_name: str | None,
        file: str | None,
        owner: str | None,
        tags: list[str],
        markers: list[str],
        suite: _Suite,
    ) -> None:
        self.name = name
        self.class_name = class_name
        self.suite_path = suite_path
        self.params = params
        self.identity = identity
        self.full_name = full_name
        self.file = file
        self.owner = owner
        self.tags = tags
        self.markers = markers
        self.suite = suite
        self.attempts: list[tuple[int, dict[str, Any], str]] = []


def _best_effort_context(
    merged: dict[str, Any], warnings: WarningCollector, location: str
) -> RunContext:
    """Build a :class:`RunContext`, dropping only the fields that will not validate.

    A single bad ``pipeline_url`` in a hand-written report should not cost the
    branch, the commit and the environment as well.
    """
    try:
        return RunContext(**merged)
    except PydanticValidationError as exc:
        bad = {str(error["loc"][0]) for error in exc.errors() if error.get("loc")}
        for field in sorted(bad):
            warnings.add(
                "invalid_run_context",
                f"run context field {field}={merged.get(field)!r} did not validate and was "
                "dropped; the rest of the context was kept",
                location=location,
            )
        remaining = {key: value for key, value in merged.items() if key not in bad}
        try:
            return RunContext(**remaining)
        except PydanticValidationError:  # pragma: no cover - belt and braces
            return RunContext()


def _coerce_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return uuid.UUID(value.strip())
        except ValueError:
            return None
    return None


def _coerce_int(value: object, default: int) -> int:
    """Read an attempt number, tolerating the string a hand-written file will use."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 1 else default
