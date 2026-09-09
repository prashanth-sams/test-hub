# Writing a report adapter

An adapter turns bytes into a stream of domain events. That is its whole job. It
does not touch the database, it does not know that HTTP exists, and it does not
decide what the run is called — all of that arrives on a `ParseContext`. Two
consequences follow, and they are the reason the seam is where it is: an adapter
is unit-testable against a fixture file with no server, and the file-import path
and the live-publishing path share every line of code after the adapter returns.

Test Hub ships two adapters, `junit-xml` and `testhub-json`, and registers them
through exactly the entry-point group your package will use. There is no
privileged built-in path. If you want the short version: implement `detect` and
`parse`, publish one entry point, and you are done.

## The protocol

```python
class ReportAdapter(Protocol):
    name: str

    def detect(
        self, stream: IO[bytes], filename: str | None, content_type: str | None
    ) -> float: ...
    def parse(self, stream: IO[bytes], context: ParseContext) -> Iterable[DomainEvent]: ...
```

It is a `Protocol`, so there is no base class you must inherit.
`test_hub.adapters.base.BaseAdapter` exists if you want the boring parts already
written — a stream sniffer that rewinds, extension and content-type scoring, and
a `describe()` that feeds `test-hub formats` — but structural typing means a
plain class with the two methods is a perfectly good adapter.

`name` must be unique across installed adapters and stable. It is stored on
every run the adapter parsed, shown to the user, and is what `--format <name>`
selects. Renaming it later orphans that history.

`BaseAdapter` also reads these class attributes:

| Attribute | Used for |
|---|---|
| `display_name` | The human name in `test-hub formats` and `GET /api/v1/adapters`. |
| `description` | One line, shown in the same places and inside a 415. |
| `extensions` | Lowercase, dotted, e.g. `(".hopper.json", ".hopper")`. Contributes 0.3 to `hint_score`. |
| `content_types` | MIME types, matched on the part before `;`. Also 0.3. |
| `priority` | Tie-break when two adapters score the same. Higher wins. `testhub-json` is 100, `junit-xml` is 50. |

## Detection, and the scores that mean something

`detect` returns a confidence in `[0.0, 1.0]`. The registry asks every installed
adapter, rewinding the stream between each, and takes the highest score at or
above `MIN_CONFIDENCE` (0.3). Ties break on `priority`, then on `name` — so two
adapters that both answer 0.9 resolve the same way on every machine and in every
process, not according to whichever `importlib.metadata` happened to yield
first.

| Score | What it means | Example |
|---|---|---|
| `0.0` | Definitely not mine. Return this, not an exception. | The stream starts with `<`, and your format is JSON. |
| `0.3` | The filename extension or content type fits; nothing else was checked. This is the floor for selection — a file can be chosen on its name alone, but only when nothing else claims it harder. | `results.hopper` with no readable content. |
| `0.6` | The content sniffs right: the root element, or the required top-level keys, are present. | A JSON object with both `run` and `tests` keys. |
| `0.9` | The content is unmistakably mine: a namespace, a generator tag, a marker key. | A `<testsuites>` root, or a top-level `"hopper":` key. |
| `1.0` | The content *declares itself* as mine. Reserve this for a real discriminator. | `"kind": "test-hub-report"`. |

`--format` (or the `format` form field) skips detection entirely and is reported
with confidence `1.0`, because nothing was guessed. An explicit format always
wins even when the content plainly looks like something else: the user asked for
a specific parser and is entitled to that parser's error message rather than a
silent substitution.

Three rules for `detect`:

- **Never raise.** Return `0.0`. A detector that throws is caught, logged and
  scored 0.0 anyway, but the log line is noise nobody needs.
- **Read a few kilobytes, not the file.** `BaseAdapter.sniff(stream)` reads
  `SNIFF_BYTES` (8192) and rewinds. The registry rewinds between adapters too,
  so you are not obliged to be tidy — but an adapter that reads 200 MB to decide
  makes every import on the server slow, including imports it loses.
