# The event contract

Every write into Test Hub becomes a domain event. A JUnit XML file dropped on
the CLI, a `POST /api/v1/imports` from a Jenkins job and a live publisher
streaming results as they happen all converge on the same list of events and the
same `apply_events` call, and nothing else changes state (ADR-0001 D2). Read
[architecture.md](architecture.md) for why that is the only write path; this
document is the wire contract for the events themselves.

The types are declared in
[`test_hub/domain/events.py`](../src/test_hub/domain/events.py) and validated
there, so anything this document says is enforced at the boundary rather than
merely hoped for.

## The envelope

The outer shape is [CloudEvents 1.0](https://cloudevents.io) in the structured
JSON mode, plus four Test Hub extension attributes.

| Attribute | Type | Required | Meaning |
|---|---|---|---|
| `specversion` | `"1.0"` | yes (defaulted) | Literal. A different value is a validation error, not a negotiation. |
| `id` | UUID | yes (generated) | Unique *within a source*. Together with `source` this is the idempotency key of a single event. |
| `source` | string, 1–512 chars | yes (defaulted) | Who produced it. A URI-ish identifier; Test Hub's own producers use `urn:test-hub:cli-ingest`, `urn:test-hub:api-import` and `urn:test-hub:api`. The default is `https://test-hub.dev/source/unknown`. |
| `type` | enum | yes | One of the eight types below. |
| `time` | RFC 3339, timezone-aware | yes (defaulted to now) | When the fact happened. A naive timestamp is rejected. |
| `datacontenttype` | string | defaulted | `application/json`. |
| `subject` | string or null | no | Free-text hint about what the event is about. The adapters set it to the project key, the suite path or the test's full name. Nothing reads it; it is there for a human tailing a log. |
| `schemaversion` | int | defaulted | Canonical schema version. See [schemas/README.md](schemas/README.md). |
| `workspaceid` | UUID | **yes** | Tenancy. |
| `runid` | UUID | **yes** | Which run this event belongs to. |
| `sequence` | int ≥ 0 | **yes** | Monotonic ordering within the run. |
| `data` | object | yes | The typed payload, validated against `type`. |

### Why `workspaceid` and not `workspace_id`

CloudEvents requires extension attribute names to be made of lowercase letters
and digits only. That is why the field name — and therefore what goes out on the
wire — is `workspaceid`, `runid` and `schemaversion`.

Writing that by hand is unpleasant, so **the readable spellings are accepted on
input**. `workspace_id`, `run_id` and `schema_version` are validation aliases;
either form parses, and only the lowercase form is ever emitted:

```python
>>> event = DomainEvent.model_validate({
...     "specversion": "1.0",
...     "id": "22222222-3333-4444-8555-666666666666",
...     "source": "urn:test-hub:ci",
...     "type": "run.started",
...     "time": "2026-09-08T01:15:00Z",
...     "workspace_id": "4f9c1b2e-6a7d-5c3b-8e1f-2d3a4b5c6d7e",
...     "run_id": "9b1d1a44-0f3e-4a1f-8f2a-7c6b5d4e3f21",
...     "schema_version": 1,
...     "sequence": 2,
...     "data": {"started_at": "2026-09-08T01:15:00Z"},
... })
>>> sorted(k for k in event.model_dump(mode="json") if "id" in k or "version" in k)
['id', 'runid', 'schemaversion', 'specversion', 'workspaceid']
```

Python code never has to type the ugly form either: `event.workspace_id`,
`event.run_id` and `event.schema_version` are read-only properties over the
extension fields.

### Unknown fields are refused

`DomainEvent` is `extra="forbid"`, and so is every payload model. A publisher
that sends `durationMs` instead of `duration_ms` gets a 422 naming the field
rather than a silently dropped value and a wrong duration on the dashboard. This
is the single most useful thing about the contract during a first integration
and it is worth not loosening.

### A schema version this build does not speak is refused

```python
>>> DomainEvent.model_validate({..., "schema_version": 2, ...})
ValidationError: Value error, unsupported schema_version 2; this build speaks 1
```

An older Test Hub reading a newer event cannot know which fields changed
meaning. A failed import is recoverable; a wrong pass rate that nobody
questions is not.

## Sequence numbers

`sequence` is a **monotonically increasing integer within one run**, assigned by
the publisher. It is not global, it is not a timestamp, and it does not have to
be dense.

It exists so that a consumer can detect a gap, and so that the server can apply
out-of-order arrivals deterministically: `apply_events` sorts the batch by
`(sequence, time)` before dispatching, so a batch that arrives shuffled by a
proxy or a thread pool lands in the order the publisher meant.

`sequence` is `ge=0`, and **0 has a special meaning on the way in: "server,
allocate one for me."** `RunService._assign_sequences` fills the zeros from the
run's current high-water mark, inside the same transaction that applies the
batch, so two publishers posting concurrently receive disjoint blocks rather
than both starting at 1:

```python
if all(event.sequence > 0 for event in events):
    return events
cursor = uow.events.next_sequence(run_id)   # max(sequence) + 1, or 1
for event in events:
    if event.sequence > 0:
        keep it
    else:
        assign cursor; cursor += 1
```

Two consequences worth knowing:

- **Send all zeros or all real numbers.** A batch that mixes them keeps the
  publisher's numbers *and* allocates from the high-water mark for the zeros,
  and nothing checks that the two ranges do not overlap. Overlap does not
  corrupt anything — sequence is not unique and the fold is keyed on
  `(run, test, attempt_number)` — but it does make the run's log confusing to
  read and makes gap detection meaningless.
- **If you want gap detection, number your own events.** A long suite streaming
  over an hour is exactly the case where a silently dropped batch matters, and a
  consumer cannot notice a hole in a sequence the server invented.

`EventRepository.list_events(run_id, after_sequence=0)` replays a run's log
oldest first. `after_sequence` is exclusive, with one deliberate exception: the
default `0` means "from the beginning" rather than "after sequence zero", so a
publisher that legitimately numbered its first event `0` does not have it
swallowed by the default argument.

Do not confuse `sequence` with the SSE `id:` line. The SSE cursor is a
*bus* cursor, global to the workspace and unrelated to any run's sequence
numbers — see [architecture.md](architecture.md#the-sse-bus).

## Idempotency

Two independent mechanisms, at two granularities.

### Per event: `UNIQUE(source, event_id)`

The `events` table carries `UNIQUE(source, event_id)`. Before applying a batch,
`SqlEventRepository._existing_keys` selects the `(source, id)` pairs that are
already stored and skips those events, counting them as `ignored`. Reposting is
therefore accepted and is a no-op *by construction*, not by an endpoint
remembering to check.

`id` alone cannot be the key: a CloudEvents `id` is only required to be unique
within its `source`, so two publishers are entitled to both call their first
event `1`. The pair is what is unique, and `DomainEvent.dedupe_key()` renders it
as `f"{source}\x1f{id}"`.

The canonical-JSON adapter leans on this deliberately: it derives event ids as
`uuid5(TEST_HUB_NAMESPACE, f"{source}\x1f{run_id}\x1f{sequence}\x1f{type}")`
rather than randomly, so importing the same file twice against the same
`run.id` applies zero events. Here is that happening, against a report whose
`run` block declares an `id`:

```console
$ test-hub ingest checkout-with-id.json --project storefront-web --type e2e --json   # first
{'run_id': 'b2f4a6d8-…', 'created': True,  'events_applied': 12, 'events_ignored': 0,  'attempts_imported': 5}  total 4

$ test-hub ingest checkout-with-id.json --project storefront-web --type e2e --json   # again
{'run_id': 'b2f4a6d8-…', 'created': False, 'events_applied': 0,  'events_ignored': 12, 'attempts_imported': 0}  total 4
```

Twelve events ignored, no counts moved, same run.

**The qualifier matters.** A report with no `run.id` gets a fresh random run id
on every import, and therefore fresh event ids, and therefore becomes a second
run. If you want repeatable imports, put a stable `id` in the run block or pass
`--run-id`.

### Per batch: `X-Idempotency-Key`

`POST /api/v1/runs/{id}/events` and `POST /api/v1/imports` both accept an
`X-Idempotency-Key` header. The first request stores the key and a summary of
what it did; a later request carrying the same key short-circuits before doing
any work and returns `applied: 0` with `ignored` equal to the size of the batch
it was handed.

```console
$ curl -sX POST "$HUB/api/v1/runs/$RUN/events" \
    -H 'content-type: application/json' -H 'X-Idempotency-Key: doc-batch-1' \
    -d @events.json
applied 5 ignored 0 counts.total 1 status running

$ # the same call again
applied 0 ignored 5 counts.total 1 status running
```

This is a different guarantee from the per-event one, and it is the one you want
for a retried CI step: it returns the original answer rather than doing the work
twice, and — crucially — rather than returning "0 applied" for a reason the
caller would read as failure.

> **Trap: the key is global, not scoped to a run.** `idempotency_keys` has
> `key` as its primary key, with no workspace or run in it. Posting a
> *different* batch to a *different* run under a key that has been used before
> silently discards the batch:
>
> ```
> run1 batch A -> applied 5 ignored 0
> run2 batch B -> applied 0 ignored 5    # nothing stored; run2 has 0 tests
> ```
>
> Derive the key from something unique per batch — the pipeline id and the batch
> number, say — rather than reusing a constant like `batch-1` across jobs.

`create_run` is idempotent on its own terms: `POST /api/v1/runs` with a
`run_id` that already exists returns the existing run unchanged instead of
erroring, so a retried "announce the run" step is safe without a key.

## The eight event types

The set is closed. Adding one is a wire-contract change and needs a
`SCHEMA_VERSION` bump, because dashboards and adapters both switch on it.

```
run.queued → run.started → suite.started* → test.started? → test.finished*
           → suite.finished* → artifact.published* → run.finished
```

Every example below is a real frame captured from the live SSE stream of a run
published with `curl`, with only whitespace added.

### `run.queued`

The run exists and is expected to start. It carries the whole of the run's
metadata, because for many publishers this is the only event sent before results
begin arriving and the dashboard should be able to draw the run — project,
branch, pipeline — while it is still empty.

| Field | Type | Notes |
|---|---|---|
| `project_key` | string | Required. |
| `name` | string or null | Human-readable run name. |
| `team` `component` | string or null | Ownership facets. |
| `test_type` | `TestType` | Defaults to `other`. |
| `trigger` | `TriggerType` | Defaults to `unknown`. |
| `context` | `RunContext` | Framework, language, branch, commit, pipeline, host. Every field optional. |
| `tags` | list of string | |
| `metadata` | object | Free-form. |
| `expected_tests` | int or null | Collected count, when the framework knows it up front. |

```json
{
  "specversion": "1.0",
  "id": "6667e61e-4c3c-40fa-9fc4-83a1c4293dda",
  "source": "urn:test-hub:api",
  "type": "run.queued",
  "time": "2026-09-08T21:33:53.575273Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 1,
  "data": {
    "project_key": "storefront-web",
    "name": null,
    "team": "payments",
    "component": "checkout",
    "test_type": "e2e",
    "trigger": "unknown",
    "context": {
      "framework": "playwright", "framework_version": null,
      "language": null, "language_version": null,
      "environment": "staging", "branch": "main",
      "commit_sha": null, "commit_message": null,
      "pipeline_id": null, "pipeline_url": null, "ci_provider": null,
      "hostname": null, "os": null, "executor": null
    },
    "tags": [],
    "metadata": {},
    "expected_tests": null
  }
}
```

### `run.started`

Execution has begun. `started_at` is the only required field; `project_key` and
`context` are there so that a publisher that skipped `run.queued` still gets a
usable run.

| Field | Type | Notes |
|---|---|---|
| `started_at` | timestamp | Required. |
| `project_key` | string or null | |
| `context` | `RunContext` or null | |

```json
{
  "specversion": "1.0",
  "id": "b24cdde1-f42d-48c6-9814-daadc88b3ae0",
  "source": "urn:test-hub:api",
  "type": "run.started",
  "time": "2026-09-08T21:33:53.575273Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 2,
  "data": {
    "started_at": "2026-09-08T21:33:53.575273Z",
    "project_key": "storefront-web",
    "context": null
  }
}
```

### `suite.started`

| Field | Type | Notes |
|---|---|---|
| `suite_id` | UUID | Required. The stable suite identity — `uuid5` over project and path, see [identity](schemas/README.md#identity). |
| `name` | string | Required. |
| `path` | string | Required. The grouping key; nested suites use `parent/child`. |
| `parent_id` | UUID or null | The parent suite's identity, for a tree. |
| `started_at` | timestamp or null | |
| `metadata` | object | |

```json
{
  "specversion": "1.0",
  "id": "cccccccc-0000-4000-8000-000000000001",
  "source": "urn:test-hub:ci",
  "type": "suite.started",
  "time": "2026-09-08T21:31:00Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 3,
  "data": {
    "suite_id": "ef54fbfe-82d2-5a49-b14f-8172db827cea",
    "name": "checkout",
    "path": "specs/checkout",
    "started_at": "2026-09-08T21:31:00Z"
  }
}
```

### `test.started`

Optional. Send it if you want a test to appear as `running` on the dashboard
while it executes; omit it and the test appears when it finishes. It creates the
attempt row with status `running`, and the matching `test.finished` updates that
same row rather than creating a second one.

| Field | Type | Notes |
|---|---|---|
| `suite_id` | UUID | Required. |
| `name` | string | Required. |
| `suite_path` | string | Required. |
| `test_id` | UUID or null | The stable test identity. Omit it and the server derives it from the identity components with `test_identity()`. |
| `class_name` `params` `full_name` `file` `owner` | string or null | Identity and display. `params` is the canonical normalized form. |
| `tags` `markers` | list of string | |
| `attempt_number` | int ≥ 1 | Defaults to 1. |
| `started_at` | timestamp or null | |
| `worker` | string or null | xdist worker or shard id. |

```json
{
  "specversion": "1.0",
  "id": "cccccccc-0000-4000-8000-000000000002",
  "source": "urn:test-hub:ci",
  "type": "test.started",
  "time": "2026-09-08T21:31:00Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 4,
  "data": {
    "test_id": "72b4ac40-333b-50f2-9519-7b0bad62c78c",
    "suite_id": "ef54fbfe-82d2-5a49-b14f-8172db827cea",
    "name": "retries a flaky redirect",
    "suite_path": "specs/checkout",
    "attempt_number": 1,
    "started_at": "2026-09-08T21:31:01Z"
  }
}
```

### `test.finished`

The event that carries a result. Everything from `test.started` plus the
outcome.

| Field | Type | Notes |
|---|---|---|
| *(all of `test.started`'s identity fields)* | | |
| `attempt_number` | int ≥ 1 | **A retry is a second `test.finished` with this incremented — never a correction of the first.** |
| `status` | `TestStatus` | Required. One of the ten canonical statuses. |
| `original_status` | string or null | Exactly what the source report called it, kept beside the normalized value. |
| `started_at` `finished_at` | timestamp or null | |
| `duration_ms` | int ≥ 0 or null | Derived from the timestamps when both are present and this is absent. |
| `failure` | `FailureDetail` or null | `type`, `message`, `stack_trace`, `expected`, `actual`, `truncated`. |
| `stdout` `stderr` `logs` | string or null | Redacted and truncated on the way into storage. |
| `skip_reason` | string or null | |
| `worker` | string or null | |
| `metadata` | object | |

Attempt 1, failing:

```json
{
  "specversion": "1.0",
  "id": "cccccccc-0000-4000-8000-000000000003",
  "source": "urn:test-hub:ci",
  "type": "test.finished",
  "time": "2026-09-08T21:31:00Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 5,
  "data": {
    "test_id": "72b4ac40-333b-50f2-9519-7b0bad62c78c",
    "suite_id": "ef54fbfe-82d2-5a49-b14f-8172db827cea",
    "name": "retries a flaky redirect",
    "suite_path": "specs/checkout",
    "attempt_number": 1,
    "status": "failed",
    "original_status": "FAILED",
    "duration_ms": 4100,
    "failure": {
      "type": "TimeoutError",
      "message": "navigation timed out after 4000ms"
    }
  }
}
```

Attempt 2, the retry that passed — same `test_id`, `attempt_number` 2:

```json
{
  "specversion": "1.0",
  "id": "cccccccc-0000-4000-8000-000000000004",
  "source": "urn:test-hub:ci",
  "type": "test.finished",
  "time": "2026-09-08T21:31:00Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 6,
  "data": {
    "test_id": "72b4ac40-333b-50f2-9519-7b0bad62c78c",
    "suite_id": "ef54fbfe-82d2-5a49-b14f-8172db827cea",
    "name": "retries a flaky redirect",
    "suite_path": "specs/checkout",
    "attempt_number": 2,
    "status": "passed",
    "duration_ms": 3900
  }
}
```

Those two events are the entire mechanism behind flakiness. The fold writes two
`attempts` rows, sets the test's effective status from the last one (`passed`),
and sets `flaky = true` because the attempts disagree. Overwriting attempt 1
instead would leave a green test and no evidence.

### `suite.finished`

| Field | Type | Notes |
|---|---|---|
| `suite_id` | UUID | Required. |
| `finished_at` | timestamp or null | |
| `duration_ms` | int or null | |

There is nowhere to put suite-level console output — that is a known compromise
the JUnit adapter works around by lifting `<system-out>` at suite level onto
`run.finished`'s metadata with a `suite_output_lifted` warning.

```json
{
  "specversion": "1.0",
  "id": "cccccccc-0000-4000-8000-000000000005",
  "source": "urn:test-hub:ci",
  "type": "suite.finished",
  "time": "2026-09-08T21:31:00Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 7,
  "data": {
    "suite_id": "ef54fbfe-82d2-5a49-b14f-8172db827cea",
    "finished_at": "2026-09-08T21:31:09Z",
    "duration_ms": 9000
  }
}
```

### `artifact.published`

The bytes travel over `POST /api/v1/runs/{id}/artifacts`; this event records the
fact and the content address. The digest is the storage key, so the same
screenshot uploaded from fifty shards is one stored object.

| Field | Type | Notes |
|---|---|---|
| `artifact_id` | UUID | Required. |
| `name` | string | Required. A label, never a path. |
| `kind` | `ArtifactKind` | `screenshot`, `video`, `log`, `trace`, `report`, `coverage`, `raw_report`, `other`. |
| `content_type` | string | Defaults to `application/octet-stream`. |
| `size_bytes` | int ≥ 0 | Required. |
| `sha256` | 64 hex chars | Required, pattern-checked. |
| `test_id` `attempt_id` | UUID or null | What it belongs to. |
| `url` | string or null | Where the API serves it. |
| `metadata` | object | |

```json
{
  "specversion": "1.0",
  "id": "063f1393-f05b-421b-ab8a-11a6771cbbe7",
  "source": "https://test-hub.dev/source/unknown",
  "type": "artifact.published",
  "time": "2026-09-08T21:34:07.264534Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 9,
  "data": {
    "artifact_id": "ce19dcb6-a455-40c0-b8eb-913928a4fd31",
    "name": "th-shot.png",
    "kind": "screenshot",
    "content_type": "image/png",
    "size_bytes": 17,
    "sha256": "827be73352f5c66d5bda4888a72f04ac62cf36a748c74fe832e3576c32768a9d",
    "test_id": "72b4ac40-333b-50f2-9519-7b0bad62c78c",
    "attempt_id": null,
    "url": "/api/v1/artifacts/ce19dcb6-a455-40c0-b8eb-913928a4fd31",
    "metadata": {}
  }
}
```

That `source` is the default, not `urn:test-hub:api` like the other
server-generated events in the same run: `RunService.add_artifact` builds its
event without passing a source. It is cosmetic — dedupe is on the pair, and the
pair is still unique — but it does mean a run's log has two sources in it where
you might expect one.

### `run.finished`

| Field | Type | Notes |
|---|---|---|
| `finished_at` | timestamp | Required. |
| `status` | `RunStatus` or null | **Advisory.** Honoured only to mark a run `cancelled`; everything else is folded from the attempts. |
| `duration_ms` | int or null | Also advisory — wall clock from `started_at` wins whenever it can be computed. |
| `metadata` | object | |

```json
{
  "specversion": "1.0",
  "id": "857d9349-b81b-4268-9a56-27a88525a64d",
  "source": "urn:test-hub:api",
  "type": "run.finished",
  "time": "2026-09-08T21:33:53.636277Z",
  "datacontenttype": "application/json",
  "subject": null,
  "schemaversion": 1,
  "workspaceid": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "runid": "df5092df-f0ff-4084-90b5-1e5dc446664f",
  "sequence": 8,
  "data": {
    "finished_at": "2026-09-08T21:33:53.636254Z",
    "status": null,
    "duration_ms": null,
    "metadata": {}
  }
}
```

## Batches

`EventBatch` — the body of `POST /api/v1/runs/{id}/events` — is
`{"events": [...]}` with between 1 and 5000 events, all belonging to the same
run. The API additionally checks that every event's `runid` matches the run id
in the URL and that every `workspaceid` matches the server's workspace, so a
file cannot write into another tenant and a copy-pasted URL cannot silently
scatter events across runs.

A batch is applied **atomically**: it lands entirely or not at all, so a
partially applied batch can never leave the counts half-updated. The whole batch
runs inside one `SqlUnitOfWork`, and `_refold` rewrites the run's derived numbers
once at the end rather than incrementally — incremental counters drift, and an
event applied twice or a shard arriving out of order leaves them wrong in a way
nobody notices until two pages disagree.

The batch ceiling is `Settings.max_events_per_batch` (default 5000) as well as
the model's own `max_length`; exceeding it is a `validation_error`, not a
truncation.

## A full run, end to end

This is a complete run published with `curl` against a local server, including a
retry. Every line was executed.

```console
$ RUN=$(curl -sX POST "$HUB/api/v1/runs" -H 'content-type: application/json' \
    -d '{"project_key":"storefront-web","test_type":"e2e","team":"payments",
         "component":"checkout",
         "context":{"branch":"main","environment":"staging","framework":"playwright"}}' \
  | jq -r .id)
```

`POST /runs` emits `run.queued` (sequence 1) and `run.started` (sequence 2)
because `started` defaults to true.

```console
$ curl -sX POST "$HUB/api/v1/runs/$RUN/events" \
    -H 'content-type: application/json' -H 'X-Idempotency-Key: doc-batch-1' \
    -d @events.json
applied 5 ignored 0 counts.total 1 status running
```

`events.json` held five events with `"sequence": 0`, so the server allocated
3–7: `suite.started`, `test.started`, `test.finished` (attempt 1, failed),
`test.finished` (attempt 2, passed), `suite.finished`. Note `counts.total` is 1,
not 2 — two attempts, one test.

```console
$ curl -sX POST "$HUB/api/v1/runs/$RUN/finish" -H 'content-type: application/json' -d '{}'
final status passed duration_ms 61
counts {'total': 1, 'passed': 1, 'failed': 0, 'error': 0, 'skipped': 0,
        'xfailed': 0, 'xpassed': 0, 'cancelled': 0, 'unknown': 0,
        'running': 0, 'queued': 0, 'flaky': 1, 'retried': 1,
        'completed': 1, 'executed': 1, 'pass_rate': 100.0, 'failure_rate': 0.0}
```

`run.finished` gets sequence 8. The run is `passed` because the effective
attempt passed, and it is `flaky: 1, retried: 1` because attempt 1 did not. The
duration is 61 ms of wall clock, not the 8000 ms the two attempts reported —
see [metrics.md](metrics.md#durations).

Attaching a screenshot afterwards emits `artifact.published` at sequence 9:

```console
$ curl -sX POST "$HUB/api/v1/runs/$RUN/artifacts" \
    -F "file=@shot.png" -F "kind=screenshot" \
    -F "test_id=72b4ac40-333b-50f2-9519-7b0bad62c78c"
{"artifact": {"id": "ce19dcb6-…", "sha256": "827be733…", "size_bytes": 17, …},
 "deduplicated": false}
```

The full stream as it arrived on `GET /api/v1/events/stream`, with the bus
cursors the SSE `id:` lines carried:

| SSE `id:` | `sequence` | `type` | `source` |
|---|---|---|---|
| 1 | 1 | `run.queued` | `urn:test-hub:api` |
| 2 | 2 | `run.started` | `urn:test-hub:api` |
| 3 | 3 | `suite.started` | `urn:test-hub:ci` |
| 4 | 4 | `test.started` | `urn:test-hub:ci` |
| 5 | 5 | `test.finished` (attempt 1, failed) | `urn:test-hub:ci` |
| 6 | 6 | `test.finished` (attempt 2, passed) | `urn:test-hub:ci` |
| 7 | 7 | `suite.finished` | `urn:test-hub:ci` |
| 8 | 8 | `run.finished` | `urn:test-hub:api` |
| 9 | 9 | `artifact.published` | `https://test-hub.dev/source/unknown` |

The two columns line up here only because this workspace had no other traffic.
They are different counters and on a busy server they will not.

## Events in a file

A publisher that already speaks this contract can put a raw stream in a file and
import it, without an HTTP session:

```json
{"kind": "test-hub-events", "schema_version": 1, "events": [ … ]}
```

The `testhub-json` adapter validates each envelope and passes it through
unchanged, **preserving sequence numbers** — they are the file's statement about
ordering and the adapter has no better information. Missing `run_id`,
`workspace_id` and `source` are filled from the import request; a `workspace_id`
that disagrees with the importing workspace is overridden with a
`workspace_override` warning, because a file must never be able to write into
another tenant.

`tests/fixtures/canonical/events.json` is a complete worked example of the
shape.
