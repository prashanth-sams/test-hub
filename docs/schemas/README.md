# The canonical schemas

Everything that crosses a boundary — an HTTP response body, the JSON embedded in
an offline export, the output of an adapter — is one of the Pydantic v2 models in
[`test_hub/domain/models.py`](../../src/test_hub/domain/models.py). Database rows
are never serialized directly (ADR-0001 D3). That separation is what lets the
table layout change — a new index, a split table, PostgreSQL — without a version
bump, and it is why this document describes models rather than tables.

The event envelope is a separate contract with the same version number; see
[events.md](../events.md).

## `schema_version`

One integer, defined once in
[`test_hub/version.py`](../../src/test_hub/version.py):

```python
SCHEMA_VERSION = 1
```

It moves independently of the package version and only on a breaking change.
`test-hub --version` prints both:

```console
$ test-hub --version
test-hub 0.1.0 (canonical schema v1)
```

`GET /healthz` reports it too, which is the cheapest way for a consumer to find
out what a server speaks before sending anything:

```console
$ curl -s http://127.0.0.1:8000/healthz
{"status":"ok","version":"0.1.0","schema_version":1,"database":"ok",
 "workspace_id":"b037a4b3-53f8-5326-a5d5-f5fad3c31052","live_subscribers":0,
 "time":"2026-09-08T21:30:36.256254Z"}
```

### Which payloads carry it

Only the ones that are allowed to cross a version boundary on their own — a
top-level document that a consumer might have received without context:

| Carries `schema_version` | Does not |
|---|---|
| `TestRun`, `RunDetail`, `TestDetail`, `OverviewSnapshot`, `ImportResult`, `Snapshot`, `DomainEvent` (as `schemaversion`) | `Workspace`, `Project`, `RunSummary`, `RunCounts`, `TestSuite`, `TestCase`, `TestAttempt`, `Artifact`, `RunComparison`, `TestHistoryPoint`, `TrendPoint`, `ProjectHealth`, `ParserWarning`, `FailureDetail`, `RunContext` |

A `TestCase` is only ever seen inside a `RunDetail`, so stamping a version on it
would be noise on every one of ten thousand objects in the same document.

### The compatibility promise

Within a major schema version:

- **Fields may be added.** A new optional field on any model is not a breaking
  change.
- **Fields are not removed, renamed, or retyped.**
- **The meaning of a field does not change.** In particular the metric
  definitions in [metrics.md](../metrics.md) are part of the contract, not an
  implementation detail — changing what `pass_rate` counts is a version bump
  even though the field's name and type are untouched.
- **Enum members may be added.** `TestStatus`, `RunStatus`, `TestType`,
  `TriggerType` and `ArtifactKind` are closed sets *for this version*, but the
  honest reading is that a future minor release could add a member. Consumers
  should treat an unrecognized member as they would `unknown` / `other` rather
  than crashing.

Anything else — a removed field, a changed formula, a new *event type*, a
different envelope shape — is a `SCHEMA_VERSION` bump.

### What Test Hub itself does with a version it does not know

It refuses:

```python
>>> TestRun.model_validate({..., "schema_version": 2})
ValidationError: Value error, unsupported schema_version 2; this build speaks 1
```

End to end, importing a `test-hub-report` that declares version 2:

```console
$ test-hub ingest checkout-v2.json --project storefront-web
error: this build speaks Test Hub schema_version 1, but the document declares 2.
Upgrade Test Hub (a newer document) or re-export the report with a publisher
that targets version 1.
  document_schema_version: 2
  supported: 1
```

```console
$ curl -s -w '%{http_code}\n' -X POST $HUB/api/v1/imports \
    -F "file=@checkout-v2.json" -F "project=storefront-web"
422
{"error": "validation_error",
 "message": "this build speaks Test Hub schema_version 1, but the document declares 2. …",
 "detail": {"document_schema_version": 2, "supported": 1}}
```

The reasoning is stated in the adapter's own docstring and is worth repeating:
an older Test Hub reading a newer document cannot know which fields changed
meaning, and a wrong pass rate is worse than a failed import.

### What a consumer should do

The same, in the same direction, with one asymmetry:

