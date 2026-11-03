# ADR-0001 — Test Hub architecture

- **Status**: Accepted, partly superseded
- **Date**: 2026-09-08
- **Deciders**: Test Hub maintainers
- **Supersedes**: —
- **Amended by**: [ADR-0002](0002-data-retention.md) (D4, D8),
  [ADR-0004](0004-postgresql.md) (D4 — PostgreSQL ships beside SQLite)
- **Superseded in part by**: [ADR-0003](0003-drop-the-offline-export.md) — D1's
  second provider, and every "snapshot" or "offline file" below, are gone. The
  decisions themselves are left as written: this file records what was decided
  in September 2026, not what is true today.

## Context

Test Hub aggregates automated test results from any language, framework, team and
pipeline into one canonical model, and shows them in one dashboard. Two consumption
modes are required from day one: a self-contained HTML file that works with no
network, and a live dashboard backed by a small Python service. The first release
must be a working vertical slice, not scaffolding, and it must stay small enough
that a single maintainer can run it on a laptop.

## Decisions

### D1 — One dashboard, two data providers

The dashboard is written once. It talks to a `DataProvider` interface with two
implementations: `SnapshotDataProvider` (reads a normalized JSON object embedded in
the HTML file) and `LiveDataProvider` (REST + Server-Sent Events). The provider is
selected at boot from a single global marker written by whichever host page loaded
the app.

*Why*: two dashboards diverge within one release. The provider seam is the only
place the two modes differ, and it is the one place the difference is real —
snapshot data is frozen and complete, live data is paged and arrives over time.

*Consequence*: every view must render from a plain object graph and must tolerate
"no more data will arrive" (snapshot) as well as "more data will arrive"
(live). Views never call `fetch` directly.

### D2 — Events are the write model; tables are the read model

All ingestion — file import, REST run creation, live event posting — is normalized
into a stream of CloudEvents-shaped domain events with a monotonic per-run sequence
number. Events are stored, then folded into normalized run/suite/case/attempt rows.

*Why*: it makes a file import and a live publisher literally the same code path, it
makes retries first-class (an attempt is an event, not an overwrite), and it makes
idempotency a property of the store (`UNIQUE(source, event_id)`) rather than of each
endpoint.

*Consequence*: aggregate counts are derived by folding, never by trusting a
publisher's totals. Reposting an event is a no-op by construction.

### D3 — Public schemas are versioned and separate from ORM models

`test_hub.domain.models` (Pydantic v2) is the wire contract, carrying a
`schema_version`. `test_hub.storage.models` (SQLAlchemy 2) is private. Nothing
serializes an ORM object.

*Why*: the database will change shape (PostgreSQL, partitioning, a materialized
overview) long before the wire contract should.

### D4 — SQLite with WAL, filesystem artifacts, behind repository interfaces

`RunRepository`, `EventRepository`, `ArtifactStore` and `EventBus` are protocols.
The shipped implementations are SQLite (WAL, `busy_timeout`, UTC, one writer lock)
and a content-addressed local filesystem tree.

*Why*: the first release must install with `pip` and run with no services. The
interfaces are where PostgreSQL, S3 and a message queue will land later — see
`docs/scaling.md`.

*Consequence*: concurrent publishers are serialized on a single write lock inside
the process and by WAL + `BEGIN IMMEDIATE` across processes. That is correct, not
fast; it is documented as the known ceiling.

### D5 — Adapters are entry-point plugins with scored detection

`ReportAdapter` is a `Protocol` with `detect(stream, filename, content_type) -> float`
and `parse(stream, context) -> Iterable[DomainEvent]`. Built-in adapters are
registered through the `test_hub.adapters` entry-point group like any third-party
one, so the plugin path is the path we ourselves use. `--format` always wins over
detection. Unknown input lists installed adapters and their formats.

*Why*: the format list is the part of this project most likely to grow, and it must
grow outside the release cycle of the core.

*Consequence*: we ship exactly two adapters — canonical JSON and JUnit/xUnit XML —
and we do not advertise "any JSON" or "any XML".

### D6 — Server-Sent Events, not WebSockets

Updates are one-way (server → dashboard). SSE gives us `Last-Event-ID` replay,
automatic browser reconnect and plain HTTP proxying for free.

*Consequence*: the bus keeps a bounded in-memory ring buffer per workspace so a
reconnect with `Last-Event-ID` can be replayed; a gap beyond the buffer is answered
with a `resync` directive that makes the dashboard refetch.

### D7 — No Node.js at runtime, no CDN, no build step

The frontend is hand-written CSS and classic (non-module) JavaScript shipped inside
the wheel. Charts are hand-rolled inline SVG.

*Why*: ES modules do not load over `file://`, the snapshot must be one file, the
CSP must be able to forbid remote origins outright, and a Python package that needs
`npm` to build is a packaging liability. Classic scripts concatenate deterministically
into a single inline `<script>` for the snapshot and are served as separate files in
live mode — one source of truth for both.

*Consequence*: no framework. The app is ~10 small files with an explicit load order
recorded in `web/assets/manifest.json`.

### D8 — Secure by default, loudly

Bind `127.0.0.1`. Binding elsewhere requires either configured API tokens or an
explicit `--insecure-no-auth` opt-in that logs a warning on every start. Tokens are
stored as salted hashes and are never accepted as a CLI argument. XML parses with
`defusedxml` (no DTD, no entity expansion). CORS is off. The dashboard sets a
restrictive CSP; snapshot JSON is embedded so that `</script>` in a test name cannot
break out.

### D9 — Workspace scoping from the first migration

Every row carries `workspace_id`, even though the OSS edition creates one `default`
workspace and no UI to change it.

*Why*: adding a tenancy column to a populated events table later is the migration
nobody wants to write.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| Store publisher-reported totals | Diverges from the rows on any retry or partial upload; folding is cheap. |
| WebSockets | Bidirectional cost with no bidirectional requirement. |
| A JS framework + bundler | Node in the wheel build, and no offline single-file story without extra work. |
| Snapshot as a second renderer | Guaranteed drift; the entire reason for D1. |
| Chart.js vendored | 200 KB in every snapshot for six chart types we can draw in 300 lines of SVG. |
| Postgres/Redis in v1 | Turns `pip install` into `docker compose up`; the interfaces keep the door open. |

## Known limits of the first release

1. Single-process write throughput (D4). Documented, not hidden.
2. The SSE bus is in-process, so multiple Uvicorn workers would each hold a partial
   view; the CLI therefore runs one worker and says why.
3. Snapshot export is a full materialization — it is not intended for a million tests.
