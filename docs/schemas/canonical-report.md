# The `test-hub-report` JSON format

Test Hub's own report format, for when your framework has no JUnit writer or
when JUnit XML would lose something you care about. It is the reference
publisher contract: everything the canonical model can hold is expressible here,
which is not true of JUnit XML.

Implemented in [`adapters/canonical_json.py`](../../src/test_hub/adapters/canonical_json.py).
The [event contract](../events.md) is the lower-level alternative.

## The rule about `kind`

Every document **must** declare `"kind": "test-hub-report"` (or
`"test-hub-events"`). This is not ceremony. A format that claimed "any JSON
object with a `tests` key" would happily swallow a coverage report, ESLint
output or half of a `package.json`, and each of those would arrive as a silently
wrong run with a plausible-looking pass rate. Test Hub does not advertise support
for arbitrary JSON, so it does not accept it: input without a recognised `kind`
is a `415` listing what the server does accept.

## A complete example

Everything below was imported with `test-hub ingest`; the output further down is
that import's real result.

```json
{
  "kind": "test-hub-report",
  "schema_version": 1,
  "run": {
    "name": "nightly e2e #418",
    "team": "growth",
    "component": "storefront",
    "test_type": "e2e",
    "trigger": "schedule",
    "started_at": "2026-04-02T01:15:00Z",
    "finished_at": "2026-04-02T01:19:42Z",
    "tags": ["nightly"],
    "context": {
      "framework": "playwright",
      "framework_version": "1.44.0",
      "language": "javascript",
      "environment": "staging",
      "branch": "main",
      "commit_sha": "8f21c0d",
      "ci_provider": "github-actions"
    }
  },
  "suites": [
    {"path": "specs/checkout", "name": "checkout"},
    {"path": "specs/checkout/payment", "name": "payment", "parent_path": "specs/checkout"}
  ],
  "tests": [
    {
      "name": "completes a card payment",
      "suite_path": "specs/checkout/payment",
      "status": "passed",
      "duration_ms": 1840
    },
    {
      "name": "declines an expired card",
      "class_name": "CheckoutPayment",
      "suite_path": "specs/checkout/payment",
      "params": {"currency": "EUR"},
      "status": "failed",
      "duration_ms": 812,
      "file": "specs/checkout/payment.spec.ts",
      "owner": "payments",
      "tags": ["smoke"],
      "failure": {
        "type": "AssertionError",
        "message": "expected 402 but got 500",
        "stack_trace": "at CheckoutPayment.decline (payment.spec.ts:44:11)",
        "expected": "402",
        "actual": "500"
      },
      "logs": "WARN issuer double returned 500"
    },
    {
      "name": "keeps the basket after a reload",
      "suite_path": "specs/checkout",
      "attempts": [
        {"status": "failed", "duration_ms": 4100,
         "failure": {"type": "TimeoutError", "message": "locator timed out"}},
        {"status": "passed", "duration_ms": 4300}
      ]
    },
    {
      "name": "applies a gift card",
      "suite_path": "specs/checkout",
      "status": "skipped",
      "skip_reason": "gift cards disabled in staging"
    }
  ]
}
```

```shell
$ test-hub ingest report.json --project storefront-web
```

```
  imported run 3f2a91c4-6d0b-4e17-9a55-1c0d2b4e77a8
  adapter   testhub-json (detected, 100% confident)
  project   storefront-web
  4 tests  2 passed  1 failed  1 skipped  1 flaky  66.7% pass
```

Four tests from five attempts, a 66.67% pass rate (the skip is excluded from the
denominator — see [metrics](../metrics.md)), and one test marked flaky because
its two attempts disagreed.

## Fields

### `run` — all optional

Anything you leave out, the import request supplies. Anything both supply, the
import request wins: the person typing `--team payments` knows more than the file
does.

| Field | Meaning |
|---|---|
| `id` | Stable UUID. Re-importing the same id **updates** that run rather than creating a second one — this is how sharded runs merge. |
| `name` | Human-readable, e.g. `nightly e2e #418`. |
| `team`, `component` | Ownership. Both are dashboard facets. |
| `test_type` | One of the `TestType` values (`unit`, `component`, `integration`, `e2e`, `api`, …). Unrecognised values warn and become `other`. |
| `trigger` | `push`, `pull_request`, `schedule`, `manual`, `api`, `retry`, `local`. |
| `status` | **Advisory only.** The server folds the real status from the attempts. It is honoured for `cancelled`, which cannot be derived. |
| `started_at`, `finished_at` | ISO-8601. Include the offset; a naive timestamp is read as UTC and warns. |
| `tags`, `metadata` | Yours. `metadata` is free-form JSON. |
| `context` | Where it ran — see below. |

### `run.context`

`framework`, `framework_version`, `language`, `language_version`, `environment`,
`branch`, `commit_sha`, `commit_message`, `pipeline_id`, `pipeline_url`,
`ci_provider`, `hostname`, `os`, `executor`. All optional, all strings, all
filterable in the dashboard. Fill in what you have.

### `suites` — optional

