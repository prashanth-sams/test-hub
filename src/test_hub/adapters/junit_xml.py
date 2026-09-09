"""JUnit / xUnit XML — the format almost every test runner can already write.

"JUnit XML" is not a specification. It is a shape that Ant emitted in 2002 and
that thirty frameworks have been extending ever since, so this adapter is
deliberately forgiving about structure and deliberately strict about two things:
safety and truthfulness about what it could not represent.

Safety
------

Parsing is done exclusively through :mod:`defusedxml`, with DTDs, entity
declarations and external references all refused. A JUnit file is an *untrusted
upload*: the two classic attacks — a billion-laughs entity bomb that turns
200 bytes into 3 GB of RAM, and an XXE that makes the server read
``/etc/passwd`` and put it in a stack trace on a dashboard — are both entity
tricks, and both are refused at the parser rather than mitigated afterwards.
``tests/fixtures/junit/billion-laughs.xml`` and ``xxe.xml`` exist to keep that
true.

Memory
------

``iterparse`` with ``elem.clear()``, and each finished ``<testcase>`` is removed
from its parent as soon as it has been turned into events. A 200 MB Surefire
aggregate is streamed, never held: the resident set is one suite's worth of
elements plus the event currently being yielded.

What it handles
---------------

* ``<testsuites>`` roots and bare ``<testsuite>`` roots.
* ``<testsuite>`` nested inside ``<testsuite>`` — the child's ``parent_id``
  chains to the parent and its path is ``parent/child``.
* ``<testcase name classname time file line assertions status>``.
* ``<failure message type>``, ``<error>``, ``<skipped message>``, with the text
  body or a ``<stackTrace>`` child as the trace.
* ``<system-out>`` / ``<system-err>`` at case level (onto the attempt) and at
  suite level (see "Known compromises").
* ``<properties><property name value/></properties>`` at suite level, kept as
  suite metadata, with the well-known keys — branch, commit, environment, ci,
  url and friends — lifted onto the run's :class:`RunContext`.
* ``time`` in seconds as a float, with a comma decimal separator, with thousands
  separators, or missing entirely.
* An empty ``<testcase/>`` — that means passed.

Retries, and why they matter most
---------------------------------

Maven Surefire records a retried test as *one* ``<testcase>`` with the earlier
attempts nested inside it::

    <testcase name="chargesCard" classname="CheckoutIT" time="4.1">
      <flakyFailure message="timeout" type="AssertionError">stack...</flakyFailure>
      <flakyFailure message="timeout" type="AssertionError">stack...</flakyFailure>
    </testcase>

``flakyFailure``/``flakyError`` mean "this failed, then a rerun passed"; the
``<testcase>``'s own (absent) outcome is the pass. ``rerunFailure``/
``rerunError`` mean "this failed, the rerun failed too"; the ``<testcase>``'s own
``<failure>`` is the final word. Either way the nested elements are the
**earlier** attempts and the ``<testcase>``'s own outcome is the **last** one,
so the example above becomes three attempts — failed, failed, passed — and the
test is flaky. Collapsing that into a single "passed" is how a flaky suite stays
invisible for a year, which is why this is the one behaviour in this module
worth reading twice.

Counting
--------

The ``tests``/``failures``/``errors``/``skipped`` attributes a publisher writes
are advisory and frequently wrong — a partially-uploaded shard, a framework that
counts retries, a hand-edited file. Counts are folded from the cases that were
actually parsed, and a ``counts_mismatch`` warning records the disagreement
rather than hiding it. Likewise, any status word that
:func:`~test_hub.domain.status.normalize_status` cannot map becomes ``unknown``
and raises an ``unknown_status`` warning naming the word and where it was found.

Known compromises
-----------------

* JUnit has no per-test wall-clock times, only durations. Attempts therefore
  carry ``duration_ms`` and no ``started_at``/``finished_at``; the run's own
  window is taken from the earliest ``timestamp`` attribute plus the reported
  total, and a ``synthesized_timestamps`` info warning says so when there was no
  ``timestamp`` at all.
* Suite-level ``<system-out>``/``<system-err>`` arrive *after* ``suite.started``
  has been emitted, and ``SuiteFinishedData`` has nowhere to carry text, so that
  output is attached to ``run.finished``'s metadata with a
  ``suite_output_lifted`` info warning.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import IO, Any

from defusedxml.ElementTree import iterparse as defused_iterparse
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
from test_hub.domain.identity import TEST_HUB_NAMESPACE, test_identity
from test_hub.domain.identity import suite_id as suite_identity
from test_hub.domain.models import FailureDetail, RunContext, utcnow
from test_hub.domain.status import TestStatus, normalize_status, worst_status
from test_hub.errors import AdapterError
from test_hub.logging import get_logger

__all__ = ["JUnitXmlAdapter"]

logger = get_logger(__name__)

#: ``LongText`` / ``ShortText`` in :mod:`test_hub.domain.models`.
_LONG_TEXT_LIMIT = 64_000
_SHORT_TEXT_LIMIT = 512
#: Suite-level console output is metadata, not a payload; keep it small.
_SUITE_OUTPUT_LIMIT = 8192

_ROOT_TAGS = frozenset({"testsuites", "testsuite"})

#: Nested elements that describe an *earlier* attempt of the same test.
_RETRY_TAGS: dict[str, TestStatus] = {
    "flakyfailure": TestStatus.FAILED,
    "flakyerror": TestStatus.ERROR,
    "rerunfailure": TestStatus.FAILED,
    "rerunerror": TestStatus.ERROR,
}

#: Outcome children of a ``<testcase>``, in the order the XML allows them.
_OUTCOME_TAGS: dict[str, TestStatus] = {
    "failure": TestStatus.FAILED,
    "error": TestStatus.ERROR,
    "skipped": TestStatus.SKIPPED,
}

#: Ant and its descendants write ``status="run"`` on *every* executed test — it
#: describes execution, not outcome. Treating it as a status word would raise an
#: ``unknown_status`` warning on every single passing test in the file, so these
#: words mean "look at the children instead" and nothing else.
_EXECUTION_MARKERS = frozenset({"run", "completed", "complete", "executed", "finished"})

#: property name (lowercased, ``-`` folded to ``_``) -> ``RunContext`` field.
_RUN_PROPERTIES: dict[str, str] = {
    "branch": "branch",
    "git.branch": "branch",
    "git_branch": "branch",
    "build_branch": "branch",
    "ci_commit_branch": "branch",
    "commit": "commit_sha",
    "commit_sha": "commit_sha",
    "git.commit": "commit_sha",
    "git_commit": "commit_sha",
    "revision": "commit_sha",
    "sha": "commit_sha",
    "commit_message": "commit_message",
    "environment": "environment",
    "env": "environment",
    "test_environment": "environment",
    "ci": "ci_provider",
    "ci_provider": "ci_provider",
    "ci_platform": "ci_provider",
    "url": "pipeline_url",
    "build_url": "pipeline_url",
    "job_url": "pipeline_url",
    "pipeline_url": "pipeline_url",
    "pipeline_id": "pipeline_id",
    "build_id": "pipeline_id",
    "build_number": "pipeline_id",
    "hostname": "hostname",
    "host": "hostname",
    "os": "os",
    "platform": "os",
    "framework": "framework",
    "framework_version": "framework_version",
    "language": "language",
    "language_version": "language_version",
    "executor": "executor",
}

_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\- ]*$")
_XML_NAME_RE = re.compile(rb"([A-Za-z_][A-Za-z0-9_.:-]*)")


# --------------------------------------------------------------- utilities


def _localname(tag: object) -> str:
    """The tag without its namespace, lowercased. ``{ns}testSuite`` -> ``testsuite``."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _clip(value: object, limit: int) -> tuple[str | None, bool]:
    """Coerce to text and cut to ``limit`` characters, reporting whether it cut."""
    if value is None:
        return None, False
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text or None, False
    return text[:limit], True


