# Architecture

How Test Hub is put together, and why. The decisions and the alternatives that
were rejected are in [ADR-0001](adr/0001-architecture.md); this document
describes the system as it actually exists, with file paths, so that a reader can
go and check.

## The shape of it

```
                    ┌─────────────────────────────────────────┐
  JUnit XML ──┐     │              test_hub.adapters          │
  Test Hub    ├────▶│   detect() → parse() → DomainEvent[]     │
  JSON      ──┘     └───────────────────┬─────────────────────┘
                                        │
  REST publisher ───────────────────────┤   (same events, different origin)
  (curl, SDK, CI plugin)                │
                                        ▼
                            ┌───────────────────────┐
                            │  EventRepository      │   the ONE write path
                            │  .apply_events()      │   atomic + idempotent
                            └───────────┬───────────┘
                                        │ fold
                    ┌───────────────────┴───────────────────┐
                    ▼                                       ▼
          ┌──────────────────┐                     ┌────────────────┐
          │  normalized rows │                     │   EventBus     │
          │  runs / suites / │                     │  (in-process)  │
          │  tests / attempts│                     └───────┬────────┘
          └────────┬─────────┘                             │ SSE
                   │ RunRepository                         │
                   ▼                                       ▼
          ┌───────────────────────────────────────────────────────┐
          │                     the dashboard                      │
          │            LiveDataProvider (REST + EventSource)       │
          └───────────────────────────────────────────────────────┘
```

## Layers, and the rule about them

```
cli ──▶ api ──▶ service ──▶ storage ──▶ (sqlite / filesystem)
 │       │         │
 │       │         └──▶ domain ◀── adapters
 └──▶ sdk ─(http)─▶ api
```

`test_hub.domain` imports nothing else from the package. That is not tidiness for
its own sake: it means an adapter, a publisher or a test can depend on the
contract without pulling in FastAPI, SQLAlchemy or a database, and it means a
cycle shows up immediately as an `ImportError` rather than as a slow build.

`storage` never imports `api`. `adapters` import only `domain`. If a piece of
logic wants to reach across those lines, it belongs in `service`.

## The write path

There is exactly one, and everything funnels into it. `test-hub ingest`,
`POST /api/v1/imports`, `POST /api/v1/runs/{id}/events` and the SDK all end at
`EventRepository.apply_events`.

1. **Parse.** An adapter turns bytes into
   [`DomainEvent`](../src/test_hub/domain/events.py) objects — a CloudEvents 1.0
   envelope with `workspaceid`, `runid` and a per-run `sequence`. The adapter
   never touches the database, which is what makes it unit-testable against a
   fixture file and what makes a file import and a live publisher literally the
   same code after this point.

2. **Apply.** `apply_events` opens one transaction, inserts each event, and folds
   it into the normalized rows. `UNIQUE(source, event_id)` makes a repost a
   no-op; `X-Idempotency-Key` does the same for a whole batch. The response says
   how many were applied and how many were ignored, so a publisher can tell the
   difference between "it worked" and "it had already worked".

3. **Fold.** Counts are recomputed from the stored attempts after every batch —
   never taken from a publisher's own totals, which drift the moment anything is
   retried or a shard fails to upload. A test contributes exactly once, through
   its *last* attempt. See [metrics.md](metrics.md) for the formulas.

4. **Publish.** The events that were actually applied go to the `EventBus`, and
   from there to every connected dashboard. A replayed batch publishes nothing,
   because nothing happened.

Retries are the reason step 3 looks the way it does. A rerun is a new
`TestAttempt` with `attempt_number` incremented; nothing is ever overwritten. A
test that failed and then passed is green *and* flaky, and both facts survive.

## The read path

`RunRepository` returns canonical Pydantic models, never ORM objects. The
database's shape will change long before the wire contract should, so nothing
outside `test_hub.storage` ever sees a `RunRow`.

Two altitudes, kept deliberately apart:

- **`GET /api/v1/overview`** — the portfolio answer. Bounded by design: the trend
  is capped at 60 buckets whatever the window, so a year of history is 53 points
  rather than 366. Cheap enough to poll.
- **`GET /api/v1/runs/{id}`** and **`/tests/{id}`** — the engineering drill-down,
  allowed to be large.

### Read transactions do not take the write lock

SQLite serializes writers, and Test Hub takes the write lock up front
(`BEGIN IMMEDIATE`) so that two publishers *queue* rather than deadlocking on a
lock upgrade. The cost is that any transaction holds that lock for its whole
life — including one that only reads.

So reads open a different kind of scope:

```python
with context.uow_factory(read_only=True) as uow:
    detail = uow.runs.get_run_detail(run_id)
```