- **Never claim "any JSON" or "any XML".** This is the one rule with teeth. An
  adapter that answers 0.6 for every JSON object will swallow coverage reports,
  ESLint output and half of `package.json`, and every one of those arrives as a
  silently wrong run with a plausible-looking pass rate. Require a discriminator
  your format actually has. `testhub-json` refuses to claim a JSON document
  without a `kind` key for exactly this reason, and unknown input returning a
  415 that lists every installed adapter is a much better outcome than a wrong
  number on a dashboard.

## `ParseContext`

Everything an adapter needs that is not in the file:

```python
@dataclass(slots=True)
class ParseContext:
    workspace_id: uuid.UUID
    project_key: str
    run_id: uuid.UUID
    name: str | None
    team: str | None
    component: str | None
    test_type: TestType
    trigger: TriggerType
    context: RunContext
    tags: list[str]
    metadata: dict[str, Any]
    source: str
    filename: str | None
    max_text_bytes: int  # mirrors TEST_HUB_MAX_TEXT_FIELD_BYTES
    warnings: WarningCollector

    def next_sequence(self) -> int: ...
```

Merge what the file knows with what the context says, and **where they conflict,
the context wins** — the human typing `--environment staging` has more
information than a report exported three hours ago on another machine.

Call `context.next_sequence()` for every event instead of counting yourself.
Events emitted by nested generators then still come out strictly increasing, and
a sub-parser handed the same context cannot restart the numbering at 1.

`max_text_bytes` is there so an adapter never builds a 40 MB string it is about
to have truncated. Respect it when you concatenate log output.

## `WarningCollector`

The contract with the user is: **anything you cannot represent exactly becomes a
warning, not an exception and not a silent approximation.** A warning appears in
the `ImportResult`, on the run, in the CLI output and in the API response — it is
how "your report claimed 40 tests and I found 19" reaches a human.

```python
context.warnings.add(
    "unknown_status",
    f"hopper outcome {word!r} has no canonical equivalent; recorded as unknown",
    location="checkout/pay",
)
context.warnings.info("synthesized_timestamps", "the report had no timestamp; using import time")
```

`add()` takes a stable `code` (match on it), a `message` (truncated at 512
characters), an optional `location`, a `severity` of `warning` (default) or
`info`, and any further keyword arguments, which land in `detail`. The collector
is bounded at 500 warnings and then counts what it drops, emitting one final
`warnings_truncated` info warning — a pathological file must produce a bounded
response.

Codes the built-in adapters already use, worth reusing rather than inventing
near-synonyms: `unknown_status`, `counts_mismatch`, `naive_timestamp`,
`synthesized_timestamps`, `suite_output_lifted`, `unknown_report_version`.

## Identity

<a id="identity"></a>

Derive test and suite ids through `test_hub.domain.identity`. Never invent them,
and never carry them from the file unless the file's ids are themselves derived
the same way.

```python
from test_hub.domain.identity import suite_id, test_identity

suite = suite_id(project=context.project_key, suite_path="checkout/cart")
test = test_identity(
    project=context.project_key,
    suite_path="checkout/cart",
    test_name="adds an item",
    class_name=None,
    params={"currency": "EUR"},
)
```

Both are UUIDv5 in a fixed namespace, computed from *what the test is* and never
from *when or where it ran*. The inputs are project key, suite path, class name,
test name and normalized parameters. Excluded on purpose: timestamps, line
numbers, durations, worker or shard id, hostname, run id, build number, branch.

That is not tidiness, it is the whole reason `GET /api/v1/tests/{id}` has a
history to show. Get it wrong — hash a line number, say — and every run invents a
fresh set of tests, nothing can be compared, and no test is ever flaky because no
test ever appears twice.

Parameters are normalized before hashing: named parameters are sorted by name so
`(b=2, a=1)` and `(a=1, b=2)` are one test, positional parameters keep their
order because position *is* the name, and rendered values have memory addresses
and bare UUIDs replaced with stable tokens so `<Foo object at 0x7f3c…>` does not
mint a new test every run. `identity_source()` is exposed and documented so a
publisher in another language can reproduce the hashed string byte for byte.