| Field | Meaning |
|---|---|
| `path` | **Required.** The identity of the suite within the project. |
| `name` | Display name; defaults to the last path segment. |
| `parent_path` | The `path` of the parent suite, for a tree. |
| `duration_ms`, `started_at`, `finished_at`, `metadata` | Optional. |

The whole array is optional: any `suite_path` a test names is created
implicitly. Declare suites when you want a nested tree or per-suite timings.

### `tests`

| Field | Meaning |
|---|---|
| `name` | **Required.** |
| `suite_path` | **Required.** Ties the test to a suite and forms part of its identity. |
| `class_name` | Class, describe block or feature. Part of the identity. |
| `params` | An object, an array, or a pre-rendered string. Object keys are sorted, so `{b,a}` and `{a,b}` are one test. |
| `status` | Any word [`normalize_status`](../../src/test_hub/domain/status.py) knows — `passed`, `failed`, `error`, `skipped`, `xfailed`, `xpassed`, and dozens of dialect spellings. Unrecognised words become `unknown` **and raise a warning**; they never quietly become a pass. |
| `duration_ms` | Integer milliseconds. |
| `file`, `owner`, `tags`, `markers`, `worker` | Optional metadata. |
| `failure` | `{type, message, stack_trace, expected, actual}` — all optional. |
| `stdout`, `stderr`, `logs` | Captured output. Truncated past `TEST_HUB_MAX_TEXT_FIELD_BYTES`, and flagged when truncated rather than silently cut. |
| `skip_reason` | Why it was skipped. |
| `attempts` | Retries — see below. |
| `attempt_number` | The other way to express retries. |
| `metadata` | Free-form. |

The original status word is always kept in `TestAttempt.original_status`, so
normalization is never lossy.

## Retries

A retry is a **new attempt**, never a correction. Two equivalent spellings:

```json
{
  "name": "keeps the basket after a reload",
  "suite_path": "specs/checkout",
  "attempts": [
    {"status": "failed", "duration_ms": 4100,
     "failure": {"type": "TimeoutError", "message": "locator timed out"}},
    {"status": "passed", "duration_ms": 4300}
  ]
}
```

or repeat the test with an increasing `attempt_number`:

```json
{"name": "keeps the basket after a reload", "suite_path": "specs/checkout",
 "attempt_number": 1, "status": "failed"}
{"name": "keeps the basket after a reload", "suite_path": "specs/checkout",
 "attempt_number": 2, "status": "passed"}
```

Both produce one test with two attempts, because entries are grouped by test
identity. The **last** attempt is the effective outcome, and the disagreement
between attempts is what makes the test flaky. Attempt numbers are re-sequenced
densely from 1, with a warning if the file's own numbering had gaps.

This is the single most important thing to get right in a publisher. Reporting a
retried test as one row with the final status loses the flakiness; reporting it
as two tests inflates the count and makes the pass rate wrong.

## Identity

Suite and test identities are **derived, never carried**. Test Hub computes them
with [`test_hub.domain.identity`](../../src/test_hub/domain/identity.py) from
project, suite path, class name, test name and normalized parameters — never from
timestamps, line numbers, shard ids or run ids.

That means a publisher in any language can compute the same UUIDs offline, and
that a test keeps its history when it moves down a file or its parameters are
reordered. The exact hashed string is five `\x1f`-separated fields:

```
project \x1f suite_path \x1f class_name \x1f test_name \x1f params
```

hashed as UUIDv5 in the namespace `4c8f4c4d-3ecb-5f78-a1e4-c1a24e07b1e6`.
`identity_source()` is public and tested precisely so another implementation can
be checked against it byte for byte.

## What the import produces

The document expands into an event stream in this order:

```
run.queued → run.started → suite.started* → test.finished* → suite.finished* → run.finished
```

Every suite is announced before any result arrives, which is what a dashboard
subscribing mid-import needs in order to draw the tree before it fills in.

## Idempotency

Event ids are UUIDv5 over `(source, run_id, sequence, type)` rather than random.
Importing the same file twice against the same `run.id` therefore applies **zero**
new events rather than doubling the run:

```shell
$ test-hub ingest report.json --project storefront-web --run-id 3f2a91c4-... --json | jq '{events_applied, events_ignored}'
{ "events_applied": 0, "events_ignored": 12 }
```

Without a `run.id` you get a new run each time, which is correct: two genuine
executions of the same suite are two runs.

## Versioning

`schema_version` is **refused**, not coerced, when this build does not speak it.
An older Test Hub reading a newer document cannot know which fields changed
meaning, and a wrong pass rate is worse than a failed import.

## The raw alternative

If you already speak the [event contract](../events.md), send events directly:

```json
{"kind": "test-hub-events", "schema_version": 1, "events": [ /* CloudEvents envelopes */ ]}
```

Sequence numbers are preserved — they are the file's statement about ordering,
and the adapter has no better information. Missing `run_id`, `workspace_id` and
`source` are filled from the import request. A `workspace_id` that disagrees with
the importing workspace is overridden with a warning: a file must never be able
to write into another tenant.