def _event_id(source: str, run_id: uuid.UUID, sequence: int, event_type: EventType) -> uuid.UUID:
    """Deterministic event id, so re-importing the same file applies nothing new."""
    return uuid.uuid5(TEST_HUB_NAMESPACE, f"{source}\x1f{run_id}\x1f{sequence}\x1f{event_type}")


def _root_element_name(head: bytes) -> str | None:
    """The name of the first real element in ``head``, skipping prologue noise.

    Hand-rolled rather than regex-driven because a DOCTYPE with an internal
    subset (``<!DOCTYPE x [ ... ]>``) contains ``>`` characters, and every
    "strip the doctype" regex gets that wrong — which matters here, since a
    DOCTYPE is exactly what an entity bomb arrives wrapped in.
    """
    index = 0
    length = len(head)
    while index < length:
        start = head.find(b"<", index)
        if start < 0:
            return None
        if head.startswith(b"<?", start):
            end = head.find(b"?>", start)
            if end < 0:
                return None
            index = end + 2
        elif head.startswith(b"<!--", start):
            end = head.find(b"-->", start)
            if end < 0:
                return None
            index = end + 3
        elif head.startswith(b"<!", start):
            cursor = start + 2
            depth = 0
            while cursor < length:
                char = head[cursor : cursor + 1]
                if char == b"[":
                    depth += 1
                elif char == b"]":
                    depth -= 1
                elif char == b">" and depth <= 0:
                    break
                cursor += 1
            if cursor >= length:
                return None
            index = cursor + 1
        else:
            match = _XML_NAME_RE.match(head, start + 1)
            if match is None:
                return None
            return match.group(1).decode("ascii", "replace")
    return None


# ------------------------------------------------------------------ adapter


class JUnitXmlAdapter(BaseAdapter):
    """Streams JUnit/xUnit XML into Test Hub events."""

    name = "junit-xml"
    display_name = "JUnit XML"
    extensions = (".xml", ".junit", ".junit.xml")
    content_types = ("application/xml", "text/xml", "application/x-junit+xml")
    priority = 50
    description = "JUnit/xUnit XML from pytest, Surefire, Gradle, Jest, Playwright and friends."

    def detect(self, stream: IO[bytes], filename: str | None, content_type: str | None) -> float:
        try:
            head = self.sniff(stream, SNIFF_BYTES)
        except Exception:  # pragma: no cover - unreadable stream
            return self.hint_score(filename, content_type)
        root = _root_element_name(head)
        if root is None:
            # Not recognisably XML in the first 8 KiB: fall back to the name.
            return self.hint_score(filename, content_type)
        if root.rsplit(":", 1)[-1].lower() in _ROOT_TAGS:
            return 0.9
        return 0.0

    def parse(self, stream: IO[bytes], context: ParseContext) -> Iterator[DomainEvent]:
        return _JUnitParser(context).run(stream)