You may omit `test_id` from a `test.finished` payload entirely and let the server
derive it from the identity components you did supply. That is usually the right
choice: one implementation, no drift.

## Retries

**A retry is a new attempt, never an overwrite.** Emit a second `test.finished`
for the same `test_id` with `attempt_number` incremented. Nothing in Test Hub
ever updates an attempt's outcome with a later one, because attempt 1 failing and
attempt 2 passing is the entire observable definition of a flaky test, and an
UPDATE erases it.

The awkward part is formats that nest the earlier attempts inside the final one.
Maven Surefire writes a retried test as *one* `<testcase>`:

```xml
<testcase name="chargesCard" classname="CheckoutIT" time="4.1">
  <flakyFailure message="timeout" type="AssertionError">stack…</flakyFailure>
  <flakyFailure message="timeout" type="AssertionError">stack…</flakyFailure>
</testcase>
```

`flakyFailure`/`flakyError` mean "this failed, then a rerun passed", so the
`<testcase>`'s own absent outcome is the pass; `rerunFailure`/`rerunError` mean
"the rerun failed too", so its own `<failure>` is the final word. Either way the
nested elements are the *earlier* attempts and the element's own outcome is the
*last* one. The example above is three attempts — failed, failed, passed — and a
flaky test. Collapsing it into a single "passed" is how a flaky suite stays
invisible for a year.

If your format has a "flaky" status word rather than attempt records, emit what
you can and warn. `normalize_status` maps Playwright's `flaky` to `xfailed` so it
does not become `unknown`, but that is a fallback, not the intent: an adapter that
can see the individual attempts should emit the individual attempts.

## Statuses

Map the framework's word through `normalize_status` and keep the original beside
it:

```python
from test_hub.domain.status import TestStatus, normalize_status

status = normalize_status(word)
if status is TestStatus.UNKNOWN and word:
    context.warnings.add("unknown_status", f"{word!r} has no canonical equivalent", location=path)
```

The table is a lookup, not a heuristic, and it is deliberately generous: it
already knows `ok`, `green`, `broken`, `crashed`, `inconclusive`,
`unexpectedSuccess`, `notRunnable`, `expectedFailure` and about sixty more, with
case, whitespace, hyphens and underscores collapsed before lookup. A word it does
not know becomes `UNKNOWN` — never `passed` by accident — and your warning is
what tells the user their dialect needs one more line in the table.

Always set `original_status` to the raw word. Verified round-trip from the worked
example below: `"broken"` → `status: "error"`, `original_status: "broken"`.

## Safety

Reports are untrusted uploads. Two rules are not negotiable.

**Parse XML with `defusedxml`, never `xml.etree`.**

```python
from defusedxml.ElementTree import iterparse as defused_iterparse
```

The two classic attacks — a billion-laughs entity bomb that turns 200 bytes into
gigabytes of RAM, and an XXE that makes the server read `/etc/passwd` and put it
in a stack trace on a dashboard — are both entity tricks, and both are refused at
the parser rather than mitigated afterwards.
`tests/fixtures/junit/billion-laughs.xml` and `xxe.xml` exist to keep that true;
both currently produce a 422 with `detail.reason: "DTDForbidden"` and no
expansion at all. Add equivalent fixtures for your own format.

**Stream; do not build a DOM.** `iterparse` with `elem.clear()`, and detach each
finished element from its parent as soon as you have turned it into events. The
built-in JUnit adapter holds one suite's worth of elements plus the event it is
currently yielding, whatever the file's size. A 200 MB Surefire aggregate parsed
into a DOM is a 2 GB resident set and an out-of-memory kill that looks like a
crash to whoever is watching.

Two more, less dramatic but just as real:

- **Do not build a path from anything in the file.** Test Hub's artifact store
  refuses names containing separators, `..`, NUL bytes or Windows device names
  outright rather than repairing them. If you emit `artifact.published` events,
  reduce a reported filename to its last component before you do.
