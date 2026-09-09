# REST API

Everything Test Hub does is reachable over plain HTTP. There is no required
client library — the Python SDK in `test_hub.sdk` and the `test-hub publish`
command are conveniences built on top of the same endpoints, and `curl` is a
first-class publisher. If your CI system can make an HTTP request, it can
publish results.

Every example below was run against a live server started with:

```shell
$ export TEST_HUB_DATA_DIR=/tmp/th-docs
$ test-hub demo
$ test-hub serve --port 8123
```

and the responses are the real ones, only trimmed where a long list adds
nothing (an elision is always marked `…`).

## Conventions

| | |
|---|---|
| Base path | `/api/v1`. `/healthz` sits outside it on purpose — see below. |
| Request bodies | JSON, except `POST /imports` and `POST /runs/{id}/artifacts`, which are `multipart/form-data` so a file can stream instead of being base64'd into a string. |
| Unknown fields | Rejected with 422. Every request model is `extra="forbid"`, so a typo in a field name is an error rather than a silently ignored value. |
| Timestamps | RFC 3339 with an offset, on the way in and on the way out. A naive datetime is a 422; Test Hub does not guess your timezone. |
| Ids | UUIDs. Run ids may be chosen by the publisher; test and suite ids are *derived* (see [Adapters](adapters.md#identity)) and must not be invented. |
| Versioning | Every response carries `schema_version` (currently `1`). A breaking change to the wire shape bumps it. |
| OpenAPI | `/api/openapi.json`, with Swagger UI at `/api/docs` and ReDoc at `/api/redoc`. |
| CORS | Off unless `TEST_HUB_CORS_ALLOW_ORIGINS` is set. Browser code on another origin cannot reach this API by default. |

## Authentication

Authentication is off by default, because the default bind is `127.0.0.1` and
nothing else can reach it. It switches itself on the moment tokens are
configured, and binding to a non-loopback interface without it is refused
outright — see [Security](security.md#binding).

Create a token on any machine that has Test Hub installed:

```shell
$ test-hub token create --label ci

  Token (shown once — copy it now)
  ths_930NYCSZN-jva--X3gJWFk2Sst-esPPNs7O8rsmQ-P4

  Configure the server with the hash, not the token:
  export TEST_HUB_API_TOKEN_HASHES="pbkdf2_sha256$120000$a98cad7071abf1fd43739cf0827bbb8a$74872c1d8e0b249632f78199d9a9d23900e03ae7bbe16775585101177d96d214"

  Publishers use the token itself:
  export TEST_HUB_TOKEN="ths_930NYCSZN-jva--X3gJWFk2Sst-esPPNs7O8rsmQ-P4"   # label: ci
```

Two header forms are accepted, and they are the same token checked the same way:

```shell
-H "Authorization: Bearer $TEST_HUB_TOKEN"
-H "X-API-Key: $TEST_HUB_TOKEN"
```

`X-API-Key` exists because a surprising number of CI systems make it awkward to
set an `Authorization` header on a generic HTTP step. Prefer `Bearer` when you
have the choice.

**Writes are guarded; reads are not.** With authentication on, `POST` endpoints
require a token and `GET` endpoints do not, because the common arrangement is
"anyone on the team may look, only CI may write". Verified against a server with
one token hash configured:

```shell
$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8125/api/v1/runs
200
$ curl -s -X POST http://127.0.0.1:8125/api/v1/imports -F file=@results.xml -F project=p
{"error":"unauthorized","message":"a valid bearer token is required; send it as `Authorization: Bearer <token>` or `X-API-Key: <token>`"}
```

Set `TEST_HUB_REQUIRE_AUTH_FOR_READS=true` to close the reads too. A 401 always
carries `WWW-Authenticate: Bearer`, and the message is identical for a missing,
malformed and wrong token — telling a caller which of those it was is free
information for an attacker.

## Errors

Every failure — validation, auth, adapter, storage — comes back in one shape, so
a client needs exactly one error parser:

```json
{
  "error": "unknown_format",
  "message": "no installed adapter recognised this input (…)",
  "detail": {"installed_adapters": [ … ]}
}
```

`detail` is **omitted when it is empty**, so treat it as optional:

```shell
$ curl -s http://127.0.0.1:8123/api/v1/runs/00000000-0000-0000-0000-000000000000
{"error":"not_found","message":"run 00000000-0000-0000-0000-000000000000 does not exist"}
```

`error` is a stable machine-readable code. Branch on it, not on the message.

| `error` | HTTP | CLI exit | Means |
|---|---|---|---|
| `validation_error` | 422 | 2 | The body was well-formed JSON but did not match the schema, or said something impossible. |
| `adapter_error` | 422 | 2 | An adapter recognised the input and then could not parse it. Also what a refused DTD produces. |
| `unknown_format` | 415 | 2 | No installed adapter recognised the input. `detail.installed_adapters` lists what would have worked. |
| `not_found` | 404 | 3 | No such run, test or artifact. |
| `conflict` | 409 | 4 | A write contradicted a write that already landed. |
| `unauthorized` | 401 | 5 | Missing, malformed or wrong token. |
| `forbidden` | 403 | 5 | Reserved; the first release has authentication but no per-user authorization. |
| `payload_too_large` | 413 | 6 | The upload exceeded `TEST_HUB_MAX_REPORT_BYTES` or `TEST_HUB_MAX_ARTIFACT_BYTES`. |
| `configuration_error` | 500 | 78 | The server was asked to start in a state that is not coherent or not safe. 78 is `EX_CONFIG`, so a supervisor can tell it apart from a crash. |

The exit-code column is what `test-hub ingest` returns for the same condition —
the taxonomy is mapped to HTTP in one place and to an exit code in one place, so
the two never drift.

FastAPI's own validation failures are rewritten into the same shape rather than
its default `{"detail": [...]}`:

```shell
$ curl -s -X POST http://127.0.0.1:8123/api/v1/runs \
    -H 'content-type: application/json' -d '{"project_key":"p","nonsense":1}'
{"error":"validation_error","message":"the request body did not match the expected schema","detail":{"errors":[{"type":"extra_forbidden","loc":["body","nonsense"],"msg":"Extra inputs are not permitted","input":1}]}}
```

## Idempotency

CI steps get retried, networks time out after the server already committed, and
a publisher that cannot retry safely is a publisher that loses data. Three
mechanisms, in increasing order of coverage:

1. **`(source, id)` on every event is unique.** Repost an event and it is
   ignored. This is enforced by the store, not by an endpoint, so it holds for
   file imports and live publishing alike.
2. **`run_id` on `POST /runs` and `POST /imports`.** Supplying one makes the
   call idempotent at the run level: a retried step announces the same run
   instead of creating a second.
3. **`X-Idempotency-Key` on `POST /imports` and `POST /runs/{id}/events`.**
   Covers a whole batch. The first call's result is remembered; a repost returns
   `applied: 0` and changes nothing.

Reposting a batch of events under the same key, verified:

```shell
$ curl -s -X POST "$HUB/api/v1/runs/$RUN/events" \
    -H 'content-type: application/json' -H 'X-Idempotency-Key: batch-1' -d @events.json
{"run_id":"4ae82e39-…","applied":3,"ignored":0,"created_run":false,"counts":{…},"status":"running"}

$ # exactly the same request again
{"run_id":"4ae82e39-…","applied":0,"ignored":3,"created_run":false,"counts":{…},"status":"running"}
```

Note the counts are identical across the two calls. That is the property worth
testing in your own pipeline.

Three traps, all of them real:

- **An idempotency key is global, not scoped to a run.** It is stored by the key
  string alone. Reusing `batch-1` for a *different* batch returns the first
  batch's answer and applies nothing. Derive keys from something unique —
  `"$CI_JOB_ID-$SHARD-$ATTEMPT"` — and never from a constant.
- **Events without an `id` are not self-deduplicating.** `id` defaults to a
  fresh UUIDv4 when you omit it, so two posts of the same JSON body are two
  different events. Either send your own stable `id` per event, or send
  `X-Idempotency-Key`. The batch above relies on the header.
- **On a replayed import the returned `run_id` is not the stored run.**
  `POST /imports` with a repeated `X-Idempotency-Key` correctly applies nothing,
  but the `ImportResult` it returns carries the run id this attempt *would*
  have created and `created: true`, not the id of the run that actually exists.
  Verified:

  ```shell
  $ curl -sX POST … -H 'X-Idempotency-Key: demo-key-1' -F file=@simple.xml -F project=idem-test
  run_id 67332d8c-…  created True   events_applied 10  events_ignored 0
  $ # same file, same key
  run_id d1ad6c00-…  created True   events_applied 0   events_ignored 10
  $ curl -s "$HUB/api/v1/runs?project=idem-test"
  total runs: 1   → 67332d8c-…
  ```

  The store is right and the response is misleading. Until that is fixed, pass
  an explicit `run_id` on an import you intend to retry, and trust
  `events_applied == 0` rather than `run_id` as the "this was a replay" signal.

## Pagination

Only `GET /api/v1/runs` pages. `limit` (1–500, default 50) and `offset` (≥ 0),
with `total` in the body so a client can tell whether there is more:

```shell
$ curl -s "$HUB/api/v1/runs?limit=1&offset=2" | jq '{total, limit, offset, returned: (.runs|length)}'
{"total": 37, "limit": 1, "offset": 2, "returned": 1}
```

Everything else is bounded by its own parameter instead: `GET /tests/{id}` takes
`history` (1–200, default 30), `GET /overview` takes `window_days` (1–365,
default 30), and `GET /runs/{id}` returns the whole run — every suite, test and
attempt — with no way to page it. That last one is a deliberate altitude
decision, not an oversight; see [Scaling](scaling.md) for where it stops being
free.

## Filters

`GET /runs` and `GET /overview` share one filter dependency, so a dashboard can
carry a user's selection from one view to the other without translating it.
Every list parameter accepts both repetition and a comma-separated list —
`?team=a&team=b` and `?team=a,b` are the same query.

| Parameter | Type | Matches |
|---|---|---|
| `project` | list | Run's project key. |
| `team` | list | Owning team. |
| `component` | list | Component or service under test. |
| `type` | list | Test type: `unit`, `component`, `integration`, `contract`, `functional`, `e2e`, `api`, `ui`, `performance`, `security`, `accessibility`, `smoke`, `regression`, `acceptance`, `other`. |
| `environment` | list | Environment the tests ran against. |
| `branch` | list | Source branch. |
| `status` | list | Run status: `queued`, `running`, `passed`, `failed`, `error`, `cancelled`, `unknown`. Anything else is a 422. |
| `tag` | list | Run tag. |
| `search` | string, ≤ 200 chars | Substring of run name, project key, branch, commit SHA, commit message or pipeline id. Case-sensitive `LIKE`, with `%` and `_` escaped. |
| `live` | bool | `true` restricts to runs that are queued or running. |
| `limit` / `offset` | int | Paging, `/runs` only. |

Real results against the demo data set:

```
team=payments                      total=14
status=failed                      total=10
type=e2e,unit                      total=16
branch=main&environment=staging    total=12
live=true                          total=1
search=basket                      total=7
```

There is **no date filter on the API**. `RunFilter` has `since` and `until`
fields and the repository honours them, but `build_filter` does not expose them
as query parameters, so the only time window you can ask for over HTTP is
`GET /overview?window_days=`.

Use `GET /api/v1/facets` to populate filter dropdowns with values that actually
exist in this workspace.

---

# Endpoints

## `GET /healthz`

Liveness and readiness, deliberately **outside** `/api/v1`: it is
infrastructure, not part of the versioned contract, and a container healthcheck
should not need editing when the API version moves. It touches the database — a
Test Hub that cannot read its own store is not healthy however well it serves
HTTP.

```shell
$ curl -s http://127.0.0.1:8123/healthz
{"status":"ok","version":"0.1.0","schema_version":1,"database":"ok","workspace_id":"b037a4b3-53f8-5326-a5d5-f5fad3c31052","live_subscribers":0,"time":"2026-09-08T21:09:39.655537Z"}
```

`status` is `ok` or `degraded`; it is `degraded` (with `database: "error"`) when
the store cannot be read, and the endpoint still returns 200 with a body rather
than raising, because a healthcheck that throws tells you nothing. It is cheap:
measured at ~2 700 requests/second on a laptop at eight concurrent clients.

## `POST /api/v1/imports`

Upload a finished report file. This is the endpoint every CI system can use
without a client library.

`multipart/form-data`. `file` and `project` are required; everything else is
optional metadata that ends up on the run.

| Field | Notes |
|---|---|
| `file` | The report. Detected by content, not by extension. |
| `project` | Project key, e.g. `payments-api`. |
| `team`, `component`, `name` | Free text. |
| `type` | One of the test types above. Default `other`. |
| `trigger` | `manual`, `push`, `pull_request`, `schedule`, `api`, `retry`, `local`, `unknown`. Default `unknown`. |
| `environment`, `branch`, `commit` | Run context. |
| `framework`, `language` | Run context, when the file does not say. |
| `pipeline_id`, `pipeline_url`, `ci_provider` | Links back to the build. |
| `tags` | Comma-separated. |
| `run_id` | Reuse an existing run id instead of creating one. |
| `format` | Force an adapter by name; skips detection entirely. |
| `X-Idempotency-Key` (header) | Makes a retried upload a no-op. |

```shell
$ curl -s -X POST http://127.0.0.1:8123/api/v1/imports \
    -F "file=@tests/fixtures/junit/simple.xml" \
    -F "project=checkout-service" \
    -F "team=payments" \
    -F "type=unit" \
    -F "environment=ci" \
    -F "branch=main"
```

```json
{
  "schema_version": 1,
  "run_id": "502f828b-245a-4d89-9a16-2c4898958f5f",
  "project_key": "checkout-service",
  "adapter": "junit-xml",
  "format": "junit-xml",
  "detection_confidence": 0.9,
  "created": true,
  "counts": {
    "total": 5, "passed": 2, "failed": 1, "error": 1, "skipped": 1,
    "xfailed": 0, "xpassed": 0, "cancelled": 0, "unknown": 0,
    "running": 0, "queued": 0, "flaky": 0, "retried": 0,
    "completed": 5, "executed": 4, "pass_rate": 50.0, "failure_rate": 50.0
  },
  "suites_imported": 1,
  "tests_imported": 5,
  "attempts_imported": 5,
  "events_applied": 10,
  "events_ignored": 0,
  "warnings": [
    {
      "code": "naive_timestamp",
      "message": "timestamps in this report carry no UTC offset; they are read as UTC",
      "severity": "info",
      "location": "testsuite[com.example.checkout.CartTotalsTest]",
      "detail": {}
    },
    {
      "code": "suite_output_lifted",
      "message": "suite-level <system-out> has nowhere to live on a suite event, so it is attached to the run's metadata",
      "severity": "info",
      "location": "com.example.checkout.CartTotalsTest",
      "detail": {}
    }
  ],
  "duration_ms": 21
}
```

Status is **201**. `detection_confidence` is what the winning adapter scored;
`1.0` means you passed `format=` and nothing was guessed. `warnings` is the
part worth logging in CI — it is where "your report said 5 tests and I found 4"
shows up.

`counts.executed` excludes skipped and cancelled tests, which is why five tests
with one skip give a 50% pass rate rather than 40%. The formulas are in
[docs/metrics.md](metrics.md).

**Importing two different files into one `run_id` loses results.** Both built-in
adapters derive each event's id from `(source, run_id, sequence, type)` so that
re-importing the *same* file is a no-op. Two *different* files both start their
sequence at 1, so the second file's events collide with the first's and are
discarded as duplicates wherever the type also matches. Verified: importing a
5-test report and then a 4-test report into one run id produced a run with 6
tests and a suite with an empty path, not 9 tests.

```shell
$ curl -sX POST … -F file=@simple.xml        -F run_id=$RUN   # 5 tests
$ curl -sX POST … -F file=@nested-suites.xml -F run_id=$RUN   # 4 tests
$ curl -s "$HUB/api/v1/runs/$RUN" | jq '.run.counts.total'
6
```

So: **one report file per run.** To collect parallel shards under one run,
publish them through `POST /runs/{id}/events`, which allocates sequence numbers
server-side from the run's high-water mark and is safe from any number of
concurrent publishers. Both CI guides show that pattern.

Failure modes: 413 when the file exceeds `TEST_HUB_MAX_REPORT_BYTES`, 415 when
nothing recognises it, 422 when an adapter recognised it and then could not
parse it.

## `GET /api/v1/adapters`

What this server can parse. The same list appears inside a 415, but a publisher
can check it up front.

```shell
$ curl -s http://127.0.0.1:8123/api/v1/adapters
```

```json
{
  "adapters": [
    {
      "name": "junit-xml",
      "display_name": "JUnit XML",
      "description": "JUnit/xUnit XML from pytest, Surefire, Gradle, Jest, Playwright and friends.",
      "priority": 50,
      "extensions": [".xml", ".junit", ".junit.xml"],
      "content_types": ["application/xml", "text/xml", "application/x-junit+xml"]
    },
    {
      "name": "testhub-json",
      "display_name": "Test Hub JSON",
      "description": "Test Hub's own report and raw event JSON (kind: test-hub-report/-events).",
      "priority": 100,
      "extensions": [".json"],
      "content_types": ["application/json", "text/json", "application/vnd.test-hub+json"]
    }
  ]
}
```

Install a third-party adapter and it appears here with no configuration — see
the [adapter guide](adapters.md).

## `POST /api/v1/runs`

Open a run so results can stream into it while the suite is still running.

```shell
$ curl -sX POST http://127.0.0.1:8123/api/v1/runs \
    -H 'content-type: application/json' \
    -d '{"project_key":"storefront-web","test_type":"e2e","name":"live demo",
         "team":"growth","context":{"branch":"main","environment":"staging"}}'
```

```json
{
  "schema_version": 1,
  "id": "4ae82e39-684e-4b0c-ac2a-74985be381cd",
  "workspace_id": "b037a4b3-53f8-5326-a5d5-f5fad3c31052",
  "project_key": "storefront-web",
  "name": "live demo",
  "team": "growth",
  "component": null,
  "test_type": "e2e",
  "trigger": "unknown",
  "status": "running",
  "context": {"environment": "staging", "branch": "main", "framework": null, "…": null},
  "started_at": "2026-09-08T21:13:57.816990Z",
  "finished_at": null,
  "duration_ms": null,
  "counts": {"total": 0, "…": 0}
}
```

Status **201**. Body fields: `project_key` (required), `run_id`, `name`, `team`,
`component`, `test_type`, `trigger`, `context`, `tags`, `metadata`,
`expected_tests`, `started`. Set `started: false` to open the run as `queued`
rather than `running`.

Send your own `run_id` whenever you can generate a UUID: it is what makes this
call idempotent, and it is what lets every shard of a matrix build agree on
which run they are writing into without a coordination step. Reposting a known
`run_id` returns the existing run unchanged.

You cannot post `counts`. They are derived from the stored attempts, and
accepting a publisher's totals would create two sources of truth for the number
on the front page.

## `POST /api/v1/runs/{run_id}/events`

Append results to an open run. The body is `{"events": [ … ]}` with 1–5000
CloudEvents-shaped envelopes (`TEST_HUB_MAX_EVENTS_PER_BATCH`, default 5000).

A minimal event needs `type`, `workspace_id`, `run_id`, `sequence` and `data`.
`id` defaults to a fresh UUID, `source` to
`https://test-hub.dev/source/unknown`, `time` to now. The full envelope and
every payload type are documented in [docs/events.md](events.md).

**Sequence numbers.** Send `"sequence": 0` on every event and the server
allocates a contiguous block from the run's current high-water mark, inside the
same transaction, so two publishers posting at the same moment get disjoint
blocks instead of both starting at 1. Send your own monotonic numbers if you
want gap detection — which you probably do for a long suite, and which you
cannot have if you let the server allocate.

```shell
$ cat events.json
{"events": [
  {"type": "suite.started", "workspace_id": "$WS", "run_id": "$RUN", "sequence": 0,
   "data": {"suite_id": "$SUITE", "name": "checkout", "path": "specs/checkout"}},
  {"type": "test.finished", "workspace_id": "$WS", "run_id": "$RUN", "sequence": 0,
   "data": {"suite_id": "$SUITE", "suite_path": "specs/checkout",
            "name": "declines an expired card", "class_name": "CheckoutPayment",
            "status": "failed", "duration_ms": 812,
            "failure": {"type": "AssertionError", "message": "expected 402, got 500"}}},
  {"type": "test.finished", "workspace_id": "$WS", "run_id": "$RUN", "sequence": 0,
   "data": {"suite_id": "$SUITE", "suite_path": "specs/checkout",
            "name": "declines an expired card", "class_name": "CheckoutPayment",
            "attempt_number": 2, "status": "passed", "duration_ms": 774}}
]}

$ curl -sX POST "$HUB/api/v1/runs/$RUN/events" \
    -H 'content-type: application/json' -H 'X-Idempotency-Key: batch-1' -d @events.json
```

```json
{
  "run_id": "4ae82e39-684e-4b0c-ac2a-74985be381cd",
  "applied": 3,
  "ignored": 0,
  "created_run": false,
  "counts": {
    "total": 1, "passed": 1, "failed": 0, "error": 0, "skipped": 0,
    "flaky": 1, "retried": 1, "completed": 1, "executed": 1,
    "pass_rate": 100.0, "failure_rate": 0.0
  },
  "status": "running"
}
```

Two `test.finished` events for the same test produced **one** test with two
attempts, `flaky: 1`, and a 100% pass rate — the last attempt is the outcome and
the disagreement between attempts is the flake. A retry is a new attempt, never
a correction of the earlier one; that is the whole reason flakiness is
observable at all.

`workspace_id` must be this server's workspace (get it from `/healthz`), and
every event's `run_id` must match the URL. Both are 422 otherwise. A 404 means
the run does not exist — unless the batch itself opens it with a `run.queued`
event, which is the one legitimate race.

`suite_id` is not free-form: derive it as
`uuid5(TEST_HUB_NAMESPACE, "suite\x1f<project>\x1f<suite_path>")`. Omit
`test_id` and the server derives it for you from the identity components, which
is usually what you want.

## `POST /api/v1/runs/{run_id}/finish`

Close a run.

```shell
$ curl -sX POST "$HUB/api/v1/runs/$RUN/finish" -H 'content-type: application/json' -d '{}'
```

```
status passed  finished_at 2026-09-08T21:14:18.704654Z  duration_ms 20888
counts {'total': 1, 'passed': 1, 'failed': 0, …, 'pass_rate': 100.0}
```

Body fields are all optional: `finished_at`, `status`, `duration_ms`,
`metadata`. `status` is advisory and is honoured only for `cancelled` — every
other outcome is folded from the results that were actually reported, because a
cancelled run is the one outcome that looks exactly like a run that simply
stopped reporting.

Note the run above closed as `passed` even though its first attempt failed. That
is correct: the test passed on retry, and the run's status is folded from each
test's *last* attempt.

## `POST /api/v1/runs/{run_id}/artifacts`

Attach a screenshot, video, trace, log or coverage file to a run, optionally to
a specific test or attempt.

`multipart/form-data` with `file`, and optional `kind`
(`screenshot`, `video`, `log`, `trace`, `report`, `coverage`, `raw_report`,
`other`), `test_id`, `attempt_id` and `name`.

```shell
$ curl -sX POST "$HUB/api/v1/runs/$RUN/artifacts" -F "file=@shot.png" -F "kind=screenshot"
```

```json
{
  "artifact": {
    "id": "24dedd68-0095-4778-8a8c-4ca4b18d574e",
    "run_id": "4ae82e39-684e-4b0c-ac2a-74985be381cd",
    "test_id": null,
    "attempt_id": null,
    "kind": "screenshot",
    "name": "shot.png",
    "content_type": "image/png",
    "size_bytes": 26,
    "sha256": "4a8bc502d26defa6f7d1828bc5586a17c254a9a2c14d6fc6d5cf30880b3f738d",
    "url": "/api/v1/artifacts/24dedd68-0095-4778-8a8c-4ca4b18d574e",
    "created_at": "2026-09-08T21:16:46.416532Z",
    "metadata": {}
  },
  "deduplicated": false
}
```

Storage is content-addressed: fifty shards uploading the same 400 KiB screenshot
occupy 400 KiB on disk, keyed by that `sha256`. Verified — two uploads of
identical bytes left exactly one file under `artifacts/4a/8b/`.

**`deduplicated` is always `false` in this release.** The store knows whether it
created the blob, but the value is not threaded through to the response, and
each upload still gets its own artifact row and id. Do not use the field to
detect a retry; compare `sha256` if you need to.

413 when the file exceeds `TEST_HUB_MAX_ARTIFACT_BYTES` (default 128 MiB).

## `GET /api/v1/runs`

List runs. Takes every filter above.

```shell
$ curl -s "http://127.0.0.1:8123/api/v1/runs?limit=2"
```

```json
{
  "runs": [
    {
      "id": "dabb72a8-61cc-46ff-8393-be7dc6b8fe22",
      "project_key": "payments-api",
      "name": "Payments API #1425",
      "team": "payments",
      "component": "checkout",
      "test_type": "component",
      "trigger": "schedule",
      "status": "running",
      "environment": "staging",
      "branch": "main",
      "commit_sha": "f096247eff60",
      "framework": "pytest",
      "language": "python",
      "pipeline_url": "https://ci.example.com/payments-api/builds/1425",
      "started_at": "2026-09-08T21:05:29.080268Z",
      "finished_at": null,
      "duration_ms": 11051,
      "counts": {
        "total": 9, "passed": 7, "failed": 2, "error": 0, "skipped": 0,
        "flaky": 1, "retried": 1, "completed": 9, "executed": 9,
        "pass_rate": 77.78, "failure_rate": 22.22
      },
      "tags": ["payments", "staging"],
      "is_live": true
    },
    { "…": "…" }
  ],
  "total": 31,
  "limit": 2,
  "offset": 0
}
```

This is a *summary* shape — enough for a table row, deliberately without suites,
tests or attempts. `is_live` is true while the run is queued or running.

## `GET /api/v1/runs/{run_id}`

One run in full: the run, every suite, every test with all of its attempts, its
artifacts, and the comparison against the previous comparable run.

```shell
$ curl -s "$HUB/api/v1/runs/$RUN"
```

Top-level keys: `schema_version`, `run`, `suites`, `tests`, `artifacts`,
`comparison`.

- `run` — the same shape as `POST /runs` returns, plus `warnings`,
  `source_adapter` and `metadata`.
- `suites[]` — `id`, `run_id`, `name`, `path`, `parent_id`, `started_at`,
  `finished_at`, `duration_ms`, `counts`, `metadata`. `parent_id` chains nested
  suites; `path` is the full slash-joined path.
- `tests[]` — `id` (the stable identity), `suite_id`, `name`, `class_name`,
  `suite_path`, `params`, `full_name`, `file`, `owner`, `tags`, `markers`,
  `status`, `duration_ms`, `flaky`, and `attempts[]`.
- `attempts[]` — `attempt_number`, `status`, `original_status` (the framework's
  own word, always kept), `started_at`, `finished_at`, `duration_ms`, `failure`,
  `stdout`, `stderr`, `logs`, `skip_reason`, `worker`, `artifact_ids`,
  `metadata`.

`?compare=false` skips the comparison, which is the expensive part.

The comparison is against the previous run with the same project, team,
component, type, environment *and* branch — comparing a staging run against a
production one would be noise, so Test Hub does not:

```json
{
  "baseline_run_id": "ff8e4eb3-60e5-421b-9439-ce12a104c1fd",
  "baseline_started_at": "2026-09-04T03:22:38.181906Z",
  "pass_rate_delta": -5.55,
  "duration_delta_ms": -4440,
  "total_delta": -3,
  "newly_failing": ["28c6073c-460f-50fd-b150-1cd50089a1e2", "b1d4ff64-9777-5c09-80c4-54ad00198a50"],
  "newly_passing": ["a810dc37-5427-5afb-9e16-2a4dc9cb62ac", "abb270b2-ca14-5f46-94db-dd9d8b6629d9"],
  "still_failing": [],
  "added": [],
  "removed": ["055d95a1-a0cc-55fe-8e12-57f1892d010b", "aa43f995-31d8-59d8-bf3b-3049c32371b6", "ff609dd7-e7f6-5b36-a552-d9bebae977b4"]
}
```

The five arrays hold **test identities**, which are exactly what
`GET /tests/{test_id}` takes. `comparison` is `null` when no comparable
predecessor exists — a first run on a new branch has nothing to be measured
against, and inventing a baseline would be worse than admitting it.

## `GET /api/v1/tests/{test_id}`

One test identity across runs. `?history=N` (1–200, default 30) bounds how far
back it looks.

```shell
$ curl -s "$HUB/api/v1/tests/35eceeb7-dd1c-5fc2-b555-3af1386bca5e?history=3"
```

Keys: `schema_version`, `test_id`, `project_key`, `name`, `class_name`,
`suite_path`, `params`, `full_name`, `owner`, `tags`, `latest` (the full test
object from its most recent run, attempts included), `history[]`, `flake_rate`,
`failure_rate`, `average_duration_ms`.

Each `history` entry is `{run_id, started_at, status, duration_ms, attempts,
branch, environment}`. Because the identity is derived from what the test *is*
and never from when or where it ran, this history follows a test across a
rename of the file it lives in, a reordering of its parameters and a move to a
different machine — see [Adapters § Identity](adapters.md#identity).

404 when nothing has ever been recorded under that identity.

## `GET /api/v1/overview`

The portfolio altitude: totals, trend, project health, live runs and the worst
offenders. `?window_days=N` (1–365, default 30).

Keys: `schema_version`, `generated_at`, `window_days`, `counts`, `total_runs`,
`total_runs_all_time`, `live_runs[]`, `recent_runs[]` (20), `projects[]`,
`trend[]`, `top_failing_tests[]`, `flakiest_tests[]`, `filters`.

```json
{
  "window_days": 14,
  "counts": {"total": 269, "passed": 239, "failed": 19, "error": 6, "skipped": 5,
             "flaky": 15, "retried": 15, "executed": 264,
             "pass_rate": 90.53, "failure_rate": 9.47},
  "total_runs": 33,
  "total_runs_all_time": 37,
  "projects": [
    {"project_key": "checkout-service", "name": "checkout-service", "team": null,
     "runs_24h": 0, "last_run_id": null, "last_run_at": null,
     "last_status": "unknown", "pass_rate": null, "pass_rate_delta": null,
     "flaky_tests": 0, "live_runs": 0}
  ],
  "trend": [
    {"bucket": "2026-08-25", "runs": 0, "pass_rate": null, "failure_rate": null,
     "duration_ms_p50": null, "duration_ms_p95": null, "total": 0, "failed": 0}
  ]
}
```

`trend` is one bucket per day for the window (15 entries for
`window_days=14`, inclusive of both ends). `top_failing_tests` and
`flakiest_tests` are full `TestDetail` objects — identity, `latest` with its
attempts, `history`, `flake_rate`, `failure_rate`, `average_duration_ms`.

That last point is worth knowing before you poll this: the response embeds
complete test details, so it is not small. On the demo data set — 5 projects,
37 runs, 269 results — `?window_days=14` returned **94 KB**, and it is the
slowest read endpoint at roughly 76 requests/second single-client on a laptop.
It is cheap enough for a dashboard on a few-second timer, and it is not cheap
enough for a per-second poll from fifty tabs.

## `GET /api/v1/projects` and `GET /api/v1/facets`

```shell
$ curl -s "$HUB/api/v1/projects"
{"projects": [{"id": "b059d666-…", "workspace_id": "b037a4b3-…", "key": "identity-service",
               "name": "identity-service", "description": null, "default_team": null,
               "repository_url": null, "tags": [],
               "created_at": "2026-09-08T21:09:29.281422Z", "updated_at": "…"}, …]}

$ curl -s "$HUB/api/v1/facets"
{
  "projects": ["identity-service", "ledger-worker", "notification-gateway", "payments-api", "storefront-web"],
  "teams": ["growth", "payments", "platform"],
  "components": ["auth", "checkout", "delivery", "ledger", "web"],
  "test_types": ["api", "component", "e2e", "integration", "unit"],
  "environments": ["preview", "production", "staging"],
  "branches": ["feature/basket-rework", "main", "release/24.9"],
  "frameworks": ["JUnit 5", "Playwright", "go test", "pytest"],
  "languages": ["go", "java", "python", "typescript"],
  "tags": ["growth", "payments", "platform", "preview", "production", "staging"]
}
```

`facets` returns only values that exist in this workspace, which is what a
filter dropdown should offer. Note it includes `frameworks` and `languages`,
which are *not* filter parameters — they are there for display.

## `GET /api/v1/runs/{run_id}/artifacts` and `GET /api/v1/artifacts/{artifact_id}`

The first lists an artifact's metadata; the second streams the bytes.

```shell
$ curl -sD - -o shot.png "$HUB/api/v1/artifacts/24dedd68-0095-4778-8a8c-4ca4b18d574e"
HTTP/1.1 200 OK
content-disposition: attachment; filename="shot.png"
content-length: 26
x-content-type-options: nosniff
content-type: image/png
content-security-policy: default-src 'none'; script-src 'self'; …
```

Always `attachment`, always the stored content type, never a type sniffed from
the bytes. An artifact is attacker-supplied content served from Test Hub's own
origin; rendering it inline would hand any publisher a stored XSS on the
dashboard's domain.

## `GET /api/v1/events/stream`

Server-Sent Events. One-way, so SSE rather than WebSockets: it gives automatic
browser reconnection, `Last-Event-ID` replay and passage through any HTTP proxy
without an upgrade handshake, none of which we would get for free otherwise.

```shell
$ curl -N -H 'Accept: text/event-stream' http://127.0.0.1:8123/api/v1/events/stream
```

Response headers, verified on the wire:

```
content-type: text/event-stream; charset=utf-8
cache-control: no-cache, no-transform
connection: keep-alive
x-accel-buffering: no
transfer-encoding: chunked
```

`x-accel-buffering: no` is already sent, because nginx buffers proxied responses
by default and would otherwise turn a live stream into a stream that arrives all
at once, at the end. It is not sufficient on its own — see
[Deployment § reverse proxy](deployment.md#reverse-proxy).

### Frame format

Four event names. The stream opens with a `retry:` directive and a `hello`:

```
retry: 15000

event: hello
id: 50
data: {"workspace_id":"b037a4b3-53f8-5326-a5d5-f5fad3c31052","replaying":false}

event: test-hub
id: 51
data: {"event":{"specversion":"1.0","id":"741e773b-…","source":"urn:test-hub:api","type":"run.queued","time":"2026-09-08T21:13:57.816990Z","datacontenttype":"application/json","subject":null,"schemaversion":1,"workspaceid":"b037a4b3-…","runid":"4ae82e39-…","sequence":1,"data":{"project_key":"storefront-web","name":"live demo","team":"growth",…}},"received_at":"2026-09-08T21:13:57.819891+00:00"}

: heartbeat
```

| `event:` | `data` | Meaning |
|---|---|---|
| `hello` | `{workspace_id, replaying}` | Sent once per connection. `replaying: true` means the frames that follow are a `Last-Event-ID` replay. |
| `test-hub` | `{event, received_at}` | One applied domain event, in its CloudEvents envelope, exactly as stored. |
| `resync` | `{reason, …}` | Stop trusting the stream; refetch over REST. |
| *(comment)* | — | A bare `: heartbeat` line every `TEST_HUB_SSE_HEARTBEAT_SECONDS` (default 15). It is a comment, not an event, so it never moves the client's `Last-Event-ID`. |

**`id:` is a bus cursor, not the event's own id.** A replayed CloudEvent keeps
its identity, so using that as the resume token would make resumption ambiguous.
Resume from the `id:` line, not from `data.event.id`.

### `Last-Event-ID` and resync

The browser's `EventSource` sends `Last-Event-ID` automatically on reconnect. A
hand-written client sends the last `id:` it saw:

```shell
$ curl -N -H 'Last-Event-ID: 53' http://127.0.0.1:8123/api/v1/events/stream
retry: 15000

event: hello
id: 56
data: {"workspace_id":"b037a4b3-…","replaying":true}

event: test-hub
id: 54
data: {"event":{…"type":"test.finished"…}}

event: test-hub
id: 55
…
```

The server keeps a bounded ring buffer per workspace
(`TEST_HUB_SSE_REPLAY_BUFFER`, default 1000 events). If your cursor has fallen
out of it, you do not get a silent jump — you get a directive:

```shell
$ curl -N -H 'Last-Event-ID: 1' http://127.0.0.1:8124/api/v1/events/stream
retry: 15000

event: hello
id: 8
data: {"workspace_id":"b037a4b3-…","replaying":true}

event: resync
id: 8
data: {"reason":"cursor_expired","oldest_available":6}
```

(captured from a server started with `TEST_HUB_SSE_REPLAY_BUFFER=2`.)

**On `resync`, throw away your local state and refetch from REST.** A dashboard
that quietly skips 400 events is a dashboard confidently displaying stale
numbers, which is worse than a reload. The other `reason` is `slow_consumer`:
each subscriber has its own bounded queue, and a client that stops draining has
its backlog dropped and gets the same directive. Continuity for one tab is not
worth correctness for everyone else.

`TEST_HUB_SSE_MAX_CLIENTS` (default 200) caps concurrent subscribers.

### The multi-worker trap

The bus is in-process. Run more than one Uvicorn worker and each holds its own
bus and its own cursor sequence, so half the dashboards miss half the events and
a `Last-Event-ID` from one worker means nothing to another. `test-hub serve`
therefore runs exactly one worker, and the fix when that ceiling bites is a
shared bus behind the `EventBus` protocol, not `--workers 4`. See
[Scaling](scaling.md#the-bus).