which begins deferred and cannot block a publisher. Every query route, the
health check and the retention planner use it. Committing such a scope raises,
rather than silently taking the lock after all. Measured: with a *writing* scope
held open, a second writer is refused after the busy timeout; with a read-only
scope held open across a full overview query, the same writer completes in about
a millisecond (`tests/test_read_only_scope.py`).

## One dashboard, one provider

ADR-0001 D1 put a seam between the dashboard and its data so that a served page
and an offline file could share one renderer. [ADR-0003](adr/0003-drop-the-offline-export.md)
removed the offline file; the seam stayed, for the two jobs it still does.

`web/assets/js/10-provider.js` defines the interface — `getOverview`, `listRuns`,
`getRun`, `getTest`, `getFacets`, `subscribe`, `capabilities`, `status` — plus the
shared base that owns the status field, so the connection pill has one definition
rather than one per transport. `12-live-provider.js` is the implementation: REST
for reads, `EventSource` for changes.

The seam earns its keep twice over:

- **No view can reach the network.** A view renders from a plain object graph, so
  it cannot grow a `fetch` in the middle of a render — enforced by
  `tests/test_frontend_assets.py`, which fails if a view file so much as mentions
  `fetch`.
- **The browser-side arithmetic has one home.** When a filter the API cannot
  express forces the client to fetch runs and refine them, the counts have to be
  recomputed — and they have to agree, to the decimal, with
  `test_hub.domain.metrics`. Every formula in `10-provider.js` is a transcription
  of that module, and says which one.

### What the detail pages cost

Most views read one payload. Seven routes are the exception — analytics, test
suites, test metrics, test steps, screenshots, API logs and coverage — and they
share one dataset rather than each fetching their own. Failure taxonomy, suite
hotspots, ownership, per-test timing and every attached file all need every
attempt of every test, and no summary carries those. So the store reads the
**five most recent finished runs** in full — `ANALYTICS_RUNS` in `30-store.js`,
loaded by `loadRunDetails` for every route in `DETAIL_ROUTES` — plus the overview
it shares with the dashboard. Moving between those seven pages costs nothing: the
runs are already in memory and already cached.

Three things keep that honest:

- Runs still in flight are skipped. Half a run's failures classified as if they
  were all of them is a wrong answer, not a partial one.
- Finished run details are immutable, so they are cached by run id. A live
  update on the page costs one overview request and, at most, one new run.
- A run that fails to load costs only itself: the page draws the rest and says
  how many of the five it is describing.

The across-run questions — flakiest tests, most failing tests, the per-test
verdicts — are *not* recomputed here. They come from the overview, which the
server computes over the whole window.

### The pages with nothing behind them

Three routes in the rail name things the schema does not model, and they say so
rather than going missing:

| Route | What exists | What the page does |
|---|---|---|
| Screenshots | `ArtifactKind.SCREENSHOT` / `.VIDEO` | Real. A gallery, images served from `/api/v1/artifacts/{id}` under the existing `img-src 'self'`; video is linked, never embedded, because `media-src` is not in the CSP. |
| API Logs | `ArtifactKind.LOG` / `.TRACE`, and `stdout` / `stderr` / `logs` per attempt | Shows those, and states in a banner that there is **no request/response object** in the schema — nothing can be filtered by status code or endpoint. |
| Test Coverage | `ArtifactKind.COVERAGE` | Lists the stored reports. Test Hub does not parse a percentage out of them, so the page prints no coverage figure and draws no trend. |
| Test Steps | nothing | The finest grain recorded is the attempt. The page renders `attempt.metadata.steps` when a publisher supplies it — an interim convention, not a model — and otherwise says how many attempts it checked and found none. |

The rule they follow: an empty page names what would fill it. "No screenshots in
the last five runs" is a fact about the suite, and it is the fact somebody
clicked the link to find out.

`test-hub demo` publishes a sample of each kind — screenshots for the browser
suite's failures, a request log, a coverage report, a trace — so the three pages
can be judged without wiring up a publisher first. Every byte of it is generated
by `test_hub.demo.evidence`, including the PNGs.

## The SSE bus

Updates are one-way, so Server-Sent Events rather than WebSockets: automatic
browser reconnection, `Last-Event-ID` replay and plain HTTP proxying, all for
free.

- Every published event is kept in a bounded ring per workspace, tagged with a
  monotonic cursor. That cursor — not the CloudEvent's own id, which a replayed
  event legitimately keeps — is what goes on the `id:` line.
- A reconnect with `Last-Event-ID` replays from just after that cursor. If the
  cursor has already fallen out of the ring, the client gets a `resync`
  directive and refetches over REST. A dashboard silently missing 400 events is
  a worse failure than one that reloads.
- Heartbeats are comment frames (`: heartbeat`), so they cannot advance a
  client's `Last-Event-ID` past an event it has not seen.