- **Do not trust the file's own totals.** `tests="200" failures="3"` is advisory
  and frequently wrong — a partially uploaded shard, a framework that counts
  retries, a hand-edited file. Counts are folded from the cases actually parsed.
  Emit a `counts_mismatch` warning when the file disagrees with you rather than
  hiding the disagreement or, worse, believing it.

## Registration

One entry point, in the `test_hub.adapters` group:

```toml
[project.entry-points."test_hub.adapters"]
hopper = "test_hub_hopper.adapter:HopperAdapter"
```

The value may name a class, a zero-argument factory or a ready-made singleton;
all three appear in the wild and all three work. The key is the entry-point name
and is only used for logging — `AdapterRegistry` keys on your adapter's `.name`
attribute, so make them match to save the next person some confusion.

Discovery is `importlib.metadata.entry_points(group="test_hub.adapters")`, which
means **your package must be installed** — `pip install .`, or `pip install -e .`
during development. A checkout on `PYTHONPATH` publishes no entry points and
your adapter will not appear. (Test Hub's own two adapters have a
by-module-path fallback for exactly that situation, which is why `junit-xml`
keeps working from a bare source tree and yours will not.)

An entry point that raises on import is skipped with a warning rather than
taking the process down: one broken plugin must not stop the server parsing a
JUnit file. Run with `--log-level debug` if your adapter is not showing up —
that path logs the name, the module and the exception.

Registering a name that already exists replaces the earlier adapter and logs a
warning. That is deliberate: it is how an operator overrides a built-in with a
patched fork without uninstalling anything.

---

# A complete adapter package

This is a working, tested package for an invented format. Everything below was
run: the tests pass, `test-hub formats` lists the adapter, and `test-hub ingest`
imports a file with it.

The format belongs to a fictional runner called **Hopper**, which writes a JSON
report with a top-level `"hopper"` key:

```json
{
  "hopper": {"version": 1, "suite": "checkout smoke"},
  "started_at": "2026-04-02T09:00:00Z",
  "finished_at": "2026-04-02T09:00:07Z",
  "cases": [
    {"group": "checkout/cart", "name": "adds an item", "outcome": "ok", "seconds": 0.42},
    {"group": "checkout/cart", "name": "applies a coupon", "outcome": "broken", "seconds": 1.1,
     "error": {"kind": "AssertionError", "message": "expected 9.99, got 10.99",
               "trace": "at cart.spec:41"}},
    {"group": "checkout/pay", "name": "charges the card", "outcome": "ok", "seconds": 2.3,
     "params": {"currency": "EUR"}},
    {"group": "checkout/pay", "name": "handles a decline", "outcome": "deferred", "seconds": 0.0}
  ]
}
```

Note what makes it adaptable: a marker key to detect on, a stable grouping key
per case, an attempt number, and a status word that mostly maps and sometimes
does not. `deferred` is in there on purpose — it is the case that must warn.

## `pyproject.toml`

```toml
[build-system]
requires = ["hatchling>=1.24"]
build-backend = "hatchling.build"

[project]
name = "test-hub-hopper"
version = "0.1.0"
description = "Test Hub adapter for Hopper runner reports."
requires-python = ">=3.11"
dependencies = ["test-hub>=0.1"]

[project.entry-points."test_hub.adapters"]
hopper = "test_hub_hopper.adapter:HopperAdapter"

[tool.hatch.build.targets.wheel]
packages = ["src/test_hub_hopper"]

[tool.pytest.ini_options]
testpaths = ["tests"]
# The domain classes are called TestStatus, TestRun… so pytest's default
# `Test*` class rule would try to collect them. Narrow it, as Test Hub does.
python_classes = ["*Tests", "Describe*"]
```

Depend on `test-hub` itself: you import its domain models, and pinning a floor
is how a user finds out that your adapter needs a newer schema version rather
than discovering it through a `KeyError`.

