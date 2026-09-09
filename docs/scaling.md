# Scaling

Test Hub 0.1 is one Python process, one SQLite file and a directory of blobs. It
installs with `pip` and needs no services, which is the point — and it has a
ceiling, which ADR-0001 D4 documents rather than hides. This page measures that
ceiling, says where it actually is, and describes what replaces each piece when
you reach it.

The short version: on a laptop, imports plateau at roughly **30 reports (1 500
test results) per second** and stop getting faster no matter how many clients you
add, while latency grows without bound. The read path is no longer limited by
SQLite at all — it is limited by one Python process.

## The measurements

Every number below is from a run you can reproduce.

**Hardware and versions.** Apple M4 Pro, 14 cores, macOS 15.7.4, Python 3.14.6,
SQLite 3.53.3, Test Hub 0.1.0. Server started with

```shell
$ TEST_HUB_DATA_DIR=/tmp/th-bench TEST_HUB_STORE_RAW_REPORTS=false \
    test-hub serve --port 8130
```

`store_raw_reports=false` takes the artifact write out of the loop so the numbers
measure the fold, not the filesystem. Client and server are on the same machine,
so network latency is zero and the two compete for the same CPU — read these as
an upper bound on throughput and a lower bound on latency.

**Method.** *N* concurrent HTTP clients, each importing the same 50-test,
3 893-byte JUnit file six times, after three warm-up imports.

### Writes do not scale with clients

| Concurrent clients | Imports | Wall | Imports/s | Test results/s | p50 | p95 | max |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 6 | 0.17 s | 35.9 | 1 797 | 27 ms | 27 ms | 28 ms |
| 2 | 12 | 0.37 s | 32.1 | 1 605 | 58 ms | 76 ms | 78 ms |
| 4 | 24 | 0.79 s | 30.2 | 1 510 | 35 ms | 108 ms | 639 ms |
| 8 | 48 | 1.66 s | 28.9 | 1 445 | 60 ms | 739 ms | 1 265 ms |
| 16 | 96 | 3.59 s | 26.8 | 1 338 | 105 ms | 1 599 ms | 2 667 ms |

That is the signature of a single writer. Throughput is flat — slightly
*declining*, from 35.9 to 26.8 imports/s across a sixteenfold increase in
offered load — while p95 latency grows 59×, from 27 ms to 1 599 ms. Adding
publishers does not add capacity; it adds queue.

The mechanism is `BEGIN IMMEDIATE`. Every writing transaction takes SQLite's
write lock up front (`src/test_hub/storage/db.py`). That is not an accident and
it is not removable: SQLite's default deferred transaction takes a read lock
first and asks for the write lock at the first write, so two publishers that both
read-then-write deadlock on the *upgrade* and one dies with `SQLITE_BUSY` however
long the busy timeout is, because neither can make progress. Taking the write
lock up front turns a deadlock into a queue. A queue is the correct behaviour and
it is also the ceiling.

Nothing is lost while the queue drains: `tests/test_concurrency.py` runs four
publishers × 25 results into one run and asserts the folded counts are exact.
Correct, not fast.

The practical read: **a single Test Hub comfortably absorbs a few thousand test
results a second in bursts**, which is a lot of CI. What it will not do is stay
responsive when fifty pipelines finish at the same instant — they will all
succeed, and the last one will wait seconds.

### Reads are no longer SQLite-bound

A unit of work opened `read_only=True` begins a plain deferred transaction
instead of `BEGIN IMMEDIATE`, so a query never takes the write lock and never
blocks a publisher. Every `GET` route does this. `/healthz` — one tiny query,
one tiny response — shows what the storage layer can now do:

| Concurrent clients | Requests | Req/s | p50 | p95 |
|---:|---:|---:|---:|---:|
| 1 | 100 | 1 155 | 0.7 ms | 0.8 ms |
| 4 | 400 | 2 508 | 1.5 ms | 1.9 ms |
| 8 | 800 | 2 652 | 2.9 ms | 3.8 ms |
| 16 | 1 600 | 2 603 | 5.9 ms | 8.2 ms |

It scales to about 2 600 req/s and plateaus — that plateau is one Python
process's CPU, not the database.

The two big read endpoints behave quite differently:

| Endpoint | 1 client | 4 clients | 8 clients |
|---|---:|---:|---:|
| `GET /api/v1/overview?window_days=30` | 72.8 req/s | 91.8 req/s | 43.7 req/s |
| `GET /api/v1/runs?limit=50` | 345 req/s | 377 req/s | 117 req/s |

Both improve slightly from 1 to 4 concurrent readers and then *collapse* at 8.
That is not the write lock — the same server was serving 2 600 `/healthz` per
second at the same concurrency. It is CPU: building and serializing these
responses is expensive Python, and eight of them at once saturate the process.

For scale: on the demo data set — 5 projects, 37 runs, 269 results — a single
`/overview?window_days=14` response is **94 KB**, because it embeds full
`TestDetail` objects (identity, latest attempt, history, rates) for the top
failing and flakiest tests. It is cheap enough for a dashboard on a few-second
timer. It is not cheap enough for a one-second poll from fifty open tabs, and it
grows with your data.