# ------------------------------------------------------------------- frames


class _SuiteFrame:
    """One open ``<testsuite>`` element and everything folded into it so far."""

    __slots__ = (
        "declared",
        "descendant_counts",
        "element",
        "identity",
        "metadata",
        "name",
        "own_counts",
        "parent_id",
        "path",
        "seconds",
        "started_at",
    )

    def __init__(
        self,
        *,
        element: Any,
        name: str,
        path: str,
        identity: uuid.UUID,
        parent_id: uuid.UUID | None,
        started_at: datetime | None,
        seconds: float | None,
        metadata: dict[str, Any],
        declared: dict[str, int],
    ) -> None:
        self.element = element
        self.name = name
        self.path = path
        self.identity = identity
        self.parent_id = parent_id
        self.started_at = started_at
        self.seconds = seconds
        self.metadata = metadata
        self.declared = declared
        self.own_counts: dict[str, int] = {}
        self.descendant_counts: dict[str, int] = {}

    def record(self, status: TestStatus) -> None:
        self.own_counts["tests"] = self.own_counts.get("tests", 0) + 1
        bucket = {
            TestStatus.FAILED: "failures",
            TestStatus.XPASSED: "failures",
            TestStatus.ERROR: "errors",
            TestStatus.SKIPPED: "skipped",
            TestStatus.XFAILED: "skipped",
        }.get(status)
        if bucket:
            self.own_counts[bucket] = self.own_counts.get(bucket, 0) + 1

    def rolled_up(self) -> dict[str, int]:
        merged = dict(self.own_counts)
        for key, value in self.descendant_counts.items():
            merged[key] = merged.get(key, 0) + value
        return merged


class _Attempt:
    """One execution of one test, before it becomes a ``test.finished`` event."""

    __slots__ = (
        "failure",
        "original_status",
        "seconds",
        "skip_reason",
        "status",
        "stderr",
        "stdout",
    )

    def __init__(
        self,
        *,
        status: TestStatus,
        original_status: str | None = None,
        seconds: float | None = None,
        failure: FailureDetail | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        skip_reason: str | None = None,
    ) -> None:
        self.status = status
        self.original_status = original_status
        self.seconds = seconds
        self.failure = failure
        self.stdout = stdout
        self.stderr = stderr
        self.skip_reason = skip_reason


# ------------------------------------------------------------------ parser