## `src/test_hub_hopper/adapter.py`

```python
"""Adapter for the Hopper runner's JSON report."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import IO, Any

from test_hub.adapters.base import SNIFF_BYTES, BaseAdapter, ParseContext
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
from test_hub.domain.identity import TEST_HUB_NAMESPACE, suite_id, test_identity
from test_hub.domain.models import FailureDetail, utcnow
from test_hub.domain.status import normalize_status
from test_hub.errors import AdapterError

_MARKER = re.compile(rb'"hopper"\s*:')


def _event_id(source: str, run_id: uuid.UUID, sequence: int, kind: EventType) -> uuid.UUID:
    """Deterministic event id, so re-importing the same file applies nothing new."""
    return uuid.uuid5(TEST_HUB_NAMESPACE, f"{source}\x1f{run_id}\x1f{sequence}\x1f{kind}")


class HopperAdapter(BaseAdapter):
    name = "hopper"
    display_name = "Hopper JSON"
    extensions = (".hopper.json", ".hopper")
    content_types = ("application/vnd.hopper+json",)
    priority = 10
    description = 'Hopper runner JSON reports (top-level "hopper" key).'

    def detect(self, stream: IO[bytes], filename: str | None, content_type: str | None) -> float:
        head = self.sniff(stream, SNIFF_BYTES)
        stripped = head.lstrip(b"\xef\xbb\xbf").lstrip()
        if not stripped.startswith(b"{"):
            return 0.0
        if _MARKER.search(head):
            return 0.9
        return self.hint_score(filename, content_type)

    def parse(self, stream: IO[bytes], context: ParseContext) -> Iterator[DomainEvent]:
        raw = stream.read()
        try:
            document: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AdapterError(f"not valid Hopper JSON: {exc}") from exc
        if not isinstance(document, dict) or "hopper" not in document:
            raise AdapterError('this JSON has no top-level "hopper" key')

        header = document.get("hopper") or {}
        if header.get("version") != 1:
            context.warnings.add(
                "unknown_report_version",
                f"hopper report version {header.get('version')!r} is not 1; "
                "parsing it as version 1",
            )

        run_id = context.run_id
        source = context.source
        started = _timestamp(document.get("started_at")) or utcnow()

        def emit(kind: EventType, data: Any, when: datetime | None = None) -> DomainEvent:
            sequence = context.next_sequence()
            return make_event(
                kind,
                run_id=run_id,
                workspace_id=context.workspace_id,
                sequence=sequence,
                data=data,
                source=source,
                event_id=_event_id(source, run_id, sequence, kind),
                time=when or started,
            )

        yield emit(
            EventType.RUN_QUEUED,
            RunQueuedData(
                project_key=context.project_key,
                name=context.name or header.get("suite"),
                team=context.team,
                component=context.component,
                test_type=context.test_type,
                trigger=context.trigger,
                context=context.context,
                tags=list(context.tags),
                metadata=dict(context.metadata),
            ),
        )
        yield emit(EventType.RUN_STARTED, RunStartedData(started_at=started))

        seen: dict[str, uuid.UUID] = {}
        for case in document.get("cases") or []:
            path = str(case.get("group") or "hopper")
            if path not in seen:
                seen[path] = suite_id(project=context.project_key, suite_path=path)
                yield emit(
                    EventType.SUITE_STARTED,
                    SuiteStartedData(suite_id=seen[path], name=path.rsplit("/", 1)[-1], path=path),
                )

            status, original = _status(case.get("outcome"), path, context)
            failure = None
            if case.get("error"):
                failure = FailureDetail(
                    type=str(case["error"].get("kind") or "Error"),
                    message=str(case["error"].get("message") or ""),
                    stack_trace=case["error"].get("trace"),
                )
            yield emit(
                EventType.TEST_FINISHED,
                TestFinishedData(
                    test_id=test_identity(
                        project=context.project_key,
                        suite_path=path,
                        test_name=str(case.get("name") or "(unnamed)"),
                        params=case.get("params"),
                    ),
                    suite_id=seen[path],
                    name=str(case.get("name") or "(unnamed)"),
                    suite_path=path,
                    params=_params(case.get("params")),
                    attempt_number=int(case.get("attempt") or 1),
                    status=status,
                    original_status=original,
                    duration_ms=_millis(case.get("seconds")),
                    failure=failure,
                    stdout=case.get("stdout"),
                ),
            )

        for path, identity in seen.items():
            yield emit(EventType.SUITE_FINISHED, SuiteFinishedData(suite_id=identity))

        yield emit(
            EventType.RUN_FINISHED,
            RunFinishedData(finished_at=_timestamp(document.get("finished_at")) or utcnow()),
        )


def _status(word: Any, where: str, context: ParseContext):
    status = normalize_status(str(word or ""))
    if str(status) == "unknown" and word:
        context.warnings.add(
            "unknown_status",
            f"hopper outcome {word!r} has no canonical equivalent; recorded as unknown",
            location=where,
        )
    return status, (str(word) if word else None)


def _params(value: Any) -> str | None:
    if value in (None, "", {}, []):
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", "="))


def _millis(seconds: Any) -> int | None:
    try:
        return max(0, round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
```