- Each subscriber has its own bounded queue. One that stops draining has its
  backlog dropped and gets a `resync`; correctness for everyone else is worth
  more than continuity for one tab.

Two implementation details that are easy to get wrong and were:

**The timeout lives on the queue, not the iterator.** Wrapping an async
generator's `__anext__` in `asyncio.wait_for` *cancels* it on timeout, which can
leave the generator unusable — so a stream would go silent after its first
heartbeat. `Subscription.get(timeout)` awaits `Queue.get()` instead, where
cancellation is well defined.

**The bus holds the server's event loop.** Every route that writes is a
synchronous `def`, so FastAPI runs it in a worker thread and a slow query cannot
block the streams. A worker thread has no running loop of its own, so
`bind_loop()` is called once in the lifespan handler and `publish_nowait` hands
work over with `run_coroutine_threadsafe`. Without it, writes succeed and no
dashboard ever hears about them.

## Storage

`storage/base.py` declares four protocols — `RunRepository`, `EventRepository`,
`ArtifactStore`, `EventBus` — plus `UnitOfWork` to group them into a transaction.
The shipped implementations are SQLite or PostgreSQL, chosen by
`TEST_HUB_DATABASE_URL`, and a local filesystem.

| Protocol | Today | Where it would go next |
|---|---|---|
| `RunRepository` / `EventRepository` | SQLite (WAL, `BEGIN IMMEDIATE`), or PostgreSQL (MVCC, per-run advisory locks — [ADR-0004](adr/0004-postgresql.md)) | partitioning, materialized views for the overview |
| `ArtifactStore` | content-addressed filesystem tree | S3-compatible object storage, same SHA-256 key |
| `EventBus` | in-process ring buffer | Redis Streams or NATS |

SQLite specifics that matter: WAL so a reader and a writer coexist,
`foreign_keys=ON` (SQLite disables them by default, which makes `ON DELETE
CASCADE` a silent no-op), `synchronous=NORMAL`, and a `busy_timeout` so a second
writer waits instead of failing instantly. Timestamps are stored as ISO-8601 UTC
through a `TypeDecorator` and read back timezone-aware — SQLite drops `tzinfo`,
and losing it silently makes every duration wrong.

Every row carries `workspace_id` from the first migration, even though the
open-source edition creates only `default`. Adding a tenancy column to a
populated events table later is the migration nobody wants to write.

## Frontend

No framework, no bundler, no CDN, no Node at build or run time. Classic scripts
sharing one namespace on `window.TestHub`, with the load order recorded once in
`web/assets/manifest.json` and asserted against `index.html` by the test suite.
Nothing is fetched from a remote origin, so an air-gapped install renders exactly
like a connected one.

Charts are hand-rolled inline SVG that read their colours from CSS custom
properties, so they follow the theme. Every colour in the stylesheet is a
property defined twice — `:root` for light, `:root[data-theme="dark"]` for dark —
which `tests/test_frontend_assets.py` enforces by failing on any colour literal
outside those two blocks.

The theme is resolved before first paint by a script at the top of `00-util.js`:
a stored choice wins, otherwise `prefers-color-scheme` decides, and an explicit
`data-theme` is written onto `<html>` so nothing is left implicit. Storage access
is wrapped in `try`/`catch` because reading `localStorage` on a `file://` origin
throws outright in some browsers — that costs the reader their saved choice, not
the theme.

## What is verified how

| Property | Where |
|---|---|
| Fold, retries, idempotency | `tests/test_ingestion.py`, `tests/test_concurrency.py` |
| Two publishers cannot corrupt counts | `tests/test_concurrency.py` |
| SSE frames, heartbeats, replay, resync | `tests/test_sse.py` (against real Uvicorn) |
| Read-only scopes do not block writers | `tests/test_read_only_scope.py` |
| Migrations match the ORM, both directions | `tests/test_migrations.py` |
| Frontend invariants (no `innerHTML`, no CDN, theme tokens, the type scale) | `tests/test_frontend_assets.py` |
| The demo attaches evidence the dashboard can draw | `tests/test_demo_evidence.py` |

Browser-level behaviour — rendering, contrast in both themes, keyboard order,
the 375px layout, and that a `<script>alert(1)</script>` test name displays as
text — is checked with Playwright against a live server. Those checks are not in
the pytest suite because they would put a 180 MB browser download in CI for
every contributor; the reproducible recipe is in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Known limits

Documented rather than hidden, with the interfaces to replace them already in
place. [scaling.md](scaling.md) has the detail.

1. **Single-process write throughput.** SQLite serializes writers. Correct, not
   fast.
2. **One Uvicorn worker.** The bus is in-process, so a second worker would hold a
   second bus and half the dashboards would miss half the events. `serve` runs
   one and says why.
3. **One workspace.** The column is there; the UI is not.