- **Version is greater than you know: refuse, and say so.** You cannot know
  what changed. Failing loudly gives the operator something to act on; guessing
  gives them a dashboard that is quietly wrong.
- **Version is lower than you know: you may accept it** if you are prepared to
  supply defaults for fields that did not exist yet. Test Hub itself does not do
  this — there is only one version so far — but a consumer with a migration
  table can.
- **Version is absent: assume the version you were built for.** Every model
  defaults `schema_version` rather than requiring it, so a hand-written report
  that omits it is read as version 1 rather than rejected. That is a
  convenience for people writing JSON by hand, and it means a document with no
  version is *not* evidence of anything.

### Unknown fields are rejected, not ignored

Every canonical model is `extra="forbid"`:

```python
>>> RunCounts.model_validate({"total": 1, "passed": 1, "typo_field": 3})
ValidationError: ... typo_field ... Extra inputs are not permitted
```

This is the opposite of the usual advice about being liberal in what you accept,
and it is deliberate. A silently ignored `durationMs` produces a run with no
durations and no error; a 422 naming the field produces a five-second fix. The
one place the rule is relaxed is `AdapterInfo` in the API layer, which ignores
extra keys so that a third-party adapter describing itself with unfamiliar
fields cannot break `GET /api/v1/adapters` for everyone else.

### Round-tripping a response