class _JUnitParser:
    """The streaming state machine. One instance per file, never reused."""

    def __init__(self, context: ParseContext) -> None:
        self.context = context
        self.warnings: WarningCollector = context.warnings
        self.project = context.project_key
        self.source = context.source
        self.run_id = context.run_id
        self.text_limit = min(context.max_text_bytes, _LONG_TEXT_LIMIT)

        self.stack: list[_SuiteFrame] = []
        self.root_element: Any = None
        self.root_attrib: dict[str, str] = {}
        self.in_testcase = 0
        self.header_flushed = False
        self.pending_suites: list[_SuiteFrame] = []
        self.used_paths: set[str] = set()
        self.implicit_suite: _SuiteFrame | None = None

        self.parse_started = utcnow()
        self.run_started_at: datetime | None = None
        self.total_seconds = 0.0
        self.case_seconds = 0.0
        self.suite_seconds = 0.0
        self.clock = self.parse_started
        self.context_fields: dict[str, Any] = {}
        self.run_metadata: dict[str, Any] = dict(context.metadata)
        self.saw_timestamp = False
        self._warned: set[str] = set()

    # ------------------------------------------------------------ warnings

    def warn_once(self, code: str, message: str, **detail: Any) -> None:
        if code in self._warned:
            return
        self._warned.add(code)
        self.warnings.add(code, message, **detail)

    def info_once(self, code: str, message: str, **detail: Any) -> None:
        if code in self._warned:
            return
        self._warned.add(code)
        self.warnings.info(code, message, **detail)

    # -------------------------------------------------------------- values

    def seconds(self, raw: object, location: str) -> float | None:
        """Parse a ``time`` attribute, in seconds.

        Accepts ``1.5``, ``1,5`` (decimal comma — de_DE locales write it that
        way), ``1,234.5`` (thousands separator) and whitespace-padded junk.
        """
        if raw is None:
            return None
        text = str(raw).strip().replace(" ", "").replace("\u00a0", "")
        if not text:
            return None
        if "," in text and "." in text:
            text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            self.warnings.add(
                "bad_duration",
                f"could not read time={raw!r} as a number of seconds; the duration is unknown",
                location=location,
            )
            return None
        if value != value or value in (float("inf"), float("-inf")):
            self.warnings.add(
                "bad_duration", f"time={raw!r} is not a finite duration", location=location
            )
            return None
        if value < 0:
            self.warnings.add(
                "bad_duration", f"negative time={raw!r} clamped to 0", location=location
            )
            return 0.0
        return value

    def timestamp(self, raw: object, location: str) -> datetime | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            self.warnings.add(
                "bad_timestamp",
                f"could not read timestamp={text!r} as ISO-8601; ignoring it",
                location=location,
            )
            return None
        if parsed.tzinfo is None:
            self.info_once(
                "naive_timestamp",
                "timestamps in this report carry no UTC offset; they are read as UTC",
                location=location,
            )
            parsed = parsed.replace(tzinfo=UTC)
        self.saw_timestamp = True
        return parsed

    def declared_counts(self, attrib: dict[str, str], location: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for key in ("tests", "failures", "errors", "skipped", "disabled"):
            raw = attrib.get(key)
            if raw is None:
                continue
            try:
                counts["skipped" if key == "disabled" else key] = int(str(raw).strip())
            except ValueError:
                self.warnings.add(
                    "bad_count",
                    f"{key}={raw!r} is not an integer; ignoring the publisher's own total",
                    location=location,
                )
        return counts

    # ---------------------------------------------------------------- emit

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
            time=time or self.clock,
            subject=subject,
        )

    # ----------------------------------------------------------------- run

    def run(self, stream: IO[bytes]) -> Iterator[DomainEvent]:
        """Stream the file, yielding events as the elements close."""
        try:
            iterator = defused_iterparse(
                stream,
                events=("start", "end"),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
        except Exception as exc:  # pragma: no cover - constructor rarely fails
            raise AdapterError(f"could not start the XML parser: {exc}") from exc

        try:
            yield from self._walk(iterator)
        except AdapterError:
            raise
        except Exception as exc:
            raise self._translate(exc) from exc

        if not self.header_flushed:
            yield from self._flush_header()
        yield from self._close_open_suites()
        yield self._run_finished()

    def _translate(self, exc: Exception) -> AdapterError:
        """Turn a parser failure into an error a user can act on."""
        name = type(exc).__name__
        if name in ("DTDForbidden", "EntitiesForbidden", "ExternalReferenceForbidden"):
            logger.warning("refused a JUnit upload containing %s", name)
            return AdapterError(
                "this XML declares a DTD or entities and was refused before it was expanded. "
                "Entity expansion is how an 'XML bomb' turns a 200-byte upload into gigabytes "
                "of memory, and how an XXE reads files off the server, so Test Hub never "
                "processes it. Re-export the report without a DOCTYPE.",
                detail={"reason": name},
            )
        return AdapterError(
            f"this file is not well-formed XML: {exc}",
            detail={"reason": name},
        )

    def _walk(self, iterator: Any) -> Iterator[DomainEvent]:
        for xml_event, element in iterator:
            tag = _localname(element.tag)
            if xml_event == "start":
                if tag == "testsuites" and self.root_element is None:
                    self.root_element = element
                    self.root_attrib = dict(element.attrib)
                elif tag == "testsuite":
                    if self.root_element is None:
                        self.root_element = element  # bare <testsuite> root
                    yield from self._open_suite(element)
                elif tag == "testcase":
                    self.in_testcase += 1
                    if not self.header_flushed:
                        yield from self._flush_header()
                continue

            # --- end events
            if tag == "testcase":
                self.in_testcase = max(0, self.in_testcase - 1)
                yield from self._finish_case(element)
                self._release(element)
            elif tag == "testsuite":
                yield from self._close_suite(element)
            elif self.in_testcase == 0 and tag == "properties":
                self._absorb_properties(element)
                self._release(element)
            elif self.in_testcase == 0 and tag in ("system-out", "system-err"):
                self._absorb_suite_output(tag, element)
                self._release(element)

    # -------------------------------------------------------------- header

    def _flush_header(self) -> Iterator[DomainEvent]:
        """Emit ``run.queued``/``run.started`` plus every buffered ``suite.started``.

        Deferred to the first ``<testcase>`` so that the first suite's
        ``<properties>`` block — where branch, commit and environment live — has
        already been read and can be lifted onto the run context.
        """
        self.header_flushed = True
        if self.run_started_at is None:
            self.run_started_at = self.timestamp(
                self.root_attrib.get("timestamp"), "testsuites"
            ) or (self.pending_suites[0].started_at if self.pending_suites else None)
        if self.run_started_at is None:
            self.run_started_at = self.parse_started
            self.info_once(
                "synthesized_timestamps",
                "the report carries no timestamp attribute, so the run's clock is the time "
                "it was imported; per-test durations are exact, the wall clock is not",
            )
        self.clock = self.run_started_at

        run_context = self._build_run_context()
        yield self.emit(
            EventType.RUN_QUEUED,
            RunQueuedData(
                project_key=self.project,
                name=self.context.name or self._run_name(),
                team=self.context.team,
                component=self.context.component,
                test_type=self.context.test_type,
                trigger=self.context.trigger,
                context=run_context,
                tags=list(self.context.tags),
                metadata=self.run_metadata,
                expected_tests=self._declared_total(),
            ),
            subject=self.project,
            time=self.run_started_at,
        )
        yield self.emit(
            EventType.RUN_STARTED,
            RunStartedData(
                started_at=self.run_started_at,
                project_key=self.project,
                context=run_context,
            ),
            subject=self.project,
            time=self.run_started_at,
        )
        for frame in self.pending_suites:
            yield self._suite_started(frame)
        self.pending_suites.clear()

    def _run_name(self) -> str | None:
        name = self.root_attrib.get("name")
        if not name and self.pending_suites:
            name = self.pending_suites[0].name
        clipped, _ = _clip(name, _SHORT_TEXT_LIMIT)
        return clipped

    def _declared_total(self) -> int | None:
        raw = self.root_attrib.get("tests")
        if raw is None:
            return None
        try:
            total = int(str(raw).strip())
        except ValueError:
            return None
        return total if total > 0 else None

    def _build_run_context(self) -> RunContext:
        """The importer's context wins; the file only fills in what it left blank."""
        merged: dict[str, Any] = {}
        request = self.context.context
        for field in RunContext.model_fields:
            chosen = getattr(request, field, None)
            if chosen is None:
                chosen = self.context_fields.get(field)
            if chosen is not None:
                merged[field] = chosen
        try:
            return RunContext(**merged)
        except PydanticValidationError as exc:
            bad = {str(error["loc"][0]) for error in exc.errors() if error.get("loc")}
            for field in sorted(bad):
                self.warnings.add(
                    "invalid_run_context",
                    f"run context field {field}={merged.get(field)!r} came from <properties> "
                    "and did not validate; it was dropped and the rest kept",
                    location="properties",
                )
            remaining = {key: value for key, value in merged.items() if key not in bad}
            try:
                return RunContext(**remaining)
            except PydanticValidationError:  # pragma: no cover - belt and braces
                return RunContext()

    # -------------------------------------------------------------- suites

    def _open_suite(self, element: Any) -> Iterator[DomainEvent]:
        attrib = {str(key): str(value) for key, value in element.attrib.items()}
        parent = self.stack[-1] if self.stack else None
        raw_name = attrib.get("name") or attrib.get("package") or "suite"
        name, _ = _clip(raw_name, _SHORT_TEXT_LIMIT)
        name = name or "suite"
        path = f"{parent.path}/{name}" if parent else name
        path = self._unique_path(path)
        location = f"testsuite[{path}]"

        metadata: dict[str, Any] = {}
        for key in ("package", "hostname", "id", "file", "timestamp", "group"):
            value = attrib.get(key)
            if value:
                metadata[key], _ = _clip(value, _SHORT_TEXT_LIMIT)
        if attrib.get("hostname") and "hostname" not in self.context_fields:
            self.context_fields["hostname"], _ = _clip(attrib["hostname"], _SHORT_TEXT_LIMIT)

        frame = _SuiteFrame(
            element=element,
            name=name,
            path=path,
            identity=suite_identity(project=self.project, suite_path=path),
            parent_id=parent.identity if parent else None,
            started_at=self.timestamp(attrib.get("timestamp"), location),
            seconds=self.seconds(attrib.get("time"), location),
            metadata=metadata,
            declared=self.declared_counts(attrib, location),
        )
        self.stack.append(frame)
        if self.run_started_at is None and frame.started_at is not None:
            self.run_started_at = frame.started_at
        if self.header_flushed:
            yield self._suite_started(frame)
        else:
            self.pending_suites.append(frame)

    def _unique_path(self, path: str) -> str:
        """Two suites with the same name are two suites, not one.

        Their identities are derived from the path, so an unqualified duplicate
        would silently merge two files' results into one suite.
        """
        if path not in self.used_paths:
            self.used_paths.add(path)
            return path
        for suffix in range(2, 1000):
            candidate = f"{path}#{suffix}"
            if candidate not in self.used_paths:
                self.used_paths.add(candidate)
                self.warn_once(
                    "duplicate_suite",
                    f"more than one suite is called {path!r}; the later ones are "
                    "distinguished with a #n suffix so their results do not merge",
                    location=path,
                )
                return candidate
        return path  # pragma: no cover - a thousand identically named suites

    def _suite_started(self, frame: _SuiteFrame) -> DomainEvent:
        return self.emit(
            EventType.SUITE_STARTED,
            SuiteStartedData(
                suite_id=frame.identity,
                name=frame.name,
                path=frame.path,
                parent_id=frame.parent_id,
                started_at=frame.started_at,
                metadata=frame.metadata,
            ),
            subject=frame.path,
            time=frame.started_at or self.clock,
        )

    def _close_suite(self, element: Any) -> Iterator[DomainEvent]:
        if not self.stack:
            return
        frame = self.stack.pop()
        if not self.header_flushed:
            yield from self._flush_header()
        self._check_counts(frame)
        if frame.seconds is not None:
            self.suite_seconds += frame.seconds
        duration_ms = round(frame.seconds * 1000) if frame.seconds is not None else None
        finished_at = (
            frame.started_at + timedelta(seconds=frame.seconds)
            if frame.started_at is not None and frame.seconds is not None
            else None
        )
        if not frame.own_counts and not frame.descendant_counts:
            self.warnings.info(
                "empty_suite",
                f"suite {frame.path!r} reported no test cases at all",
                location=frame.path,
            )
        yield self.emit(
            EventType.SUITE_FINISHED,
            SuiteFinishedData(
                suite_id=frame.identity,
                finished_at=finished_at,
                duration_ms=duration_ms,
            ),
            subject=frame.path,
            time=finished_at or self.clock,
        )
        if self.stack:
            rolled = frame.rolled_up()
            for key, value in rolled.items():
                self.stack[-1].descendant_counts[key] = (
                    self.stack[-1].descendant_counts.get(key, 0) + value
                )
        self._release(element)

    def _close_open_suites(self) -> Iterator[DomainEvent]:
        """A truncated file can leave suites open; close them rather than lose them."""
        while self.stack:
            frame = self.stack[-1]
            self.warn_once(
                "unclosed_suite",
                f"suite {frame.path!r} was never closed by the file; closing it",
                location=frame.path,
            )
            yield from self._close_suite(frame.element)
        if self.implicit_suite is not None:
            frame = self.implicit_suite
            self.implicit_suite = None
            yield self.emit(
                EventType.SUITE_FINISHED,
                SuiteFinishedData(suite_id=frame.identity, finished_at=None, duration_ms=None),
                subject=frame.path,
                time=self.clock,
            )

    def _check_counts(self, frame: _SuiteFrame) -> None:
        """Compare the publisher's own totals with what was actually parsed."""
        if not frame.declared:
            return
        own = frame.own_counts
        rolled = frame.rolled_up()
        keys = set(frame.declared)
        if all(frame.declared[key] == own.get(key, 0) for key in keys):
            return
        if all(frame.declared[key] == rolled.get(key, 0) for key in keys):
            return
        self.warnings.add(
            "counts_mismatch",
            f"suite {frame.path!r} declares "
            + ", ".join(f"{key}={frame.declared[key]}" for key in sorted(keys))
            + " but "
            + ", ".join(f"{key}={rolled.get(key, 0)}" for key in sorted(keys))
            + " were parsed; the parsed numbers are the ones Test Hub keeps",
            location=frame.path,
            declared=frame.declared,
            parsed={key: rolled.get(key, 0) for key in sorted(keys)},
        )

    # ---------------------------------------------------------- properties

    def _absorb_properties(self, element: Any) -> None:
        frame = self.stack[-1] if self.stack else None
        collected: dict[str, str] = {}
        for child in element:
            if _localname(child.tag) != "property":
                continue
            raw_name = child.attrib.get("name")
            if not raw_name:
                continue
            value = child.attrib.get("value")
            if value is None:
                value = (child.text or "").strip()
            clipped, _ = _clip(value, _SHORT_TEXT_LIMIT)
            collected[str(raw_name)] = clipped or ""
        if not collected:
            return
        if frame is not None:
            frame.metadata.setdefault("properties", {}).update(collected)
        else:
            self.run_metadata.setdefault("properties", {}).update(collected)
        self._lift_properties(collected, frame.path if frame else "testsuites")

    def _lift_properties(self, collected: dict[str, str], location: str) -> None:
        """Promote the well-known property names onto the run context."""
        for raw_name, value in collected.items():
            field = _RUN_PROPERTIES.get(raw_name.strip().lower().replace("-", "_"))
            if field is None or not value:
                continue
            if self.header_flushed:
                self.info_once(
                    "late_run_property",
                    f"property {raw_name!r} names run-level context but arrived after the run "
                    "header had been emitted; it is kept as suite metadata only",
                    location=location,
                )
                continue
            if field in self.context_fields:
                continue
            cleaned = self._validated_property(field, value, raw_name, location)
            if cleaned is not None:
                self.context_fields[field] = cleaned

    def _validated_property(
        self, field: str, value: str, raw_name: str, location: str
    ) -> str | None:
        if field == "environment" and not _SLUG_RE.match(value):
            self.warnings.add(
                "invalid_property",
                f"property {raw_name}={value!r} cannot be an environment name "
                "(letters, digits, dot, dash, underscore and space only); kept as metadata",
                location=location,
            )
            return None
        if field == "pipeline_url" and not value.lower().startswith(("http://", "https://")):
            self.warnings.add(
                "invalid_property",
                f"property {raw_name}={value!r} is not an http(s) URL; kept as metadata",
                location=location,
            )
            return None
        if field == "commit_sha":
            return value[:64]
        return value[:_SHORT_TEXT_LIMIT]

    def _absorb_suite_output(self, tag: str, element: Any) -> None:
        text = "".join(element.itertext()).strip()
        if not text:
            return
        key = "system_out" if tag == "system-out" else "system_err"
        if key in self.run_metadata:
            return
        clipped, truncated = _clip(text, _SUITE_OUTPUT_LIMIT)
        self.run_metadata[key] = clipped
        cut = f" (cut to {_SUITE_OUTPUT_LIMIT} characters)" if truncated else ""
        self.info_once(
            "suite_output_lifted",
            f"suite-level <{tag}> has nowhere to live on a suite event, so it is attached "
            f"to the run's metadata{cut}",
            location=self.stack[-1].path if self.stack else "testsuites",
        )

    # ------------------------------------------------------------- testcase

    def _finish_case(self, element: Any) -> Iterator[DomainEvent]:
        frame = self.stack[-1] if self.stack else self._ensure_implicit_suite()
        attrib = {str(key): str(value) for key, value in element.attrib.items()}
        raw_name = attrib.get("name") or "(unnamed test)"
        name, _ = _clip(raw_name, _SHORT_TEXT_LIMIT)
        name = name or "(unnamed test)"
        class_name, _ = _clip(attrib.get("classname") or attrib.get("class"), _SHORT_TEXT_LIMIT)
        location = f"{frame.path}/{class_name or ''}::{name}"

        attempts = self._collect_attempts(element, attrib, location)
        seconds = self.seconds(attrib.get("time"), location)
        if seconds is not None:
            self.case_seconds += seconds

        metadata: dict[str, Any] = {}
        for key in ("line", "assertions", "group", "owner", "retries"):
            if attrib.get(key):
                metadata[key] = attrib[key][:_SHORT_TEXT_LIMIT]
        case_properties = self._case_properties(element)
        if case_properties:
            metadata["properties"] = case_properties

        file_name, _ = _clip(attrib.get("file"), _SHORT_TEXT_LIMIT)
        identity = test_identity(
            project=self.project,
            suite_path=frame.path,
            test_name=name,
            class_name=class_name,
        )
        full_name = f"{class_name}.{name}" if class_name else name

        # The whole case's `time` belongs to the final attempt; earlier attempts
        # have no duration of their own in this format.
        for number, attempt in enumerate(attempts, start=1):
            is_last = number == len(attempts)
            duration = attempt.seconds
            if duration is None and is_last:
                duration = seconds
            duration_ms = round(duration * 1000) if duration is not None else None
            if duration_ms is not None:
                self.clock = self.clock + timedelta(milliseconds=duration_ms)
            frame_status = attempt.status
            if is_last:
                frame.record(frame_status)
            yield self.emit(
                EventType.TEST_FINISHED,
                TestFinishedData(
                    test_id=identity,
                    suite_id=frame.identity,
                    name=name,
                    class_name=class_name,
                    suite_path=frame.path,
                    params=None,
                    full_name=_clip(full_name, _SHORT_TEXT_LIMIT)[0],
                    file=file_name,
                    owner=None,
                    tags=[],
                    markers=[],
                    attempt_number=number,
                    status=attempt.status,
                    original_status=attempt.original_status,
                    started_at=None,
                    finished_at=None,
                    duration_ms=duration_ms,
                    failure=attempt.failure,
                    stdout=attempt.stdout,
                    stderr=attempt.stderr,
                    logs=None,
                    skip_reason=attempt.skip_reason,
                    worker=None,
                    metadata=metadata if is_last else {"attempt_source": "surefire-rerun"},
                ),
                subject=full_name[:_SHORT_TEXT_LIMIT],
            )

    def _ensure_implicit_suite(self) -> _SuiteFrame:
        """A ``<testcase>`` outside any ``<testsuite>`` still needs somewhere to live."""
        if self.implicit_suite is None:
            path = self._unique_path(self.root_attrib.get("name") or "(root)")
            self.implicit_suite = _SuiteFrame(
                element=None,
                name=path,
                path=path,
                identity=suite_identity(project=self.project, suite_path=path),
                parent_id=None,
                started_at=None,
                seconds=None,
                metadata={},
                declared={},
            )
            self.warn_once(
                "orphan_testcase",
                "the file has <testcase> elements outside any <testsuite>; they were grouped "
                f"into a synthetic suite called {path!r}",
                location=path,
            )
        return self.implicit_suite

    def _case_properties(self, element: Any) -> dict[str, str]:
        collected: dict[str, str] = {}
        for child in element:
            if _localname(child.tag) != "properties":
                continue
            for prop in child:
                if _localname(prop.tag) != "property":
                    continue
                key = prop.attrib.get("name")
                if not key:
                    continue
                value = prop.attrib.get("value")
                if value is None:
                    value = (prop.text or "").strip()
                collected[str(key)] = str(value)[:_SHORT_TEXT_LIMIT]
        return collected

    def _collect_attempts(
        self, element: Any, attrib: dict[str, str], location: str
    ) -> list[_Attempt]:
        """Earlier attempts from the rerun elements, then the case's own outcome."""
        attempts: list[_Attempt] = []
        outcomes: list[tuple[str, Any]] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        for child in element:
            tag = _localname(child.tag)
            if tag in _RETRY_TAGS:
                attempts.append(self._retry_attempt(tag, child, location))
            elif tag in _OUTCOME_TAGS:
                outcomes.append((tag, child))
            elif tag == "system-out":
                stdout_parts.append("".join(child.itertext()))
            elif tag == "system-err":
                stderr_parts.append("".join(child.itertext()))

        attempts.append(self._final_attempt(attrib, outcomes, location))
        final = attempts[-1]
        final.stdout = self._text("stdout", stdout_parts, location)
        final.stderr = self._text("stderr", stderr_parts, location)
        return attempts

    def _text(self, what: str, parts: list[str], location: str) -> str | None:
        if not parts:
            return None
        joined = "\n".join(part for part in parts if part.strip())
        clipped, truncated = _clip(joined, self.text_limit)
        if truncated:
            self.warn_once(
                "text_truncated",
                f"{what} longer than {self.text_limit} characters was cut; the rest is in the "
                "stored raw report",
                location=location,
            )
        return clipped

    def _retry_attempt(self, tag: str, child: Any, location: str) -> _Attempt:
        status = _RETRY_TAGS[tag]
        return _Attempt(
            status=status,
            original_status=tag,
            seconds=self.seconds(child.attrib.get("time"), location),
            failure=self._failure(child, location),
            stdout=self._text("stdout", [self._child_text(child, "system-out")], location),
            stderr=self._text("stderr", [self._child_text(child, "system-err")], location),
        )

    def _final_attempt(
        self, attrib: dict[str, str], outcomes: list[tuple[str, Any]], location: str
    ) -> _Attempt:
        raw_status = (attrib.get("status") or "").strip()
        if outcomes:
            statuses = [_OUTCOME_TAGS[tag] for tag, _ in outcomes]
            status = worst_status(statuses)
            if len({*statuses}) > 1:
                self.warnings.add(
                    "conflicting_outcome",
                    "this test carries "
                    + " and ".join(sorted({f"<{tag}>" for tag, _ in outcomes}))
                    + f"; the more severe one ({status.value}) is the outcome",
                    location=location,
                )
            chosen = next(child for tag, child in outcomes if _OUTCOME_TAGS[tag] is status)
            skip_reason = None
            failure = None
            original = raw_status or _localname(chosen.tag)
            if status is TestStatus.SKIPPED:
                skip_reason, _ = _clip(
                    chosen.attrib.get("message") or "".join(chosen.itertext()).strip(),
                    _SHORT_TEXT_LIMIT,
                )
                status, original = self._refine_skip(chosen, status, original)
            else:
                failure = self._failure(chosen, location)
            return _Attempt(
                status=status,
                original_status=original,
                failure=failure,
                skip_reason=skip_reason,
            )

        if not raw_status or raw_status.lower() in _EXECUTION_MARKERS:
            # An empty <testcase/> means the test ran and nothing went wrong.
            return _Attempt(status=TestStatus.PASSED, original_status=raw_status or None)

        status = normalize_status(raw_status)
        if status is TestStatus.UNKNOWN:
            self.warnings.add(
                "unknown_status",
                f"status={raw_status!r} is not a word Test Hub can map; the test is recorded "
                "as unknown rather than guessed at",
                location=location,
                status=raw_status,
            )
        return _Attempt(status=status, original_status=raw_status)

    def _refine_skip(
        self, element: Any, status: TestStatus, original: str
    ) -> tuple[TestStatus, str]:
        """Let ``<skipped type="pytest.xfail">`` be an xfail rather than a plain skip.

        pytest has no ``<xfail>`` element — it writes an expected failure as a
        ``<skipped>`` with the intent in the ``type`` attribute. Recording that
        as ``skipped`` would quietly remove every xfail from the pass-rate
        denominator, which is the opposite of what ``xfailed`` means.
        """
        raw_type = element.attrib.get("type")
        if not raw_type:
            return status, original
        refined = normalize_status(str(raw_type).rsplit(".", 1)[-1])
        if refined in (TestStatus.XFAILED, TestStatus.XPASSED, TestStatus.CANCELLED):
            return refined, str(raw_type)[:_SHORT_TEXT_LIMIT]
        return status, original

    def _child_text(self, element: Any, tag: str) -> str:
        for child in element:
            if _localname(child.tag) == tag:
                return "".join(child.itertext())
        return ""

    def _failure(self, element: Any, location: str) -> FailureDetail | None:
        """Build a :class:`FailureDetail` from a ``<failure>``-shaped element.

        The trace is the element's own text plus any ``<stackTrace>`` child, and
        pointedly *not* ``itertext()``: a Surefire rerun element also contains
        ``<system-out>``, and folding the console log into the stack trace makes
        every failure look identical.
        """
        parts: list[str] = []
        if element.text and element.text.strip():
            parts.append(element.text)
        for child in element:
            if _localname(child.tag) == "stacktrace":
                parts.append("".join(child.itertext()))
        body = "\n".join(part for part in parts if part.strip()).strip()

        type_text, _ = _clip(element.attrib.get("type"), _SHORT_TEXT_LIMIT)
        message, _ = _clip(element.attrib.get("message"), self.text_limit)
        if message is None and body:
            message, _ = _clip(body.splitlines()[0], _SHORT_TEXT_LIMIT)
        stack, truncated = _clip(body, self.text_limit)
        if truncated:
            self.warn_once(
                "text_truncated",
                f"a stack trace longer than {self.text_limit} characters was cut",
                location=location,
            )
        if not any((type_text, message, stack)):
            return None
        return FailureDetail(
            type=type_text,
            message=message,
            stack_trace=stack,
            truncated=truncated,
        )

    # ------------------------------------------------------------- closing

    def _run_finished(self) -> DomainEvent:
        started = self.run_started_at or self.parse_started
        total = self.suite_seconds or self.case_seconds
        root_time = self.seconds(self.root_attrib.get("time"), "testsuites")
        if root_time:
            total = root_time
        duration_ms = round(total * 1000) if total else None
        finished = started + timedelta(seconds=total) if total else max(started, self.clock)
        return self.emit(
            EventType.RUN_FINISHED,
            RunFinishedData(
                finished_at=finished,
                status=None,  # advisory only; the server folds the attempts
                duration_ms=duration_ms,
                metadata=self.run_metadata,
            ),
            subject=self.project,
            time=finished,
        )

    # ------------------------------------------------------------- memory

    def _release(self, element: Any) -> None:
        """Drop a finished element so a huge file stays a stream, not a DOM."""
        if element is None:
            return
        element.clear()
        parent = self.stack[-1].element if self.stack else self.root_element
        if parent is None or parent is element:
            return
        with contextlib.suppress(ValueError, AttributeError):
            parent.remove(element)