If your dashboard feels slow before your imports do, this is why, and the fix is
not PostgreSQL.

## The bus

<a id="the-bus"></a>

`InMemoryEventBus` is a dictionary of subscriber queues plus a bounded ring
buffer per workspace, living in the server process. It needs no Redis to be
correct, and it is the reason `test-hub serve` runs **one** Uvicorn worker.

Two workers would each hold their own bus and their own cursor counter. A
publisher's write lands in whichever worker got the POST, and only the dashboards
connected to *that* worker see it. Worse, `id:` is a per-bus counter, so a
browser that reconnects to a different worker sends a `Last-Event-ID` that means
something else entirely there — it would be replayed the wrong events, silently.
`--workers 4` does not make Test Hub faster; it makes it wrong.

That is the ceiling that bites first in practice, because it is not about
throughput at all. One worker is one CPU core for everything Python does, which
is exactly what the read collapse above is measuring.

Within one process the bus is bounded on both sides: `sse_replay_buffer` (1000)
events kept per workspace, `sse_max_clients` (200) subscribers, and a per-
subscriber queue of 256. A client that stops draining gets its backlog dropped
and a `resync` directive rather than growing the process; a client whose cursor
has fallen out of the ring gets `resync` with `reason: "cursor_expired"` rather
than a silent jump. Both are visible in [api.md](api.md#last-event-id-and-resync).

## What replaces what

The protocols in `src/test_hub/storage/base.py` are where the replacements land.
Everything above them — the service layer, the API, the CLI, the exporter, the
dashboard — takes an `AppContext` and asks it for a unit of work, and knows
nothing about SQLite, the filesystem or the in-process bus. That is what makes
the following more than a comment.

### PostgreSQL behind `RunRepository` and `EventRepository`

**When.** Sustained concurrent publishing, more than one API process, or a data
set where `overview` is slow because of row counts rather than serialization.

**What does not change.** The wire contract (`test_hub.domain.models`, Pydantic,
`schema_version 1`), the event contract, every route, the CLI, the dashboard, the
exporter, and both adapters. Nothing serializes an ORM object — ADR-0001 D3 —
so the database's shape can move without the API's shape moving.

**What changes.**

- `Database` grows a non-SQLite branch. Most of it is already conditional on
  `url.get_backend_name() == "sqlite"`; the PRAGMAs and the `BEGIN IMMEDIATE`
  listener are the SQLite-only parts and would simply not install.
  `TEST_HUB_DATABASE_URL` already exists and is already threaded through
  `resolved_database_url`.
- The `BEGIN IMMEDIATE` discipline is replaced by real MVCC. Readers stop
  queueing behind writers by construction, and the read-only unit of work becomes
  a `SET TRANSACTION READ ONLY` (or nothing at all).
- The Alembic migrations need a second dialect. `0001_initial` is the only one
  today, so this is the cheapest moment in the project's life to do it. Watch for
  the JSON columns (`JSON` → `JSONB`), the UUID columns (SQLite stores them as
  text), and the `datetime` columns — SQLite has no native type and the
  repository already normalizes to UTC on the way in and out.
- `_existing_keys()` currently relies on holding the write lock: it SELECTs which
  `(source, event_id)` pairs already exist and then inserts the rest, which is
  safe only because nothing can interleave. On PostgreSQL that becomes
  `INSERT … ON CONFLICT (source, event_id) DO NOTHING RETURNING id`, which is
  both correct under MVCC and faster.
- Idempotency keys get an expiry of their own. Retention deletes them with their
  run ([ADR-0002](adr/0002-data-retention.md)), which bounds them, but a key is
  only useful for the minutes a publisher might retry in and is currently kept for
  as long as the run is.

**What you get.** Concurrent writers actually concurrent, connection pooling
across processes, and partial indexes and materialized views for the overview.
**What you pay.** `pip install test-hub && test-hub serve` stops being the whole
setup story, which is why it is not the default.

### S3 (or any object store) behind `ArtifactStore`

**When.** Artifacts outgrow the volume, or more than one API process needs to
serve the same blobs.

This is the easiest of the three, and deliberately so. The store is
content-addressed: `FilesystemArtifactStore` writes `<root>/<aa>/<bb>/<sha256>`
and an S3 implementation uses the same digest as the key. `put`, `open`,
`exists`, `delete` and `collect_unreferenced` are the whole blob interface, and
`put` already streams and already enforces `max_bytes` while reading.

**What changes.** One class. `record`, `list_for_run` and `get` are row
operations and stay on the database — an S3 store would compose with the existing
repository rather than replacing it, so the current `FilesystemArtifactStore`
would split into "blobs" and "rows".

**What does not.** Every caller, the artifact download route, the API shape, and
deduplication — which is free either way, because the key is the content.

One thing worth designing at the same time, because it is missing today:
presigned URLs, so `GET /api/v1/artifacts/{id}` can redirect instead of proxying
128 MiB through the API process.

Note that `collect_unreferenced` is the one method whose implementation is not
a straight translation. On a filesystem it can ask "is this digest still named by
a row?" and unlink in the same breath, because both the rows and the bytes are
local. An object store would want the same question answered in bulk — the
digests to keep, diffed against a listing — rather than one HEAD-and-DELETE per
blob. The signature takes an iterable of digests for exactly that reason.

### Redis Streams or NATS behind `EventBus`

**When.** You want more than one API process — which, given the read numbers
above, is the first thing you will want.

**What changes.** `InMemoryEventBus` becomes a client of a shared log. The
protocol is already the right shape:

```python
async def publish(self, workspace_id: uuid.UUID, events: Iterable[DomainEvent]) -> None: ...
def subscribe(
    self, workspace_id: uuid.UUID, *, last_event_id: str | None = None
) -> AsyncIterator[Any]: ...
```

`last_event_id` is in the interface precisely so a replaying implementation has
somewhere to put its cursor. Redis Streams is the closest fit: `XADD` per
workspace, `XREAD BLOCK` per subscriber, and the stream entry id becomes the SSE
`id:` — which also fixes the cursor-identity problem, because a Redis stream id
is meaningful across processes in a way the current in-process counter is not.
`MAXLEN ~ N` replaces the ring buffer, and an id older than the trimmed window is
answered with the same `resync` directive that exists today. NATS JetStream works
equally well with a durable consumer per connection.

**What does not change.** The SSE route, the frame format, `Last-Event-ID`
semantics, the resync directive, the heartbeat, and every line of dashboard code.
A client cannot tell which bus is behind the stream.

**What you must not skip.** `publish_nowait` currently hands work to the server's
event loop from a worker thread, and a cross-process bus makes publishing a
network call that can fail. It must stay non-fatal — a bus that is down should
degrade the dashboard to stale, not fail the import that has already committed.

### More than one API process

Once the bus is shared, `test-hub serve` can run multiple workers, or several
containers can sit behind a load balancer. Three things need attention:

- **Sticky sessions are not required** for SSE once the bus is shared, but the
  proxy must still not buffer (see
  [deployment.md](deployment.md#reverse-proxy)).
- **Migrations must run once**, not once per worker. Today every entry point
  calls `database.upgrade()` on start, which is right for one process and a race
  for four. It becomes a separate step or an init container.
- **Idempotency keys and the `(source, event_id)` unique constraint** are what
  make concurrent writers safe. They already are; nothing new is needed.

## Limits that are not about throughput

**Snapshot export is a full materialization.** `test-hub export` builds the
entire object graph in memory and writes it into one HTML file — 252 KB for the
demo data set. It is not intended for a million tests, and the fix is not a
faster database; it is `--days` and `--max-runs`, which already exist.

**`GET /runs/{id}` has no paging.** A run returns every suite, every test and
every attempt. A 50 000-test run is one enormous response. That is a deliberate
altitude choice — the drill-down is allowed to be large, the overview is not —
and the first thing to revisit if anyone runs a suite that big.

**Storage grows only as far as retention lets it.** `test-hub prune` deletes runs
past a cutoff, everything under them, and the content-addressed blobs left
unreferenced as a result ([ADR-0002](adr/0002-data-retention.md)). Two things it
is not: it is not automatic — nothing sweeps until you schedule the command — and
it is not a size ceiling, because the policy is age with a per-project floor, so
a workspace full of dormant projects keeps `--keep-min` runs of each regardless
of the cutoff. There is still no deletion *endpoint*, deliberately (D8).

**One workspace.** The column is on every row from the first migration
(ADR-0001 D9) precisely so that adding tenancy later is not the migration nobody
wants to write — but the OSS edition creates one workspace and offers no way to
make another.

## What to do before any of this

In order of value per hour spent, if a single Test Hub is struggling:

1. **Stop storing raw reports** if you do not need them for parser debugging:
   `TEST_HUB_STORE_RAW_REPORTS=false`. One fewer write and one fewer file per
   import.
2. **Poll `/overview` less often, and with a smaller `window_days`.** It is the
   most expensive endpoint by an order of magnitude.
3. **Publish one report per run.** Not for speed — because importing two files
   into one run id loses events; see [api.md](api.md#post-apiv1imports).
4. **Batch live events.** One `POST /runs/{id}/events` with 200 events is one
   transaction; 200 posts are 200 transactions, each taking the write lock.
5. **Raise `TEST_HUB_SQLITE_BUSY_TIMEOUT_MS`** (default 10 000) before you
   conclude that a burst is failing rather than queueing.
6. **Put the data directory on a local SSD.** WAL on a network filesystem is a
   different and much worse ceiling; SQLite's locking assumes POSIX semantics
   that NFS does not reliably provide.
7. **Run a second Test Hub.** Two teams, two instances, two data directories,
   zero coordination. It is not elegant and it is available today.