Three details in there are the ones that matter:

**Event ids are derived, not random.** `make_event` defaults `id` to a fresh
UUIDv4, which means re-importing the same file would apply everything twice.
`_event_id` hashes `(source, run_id, sequence, type)` instead, so a repeated
import of the same file into the same run is a no-op by construction. Both
built-in adapters do exactly this. The cost is the flip side documented in
[api.md](api.md#post-apiv1imports): two *different* files into one `run_id`
collide on those ids and lose events. One file, one run.

**Every suite is announced before any result arrives.** `suite.started` is
emitted the first time a group is seen, and all `suite.finished` events come at
the end. A dashboard that subscribes mid-import needs the tree before the leaves.

**The run's own outcome is not emitted.** `RunFinishedData` carries no `status`
here, because the server folds it from the attempts. Sending one would be
ignored anyway for everything except `cancelled`.

## `tests/test_hopper_adapter.py`

An adapter's test suite has a shape. These are the questions worth answering,
and this file answers each of them once:

```python
"""What a third-party adapter has to prove about itself."""

from __future__ import annotations

import io
import json
import uuid

import pytest

from test_hub.adapters.base import MIN_CONFIDENCE, ParseContext
from test_hub.domain.events import EventType

# Aliased: pytest collects any module-level `test_*` name, imports included.
from test_hub.domain.identity import test_identity as compute_identity
from test_hub.domain.status import TestStatus
from test_hub.errors import AdapterError
from test_hub_hopper import HopperAdapter

REPORT = {
    "hopper": {"version": 1, "suite": "checkout smoke"},
    "started_at": "2026-04-02T09:00:00Z",
    "finished_at": "2026-04-02T09:00:07Z",
    "cases": [
        {"group": "checkout/cart", "name": "adds an item", "outcome": "ok", "seconds": 0.42},
        {
            "group": "checkout/cart",
            "name": "adds an item",
            "outcome": "ok",
            "seconds": 0.40,
            "attempt": 2,
        },
        {
            "group": "checkout/pay",
            "name": "charges the card",
            "outcome": "broken",
            "seconds": 2.3,
            "error": {"kind": "AssertionError", "message": "boom"},
        },
        {"group": "checkout/pay", "name": "handles a decline", "outcome": "deferred"},
    ],
}


def stream(document: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(document).encode("utf-8"))


def context() -> ParseContext:
    return ParseContext(
        workspace_id=uuid.uuid4(), project_key="checkout-service", run_id=uuid.uuid4()
    )


class DetectionTests:
    def test_claims_its_own_format(self) -> None:
        assert HopperAdapter().detect(stream(REPORT), "results.hopper.json", None) == 0.9

    def test_does_not_claim_unrelated_json(self) -> None:
        score = HopperAdapter().detect(stream({"tests": [], "coverage": 91}), "cov.json", None)
        assert score < MIN_CONFIDENCE

    def test_does_not_claim_xml(self) -> None:
        assert HopperAdapter().detect(io.BytesIO(b"<testsuites/>"), "r.xml", None) == 0.0

    def test_leaves_the_stream_where_it_found_it(self) -> None:
        handle = stream(REPORT)
        HopperAdapter().detect(handle, "results.hopper.json", None)
        assert handle.tell() == 0


class ParsingTests:
    def test_emits_the_expected_event_shape(self) -> None:
        events = list(HopperAdapter().parse(stream(REPORT), context()))
        types = [event.type for event in events]
        assert types[0] is EventType.RUN_QUEUED
        assert types[1] is EventType.RUN_STARTED
        assert types[-1] is EventType.RUN_FINISHED
        assert types.count(EventType.TEST_FINISHED) == 4
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))

    def test_identity_is_derived_not_invented(self) -> None:
        events = list(HopperAdapter().parse(stream(REPORT), context()))
        first = next(e for e in events if e.type is EventType.TEST_FINISHED)
        assert uuid.UUID(first.data["test_id"]) == compute_identity(
            project="checkout-service", suite_path="checkout/cart", test_name="adds an item"
        )

    def test_a_retry_is_a_second_attempt(self) -> None:
        events = list(HopperAdapter().parse(stream(REPORT), context()))
        cart = [
            e.data
            for e in events
            if e.type is EventType.TEST_FINISHED and e.data["suite_path"] == "checkout/cart"
        ]
        assert [d["attempt_number"] for d in cart] == [1, 2]
        assert len({d["test_id"] for d in cart}) == 1

    def test_reimporting_the_same_file_produces_the_same_event_ids(self) -> None:
        run_id = uuid.uuid4()
        workspace = uuid.uuid4()

        def once() -> list[str]:
            ctx = ParseContext(
                workspace_id=workspace, project_key="checkout-service", run_id=run_id
            )
            return [str(event.id) for event in HopperAdapter().parse(stream(REPORT), ctx)]

        assert once() == once()

    def test_the_original_word_survives_normalization(self) -> None:
        events = list(HopperAdapter().parse(stream(REPORT), context()))
        pay = next(
            e.data
            for e in events
            if e.type is EventType.TEST_FINISHED and e.data["name"] == "charges the card"
        )
        assert pay["status"] == TestStatus.ERROR
        assert pay["original_status"] == "broken"


class WarningTests:
    def test_an_unmappable_outcome_warns_instead_of_guessing(self) -> None:
        ctx = context()
        list(HopperAdapter().parse(stream(REPORT), ctx))
        codes = [warning.code for warning in ctx.warnings.to_list()]
        assert "unknown_status" in codes

    def test_a_future_report_version_warns_and_still_parses(self) -> None:
        ctx = context()
        document = {**REPORT, "hopper": {"version": 2}}
        events = list(HopperAdapter().parse(stream(document), ctx))
        assert any(e.type is EventType.TEST_FINISHED for e in events)
        assert "unknown_report_version" in [w.code for w in ctx.warnings.to_list()]


class RefusalTests:
    def test_json_without_the_marker_is_refused(self) -> None:
        with pytest.raises(AdapterError):
            list(HopperAdapter().parse(stream({"tests": []}), context()))

    def test_broken_json_is_an_adapter_error_not_a_crash(self) -> None:
        with pytest.raises(AdapterError):
            list(HopperAdapter().parse(io.BytesIO(b"{not json"), context()))
```

```shell
$ pytest -q
.............                                                            [100%]
13 passed in 0.07s
```

The aliased import in the header is not decoration. `from
test_hub.domain.identity import test_identity` puts a module-level callable named
`test_identity` in scope, pytest collects it as a test, and you get a confusing
`fixture 'project' not found` error next to thirteen passing tests. Test Hub's
own suite aliases the same import for the same reason.

## Installing and checking it

```shell
$ pip install .

$ test-hub formats

  3 adapter(s) installed

  hopper  Hopper JSON
      Hopper runner JSON reports (top-level "hopper" key).
      extensions: .hopper.json, .hopper

  junit-xml  JUnit XML
      JUnit/xUnit XML from pytest, Surefire, Gradle, Jest, Playwright and friends.
      extensions: .xml, .junit, .junit.xml

  testhub-json  Test Hub JSON
      Test Hub's own report and raw event JSON (kind: test-hub-report/-events).
      extensions: .json

  Force one with --format <name>. Add more by installing an adapter package.

$ test-hub ingest results.hopper.json --project checkout-service --type smoke

  imported run ed1a7ddd-0e4f-4b96-a0e4-7b5462b536d1
  adapter   hopper (detected, 90% confident)
  project   checkout-service
  stored in /tmp/th-docs-hopper
  4 tests  2 passed  1 error  50.0% pass

  1 parser warning(s):
    unknown_status [checkout/pay]: hopper outcome 'deferred' has no canonical equivalent; recorded as unknown
```

Four cases, four tests, one of them an unmapped status that shows up as a warning
rather than as a wrong pass rate. That warning is the adapter doing its job.

The same adapter is now live on a server too: restart `test-hub serve` and it
appears in `GET /api/v1/adapters`, and every `POST /api/v1/imports` of a Hopper
file is detected and parsed. No configuration, no core change.

---

# Formats we would like

The first release ships **two** adapters — `junit-xml` and `testhub-json` — and
says so plainly rather than advertising a matrix of formats it does not have.
JUnit XML covers a lot of the world by accident (pytest, Surefire, Gradle, Jest,
Playwright, Mocha and most of the rest can all emit it), which is precisely why
it was the one to build first. Everything below is a format that deserves a
real adapter rather than a lossy JUnit export, and every one of them is a
package somebody else can own and release on their own schedule:

| Format | Why it deserves its own adapter rather than a JUnit export |
|---|---|
| **TRX** (MSTest / `dotnet test`) | Carries `Outcome` words JUnit has no room for — `NotRunnable`, `Inconclusive`, `Timeout` — which `normalize_status` already maps. Also has real per-test start and end times, which JUnit does not. |
| **NUnit 3** | Native `<test-case>` with attachments, properties and its own `<reason>`/`<failure>` split. |
| **Cucumber / Gherkin JSON** | Step-level results. Flattening a scenario to one test throws away exactly the information a BDD suite exists to produce. |
| **Jest** (`--json`) | Native output has per-assertion failure messages, `ancestorTitles` for the suite tree and `numRetries`, none of which survive its JUnit reporter. |
| **Playwright** (`--reporter=json`) | Attempts, traces, screenshots, videos and projects (browser × config) as first-class fields. This is the one where the JUnit export loses the most. |
| **Maven Surefire** (native) | The `rerunFailure`/`flakyFailure` handling is already in `junit-xml`, but Surefire's own report XML additionally carries the full system-out per test and the rerun ordering. |
| **Gradle test XML** | Same family, different quirks around nested suites and `standard-out` placement. |
| **Go** (`go test -json`) | A stream, not a document: an adapter could publish live over `POST /runs/{id}/events` as the events arrive. Subtests give a real hierarchy. |
| **Rust** (`cargo test --format json` / `cargo-nextest`) | Nextest has retries and per-test timing; the libtest JSON stream has neither in a JUnit export. |
| **Performance formats** (JMeter JTL, k6 JSON, Gatling) | These do not fit the pass/fail model cleanly, and pretending they do is worse than modelling them properly. `TestType.PERFORMANCE` exists; the interesting design question is what a "test" is when the answer is a latency distribution. |

If you write one, the checklist is short: derive identity through
`test_hub.domain.identity`, emit attempts rather than overwriting, warn about
anything you cannot represent exactly, never claim a format you cannot identify
from its content, and parse untrusted XML with `defusedxml`. Everything else is
your format's problem, which is the point of the seam.