Computed fields (`pass_rate`, `executed`, a test case's `status`) are written
out but derived, so `extra="forbid"` would normally reject a client that read a
response and validated it straight back into the same model. `_Base` drops
exactly the computed names — and nothing else — on the way in, so the round trip
works while a genuine typo is still an error:

```python
>>> counts = RunCounts(total=2, passed=1, failed=1)
>>> RunCounts.model_validate(counts.model_dump(mode="json")).pass_rate
50.0
```

## Generating the JSON Schema

The models are the source of truth; the JSON Schema is generated from them, so
there is no second artefact to keep in step:

```console
$ PYTHONPATH=src .venv/bin/python -c \
  "import json;from test_hub.domain.models import Snapshot;print(json.dumps(Snapshot.model_json_schema())[:200])"
{"$defs": {"Artifact": {"additionalProperties": false, "description": "A file produced by a run — a screenshot, a log, a trace, the raw report.\n\nStored content-addressed: ``sha256`` is the stor
```

`Snapshot`'s schema pulls in 25 definitions and is about 33 KB of JSON. Every
model's docstring becomes the `description`, so the generated schema carries the
same reasoning this document does.

**Ask for the serialization schema when you are describing a response.**
Pydantic's default is the *validation* schema, which omits computed fields —
correct for "what may I send", wrong for "what will I receive":

```python
>>> list(RunCounts.model_json_schema(mode="validation")["properties"])[-3:]
['queued', 'flaky', 'retried']
>>> list(RunCounts.model_json_schema(mode="serialization")["properties"])[-4:]
['completed', 'executed', 'pass_rate', 'failure_rate']
```

The OpenAPI document at `/api/openapi.json` is generated by FastAPI from the
same models and is the machine-readable form of the REST contract.

## Shared field types

Three aliases recur across every model and are worth learning once.

| Alias | Definition | Used for |
|---|---|---|
| `Slug` | 1–120 chars, `^[a-zA-Z0-9][a-zA-Z0-9._\- ]*$` | Project keys, teams, components, environments, tags |
| `ShortText` | ≤ 512 chars | Names, paths, class names, single-line free text |
| `LongText` | ≤ 64 000 chars | Stack traces, stdout, stderr, logs |

`Slug` is deliberately permissive about case and spaces, because real team names
have both, and deliberately strict about everything else, because these values
end up in filenames, URLs and SQL `LIKE` patterns. It is not a
lowercase-hyphenated slug in the blog-URL sense.

Event payloads type these same fields as bare `str`, which means a value can
pass event validation and still be unstorable. Two layers deal with that, in
opposite directions and on purpose: the **adapters** drop a bad slug with an
`invalid_slug` warning, because a file should not be able to invent a team name;
the **storage layer** repairs one with `coerce_slug`, replacing disallowed
characters with `-`, because a run that has already been accepted should not be
lost over a slash in a team name.

`LongText` has a second ceiling above it: `Settings.max_text_field_bytes`
(default 256 KiB). The smaller of the two wins, truncation sets
`FailureDetail.truncated` or a `text_truncated` flag in the attempt's metadata,
and redaction runs *before* truncation so a cut cannot leave the tail of a
secret behind.

Every timestamp is an `AwareDatetime` in UTC. A naive datetime is a validation
error everywhere, including on the way out of the database — `UtcDateTime` in
the ORM stores a fixed-width ISO-8601 string precisely so that pysqlite cannot
hand back a naive value.

## The hierarchy

```
Workspace
  └── Project
        └── TestRun                one execution of a set of tests
              ├── TestSuite        a file / class / feature grouping
              │     └── TestCase   a test's identity within that run
              │           └── TestAttempt   one execution of it (retries!)
              ├── Artifact         a file the run produced
              └── (events)         the append-only log it was folded from
```

### `Workspace`

The tenancy boundary. The open-source edition creates exactly one, `default`,
and gives it no UI — but every row in the database carries its id from the first
migration (ADR-0001 D9), because adding a tenancy column to a populated events
table later is the migration nobody wants to write.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | `uuid5(TEST_HUB_NAMESPACE, "workspace\x1f" + slug.lower())`. |
| `slug` | `Slug` | |
| `name` | `ShortText` | |
| `created_at` | timestamp | |

The id is derived, not random, so `default` is the same UUID on every
installation — `b037a4b3-53f8-5326-a5d5-f5fad3c31052`. That is what lets an
offline publisher compute workspace-scoped identities without asking the server.
If an existing database disagrees, the database wins and a warning is logged.

### `Project`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `workspace_id` | UUID | |
| `key` | `Slug` | The stable, human-typed key publishers send: `payments-api`. |
| `name` | `ShortText` | |
| `description` | `ShortText` or null | |
| `default_team` | `Slug` or null | |
| `repository_url` | URL or null | |
| `tags` | list of `Slug`, ≤ 64 | |
| `created_at` `updated_at` | timestamp | |

Projects are created implicitly on first import. `key` is what a publisher
sends; `id` is internal.

### `RunCounts`

Thirteen stored integers and four computed ones. The full definition, including
why `xfailed` counts as a pass and why `pass_rate` is `None` rather than `0.0`,
is in [metrics.md](../metrics.md) — the short version:

| Field | Type | Notes |
|---|---|---|
| `total` | int | Tests folded into this block; equals the sum of the ten status fields. |
| `passed` `failed` `error` `skipped` `xfailed` `xpassed` `cancelled` `unknown` `running` `queued` | int | One per `TestStatus`, counting effective attempts. |
| `flaky` | int | Tests flaky within this run. Overlaps the status counts on purpose. |
| `retried` | int | Tests with more than one attempt. Likewise. |
| `completed` | int, computed | Everything but `running` and `queued`. |
| `executed` | int, computed | `completed` minus `skipped` and `cancelled`. |
| `pass_rate` | float or null, computed | `100 × (passed + xfailed) / executed`, 2 dp. |
| `failure_rate` | float or null, computed | `100 × (failed + error + xpassed) / executed`, 2 dp. |

### `RunContext`

Where a run came from. Every field is optional, because publishers know varying
amounts, and the ones the dashboard filters on are stored as indexed columns
rather than inside a JSON blob.

`framework`, `framework_version`, `language`, `language_version`, `environment`
(`Slug`), `branch`, `commit_sha` (≤ 64 chars), `commit_message`, `pipeline_id`,
`pipeline_url` (URL), `ci_provider`, `hostname`, `os`, `executor` — all
`ShortText` or null unless noted.

### `TestRun`

The central object.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | |
| `id` | UUID | Chosen by the publisher or the server. |
| `workspace_id` | UUID | |
| `project_key` | `Slug` | |
| `name` | `ShortText` or null | |
| `team` `component` | `Slug` or null | |
| `test_type` | `TestType` | `unit`, `component`, `integration`, `contract`, `functional`, `e2e`, `api`, `ui`, `performance`, `security`, `accessibility`, `smoke`, `regression`, `acceptance`, `other`. |
| `trigger` | `TriggerType` | `manual`, `push`, `pull_request`, `schedule`, `api`, `retry`, `local`, `unknown`. |
| `status` | `RunStatus` | `queued`, `running`, `passed`, `failed`, `error`, `cancelled`, `unknown`. Derived by folding — see [metrics.md](../metrics.md#where-a-runs-status-comes-from). |
| `context` | `RunContext` | |
| `started_at` `finished_at` | timestamp or null | |
| `duration_ms` | int ≥ 0 or null | Wall clock when both ends are known. |
| `counts` | `RunCounts` | |
| `tags` | list of `Slug`, ≤ 64 | |
| `metadata` | object | Free-form. Adapters put the original free-text test type here as `test_type_raw` when they had to fall back to `other`. |
| `warnings` | list of `ParserWarning`, ≤ 1000 | What the import could not represent exactly. Appended, not replaced, so a run built from several shards keeps them all. |
| `source_adapter` | `ShortText` or null | Which adapter parsed it, when it came from a file. |
| `created_at` `updated_at` | timestamp | |
| `is_live` | bool, computed | True while `status` is `queued` or `running`. |

### `RunSummary`

The compact form used in listings — no suites, no tests, no stack traces. It
flattens the four `RunContext` facets a list needs (`environment`, `branch`,
`commit_sha`, `framework`, `language`, `pipeline_url`) onto the top level and
carries `is_live` as a plain boolean rather than a computed field. It exists so
that a page of fifty runs is a few kilobytes rather than a few megabytes.

### `TestSuite`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | The **stable suite identity**, not a row id: `uuid5(ns, "suite\x1f" + project + "\x1f" + path)`. Two runs of the same suite share it. |
| `run_id` | UUID | |
| `name` `path` | `ShortText` | |
| `parent_id` | UUID or null | Another suite's identity, for a tree. |
| `started_at` `finished_at` | timestamp or null | |
| `duration_ms` | int ≥ 0 or null | |
| `counts` | `RunCounts` | Folded from the suite's tests at read time, not stored. |
| `metadata` | object | |

### `TestCase`

A test's identity within one run, plus every attempt it made.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | The **stable test identity**. The same value in every run that executed this test — history, comparison and flakiness are keyed on it. |
| `run_id` `suite_id` | UUID | |
| `name` | `ShortText` | |
| `class_name` | `ShortText` or null | |
| `suite_path` | `ShortText` | |
| `params` | `ShortText` or null | The canonical normalized parameter string. |
| `full_name` | `ShortText` or null | Identifier as the framework prints it. |
| `file` `owner` | `ShortText` or null | |
| `tags` | list of `Slug`, ≤ 64 | |
| `markers` | list of `ShortText`, ≤ 64 | |
| `attempts` | list of `TestAttempt` | In order. |
| `metadata` | object | |
| `status` | `TestStatus`, computed | The **last** attempt's status. `unknown` when there are no attempts. |
| `duration_ms` | int, computed | **Sum across every attempt.** |
| `flaky` | bool, computed | Both succeeded and failed within this run. |

### `TestAttempt`

One execution of one test. A retry produces a new attempt; nothing in Test Hub
ever overwrites one.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | Row identity, not stable across runs. |
| `test_id` `run_id` | UUID | |
| `attempt_number` | int ≥ 1 | |
| `status` | `TestStatus` | |
| `original_status` | `ShortText` or null | Exactly what the source report called it. Always kept. |
| `started_at` `finished_at` | timestamp or null | |
| `duration_ms` | int ≥ 0 or null | Derived from the timestamps when both exist and this does not. |
| `failure` | `FailureDetail` or null | |
| `stdout` `stderr` `logs` | `LongText` or null | |
| `skip_reason` | `ShortText` or null | |
| `worker` | `ShortText` or null | xdist worker or shard. |
| `artifact_ids` | list of UUID, ≤ 256 | |
| `metadata` | object | |

`FailureDetail` is `type`, `message`, `stack_trace`, `expected`, `actual` (all
`LongText` except `type`, which is `ShortText`) plus `truncated: bool` — set
when the message or trace was cut to fit the limits, so a shortened stack trace
never pretends to be complete.

### `Artifact`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `run_id` | UUID | |
| `test_id` `attempt_id` | UUID or null | |
| `kind` | `ArtifactKind` | `screenshot`, `video`, `log`, `trace`, `report`, `coverage`, `raw_report`, `other`. |
| `name` | `ShortText` | A display label. **Never a path** — the store builds paths only from the digest. |
| `content_type` | `ShortText` | |
| `size_bytes` | int ≥ 0 | |
| `sha256` | 64 lowercase hex, pattern-checked | The storage key. Uploading the same screenshot from a hundred shards costs one copy. |
| `url` | `ShortText` or null | Where the API serves it. |
| `created_at` | timestamp | |
| `metadata` | object | |

### `ParserWarning`

Something the input said that could not be represented exactly. Raised whenever
an adapter has to guess, drop or coerce, and never swallowed: the import
response carries every one and the dashboard shows them on the run.

| Field | Type | Notes |
|---|---|---|
| `code` | `^[a-z0-9_.]+$`, ≤ 64 | Stable and machine-readable: `unknown_status`, `invalid_slug`, `text_truncated`, `counts_mismatch`, `attempt_renumbered`, `naive_timestamp`, `workspace_override`, … |
| `message` | `ShortText` | |
| `severity` | `info` or `warning` | |
| `location` | `ShortText` or null | Where in the input: `testsuite[2]/testcase[7]`, `tests[3].attempts[1]`. |
| `detail` | object | |

Warnings are bounded at 500 per parse; past the cap the collector counts what it
drops and appends a `warnings_truncated` note, so a pathological file produces a
bounded response.

### `RunComparison`

A run measured against the previous **comparable** run: same project, team,
component, test type, environment and branch. That is the narrowest slice that
makes the comparison honest — comparing a nightly regression run on `main`
against a smoke run on a feature branch produces deltas that look like
regressions and are not.

`baseline_run_id`, `baseline_started_at`, `pass_rate_delta`,
`duration_delta_ms`, `total_delta`, and five lists of stable test ids:
`newly_failing`, `newly_passing`, `still_failing`, `added`, `removed`. Every
field is null or empty when there is no comparable predecessor.

Note that `newly_failing`, `newly_passing` and `still_failing` are computed over
the *intersection* of the two runs' test ids, so a test that appeared for the
first time and failed is in `added`, not `newly_failing`.

### `TestDetail` and `TestHistoryPoint`

Everything known about one stable test identity, across runs: identity fields,
the `latest` `TestCase`, a `history` of `TestHistoryPoint` (run id, start,
status, duration, attempt count, branch, environment) newest first, and three
derived numbers — `flake_rate`, `failure_rate` and `average_duration_ms`. All
three are defined in [metrics.md](../metrics.md); the important thing here is
that `TestDetail.failure_rate` and `RunCounts.failure_rate` are different
quantities over different denominators.

### `OverviewSnapshot`

The portfolio view. Deliberately separate from `RunDetail`: the overview is the
executive-altitude answer and must stay cheap enough to poll, while the run
detail is the engineering drill-down and is allowed to be large.

| Field | Type | Notes |
|---|---|---|
| `generated_at` | timestamp | |
| `window_days` | int | |
| `counts` | `RunCounts` | Merged across every run in the window, then the rates computed — so they are test-weighted. |
| `total_runs` | int | Runs that started inside the window. |
| `total_runs_all_time` | int | Regardless of the window. When this is non-zero and `total_runs` is zero, every run is older than the window, and the dashboard says so rather than showing an empty state that looks like an empty installation. |
| `live_runs` | list of `RunSummary` | Queued or running. |
| `recent_runs` | list of `RunSummary` | Newest regardless of the window, so a quiet workspace still has something to show. |
| `projects` | list of `ProjectHealth` | |
| `trend` | list of `TrendPoint` | |
| `top_failing_tests` `flakiest_tests` | list of `TestDetail` | Ten each. |
| `filters` | object of string → list of string | Distinct values per facet, for the dropdowns. |

`TrendPoint` is `bucket` (an ISO date), `runs`, `pass_rate`, `failure_rate`,
`duration_ms_p50`, `duration_ms_p95`, `total` and `failed` (which is
`failed + error`). Buckets with no runs are still emitted, so a chart shows a
genuine gap rather than joining two distant points with a line implying
continuity, and the bucket *widens* past 60 points rather than the list growing
— a year of daily points would be 366 objects in every overview response for a
chart 700 pixels wide.

`ProjectHealth` is one line per project: `project_key`, `name`, `team`,
`runs_24h`, `last_run_id`, `last_run_at`, `last_status`, `pass_rate`,
`pass_rate_delta` (against the equally long window immediately before this one),
`flaky_tests` and `live_runs`.

### `ImportResult`

What every import returns, whether it came from the CLI, the SDK or REST.

| Field | Type | Notes |
|---|---|---|
| `run_id` `project_key` | UUID / `Slug` | |
| `adapter` `format` | `ShortText` | Which parser was chosen. |
| `detection_confidence` | float or null | `1.0` when `--format` was explicit — there was nothing to guess. |
| `created` | bool | False when the import updated an existing run. |
| `counts` | `RunCounts` | Read back from the run after folding. |
| `suites_imported` `tests_imported` `attempts_imported` | int | Touched by this import. |
| `events_applied` `events_ignored` | int | The idempotency signal. |
| `warnings` | list of `ParserWarning` | |
| `duration_ms` | int or null | How long the import took. |

### `Snapshot`

The complete frozen payload embedded in a standalone HTML export, and exactly
what `SnapshotDataProvider` reads in the browser.

| Field | Type | Notes |
|---|---|---|
| `kind` | `"test-hub-snapshot"` | Literal discriminator. |
| `generated_at` | timestamp | |
| `generator` `generator_version` | `ShortText` | `"test-hub"` and the package version. |
| `title` | `ShortText` | |
| `workspace` | `Workspace` | |
| `projects` | list of `Project` | |
| `overview` | `OverviewSnapshot` | |
| `runs` | list of `RunDetail` | Each with its own comparison. |
| `tests` | list of `TestDetail` | |

It is a superset of what the live API returns for the same data: the two
providers obtain the same shapes by different means. Note the `kind` value —
`test-hub-snapshot` — is *not* one of the two `kind` values the JSON adapter
accepts on import. An export is a report to read, not a report to re-ingest.

## Identity

Three identities are derived rather than assigned, which is what makes a
publisher in another language able to compute them without ever talking to the
server. They are `uuid5` in a fixed namespace:

```python
TEST_HUB_NAMESPACE = uuid.UUID("4c8f4c4d-3ecb-5f78-a1e4-c1a24e07b1e6")
```

The namespace is hard-coded and must never move: every historical identity would
change with it.

| Identity | Hashed input |
|---|---|
| workspace | `"workspace" \x1f slug.lower()` |
| suite | `"suite" \x1f project \x1f suite_path` |
| test | `project \x1f suite_path \x1f class_name \x1f test_name \x1f params` |

`\x1f` is the ASCII unit separator, chosen because it cannot appear in any of
the fields. `identity_source()` is exported and documented precisely so another
implementation can reproduce the string byte for byte.

A test's identity is derived from *what the test is*, never from *when or where
it ran*:

| In the identity | Deliberately excluded |
|---|---|
| project key, suite path, class name, test name, normalized params | timestamps, line numbers, file offsets, durations, worker/shard id, hostname, run id, build number, branch |

Before hashing, each component is normalized: Unicode NFC, whitespace collapsed,
suite paths converted to forward slashes with a leading `./` stripped. Parameter
values are rendered with volatile fragments scrubbed — `<Foo object at
0x7f3c…>` becomes `object at 0x?`, a bare UUID becomes `<uuid>`, a long hex
literal becomes `0x?` — because a framework that renders objects into a
parametrized id would otherwise invent a new test on every run. They are
replaced with a stable token rather than dropped, so two genuinely different
parameters do not collide.

Named parameters are sorted by name, so `(b=2, a=1)` and `(a=1, b=2)` are one
test. Positional parameters keep their order, because position *is* the name.

```python
>>> normalize_params({"b": 2, "a": 1})
'a=1|b=2'
>>> normalize_params(["chrome", 1024])
'0=chrome|1=1024'
>>> test_identity(project="p", suite_path="tests/t.py", test_name="x") == \
...     test_identity(project="p", suite_path="./tests/t.py", test_name=" x ")
True
```

## Writing a report by hand

If you are producing JSON for Test Hub rather than consuming it, the format is
[canonical-report.md](canonical-report.md), with a complete worked example that
imports cleanly.
