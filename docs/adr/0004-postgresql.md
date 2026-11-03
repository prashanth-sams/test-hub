# ADR-0004 — PostgreSQL as a second database

- **Status**: Accepted
- **Date**: 2026-11-03
- **Deciders**: Test Hub maintainers
- **Supersedes**: —
- **Amends**: [ADR-0001](0001-architecture.md) D4

## Context

ADR-0001 D4 shipped SQLite behind repository interfaces and named its cost: every
writing transaction takes the one database write lock (`BEGIN IMMEDIATE`), so
concurrent publishers queue. `docs/scaling.md` measured the queue — imports
plateau at about 30 per second however many clients publish, and p95 latency
grows 59× across a sixteenfold increase in load. That is fine for a team; it is
the first ceiling a busy CI fleet reaches, because pipelines finish together.

The interfaces were built so that PostgreSQL could replace SQLite without moving
anything above them. This ADR records how it was done.

## Decisions

### D1 — The URL chooses the database; SQLite stays the default

`TEST_HUB_DATABASE_URL` unset is SQLite in the data directory, as before.
`postgres://…` or `postgresql://…` is PostgreSQL, given the psycopg 3 driver
unless the URL names another. Any other backend is refused at startup by name,
because the write path's concurrency guarantees are implemented per backend and a
third one would run without them. The driver is an extra
(`pip install "test-hub[postgres]"`); the Docker image installs it, so one image
runs either database.

*Why not a separate `TEST_HUB_DATABASE=postgresql` switch plus host/port/user
settings*: the URL already existed, every hosting provider hands one out, and a
second way of saying the same thing is a second thing to disagree. Passwords that
are awkward to URL-encode can stay out of the URL entirely — libpq reads
`PGPASSWORD` and `~/.pgpass`, which is what the Compose override uses.

### D2 — Serialize per run, not per database

The fold reads before it writes: which events are stored, the suite, the test and
the attempt it is about to touch, then a recount of the whole run. On PostgreSQL
under READ COMMITTED, two batches for one run that interleave lose an update —
each recount sees only its own attempts, and the later commit overwrites the
earlier total.

Each write path therefore takes a transaction-scoped advisory lock on the run
(`pg_advisory_xact_lock`, keyed by a hash of the run id) before its first read:
`apply_events`, and `next_sequence`, whose number must still be the high-water
mark when the batch using it lands. An idempotency key gets a lock of its own,
taken first, because a retried import carries the same key with a *fresh* run id
and the run lock alone would let both copies past the replay check. After taking
a lock the session expires what it had loaded, so a row read before the wait is
re-read rather than trusted.

*Why advisory locks rather than SERIALIZABLE*: serializable isolation would be
correct too, but every conflict becomes a retry loop wrapped around the unit of
work, and the fold is the expensive part to repeat. A lock that queues is what
SQLite already did — just per run instead of per database.

*Why not `SELECT … FOR UPDATE` on the run row*: the first batch for a run has no
row to lock yet; two first batches would both insert it.

### D3 — Cross-run races use `INSERT … ON CONFLICT DO NOTHING`

The rows that several *different* runs may create at once — the workspace, and a
project whose first pipelines report together — are inserted with
`ON CONFLICT DO NOTHING` and then read back, so whichever insert wins, the other
writer proceeds with its row. Locking them instead would serialize every import
of a project on its project row for the length of the transaction.

### D4 — Keep the portable schema; widen what PostgreSQL narrowed

- **Timestamps** stay fixed-width UTC strings (`UtcDateTime`). One code path, and
  ordering and comparison are byte-for-byte the same on both backends.
- **JSON** stays `JSON`, not `JSONB`. `JSONB` rejects `\u0000`, and the event log
  stores publishers' payloads verbatim; nothing queries inside JSON.
- **Integers** that publishers supply — sequences, durations, sizes, attempt
  numbers — become `BIGINT` on PostgreSQL in migration `0003_postgresql_bigint`.
  SQLite's `INTEGER` is already 64-bit, so the migration is a no-op there rather
  than a table rebuild that changes nothing.
- **Text** drops NUL characters from captured output (PostgreSQL refuses them in
  text columns). **Search and tag filters** use `ILIKE`, which means what SQLite's
  `LIKE` already meant. **Run lists** say `NULLS LAST`, which SQLite does by
  default and PostgreSQL does not.

### D5 — Startup and maintenance

Migrations take an advisory lock, so two instances starting against one empty
database queue instead of both creating the schema. A server that cannot be
reached is reported in one line with the masked URL before anything binds.
`prune` locks a batch's run rows before sweeping their events, so an import still
writing one of them finishes first rather than leaving orphaned events; `--vacuum`
runs `VACUUM (ANALYZE)` on Test Hub's own tables in autocommit.

## Consequences

- Writers to different runs proceed in parallel; writers to one run queue, as
  every writer did on SQLite. Readers never block writers on either backend.
- **One Test Hub process imports faster on SQLite than on PostgreSQL.**
  Measured, a 40-import burst took 1.6 s on SQLite and 2.7 s on PostgreSQL with
  the server on cores of its own ([scaling.md](../scaling.md#sqlite-or-postgresql)).
  Imports are CPU-bound in the process, and PostgreSQL adds a round trip per
  statement; the lock this ADR replaces was never the binding limit for one
  process. PostgreSQL is chosen for its operations and as the database half of
  scaling out — not as a speed-up on its own. Measuring this also found the fold
  issuing ~270 statements per 50-test import, now ~20, which helped both.
- The suite runs unchanged against PostgreSQL when `TEST_HUB_TEST_DATABASE_URL`
  is set, one schema per test. One SQLite-only test — the database-wide write lock
  — is skipped there, and PostgreSQL-only tests cover what replaced it.
- It is still one Test Hub process. The live-update bus (ADR-0001 D6) is
  in-process, so PostgreSQL does not make a second API worker safe; that is the
  next ceiling, along with the CPU of the one process.
- Artifacts remain on the filesystem, so the data volume and its backup are still
  required with PostgreSQL.
- There is no built-in copy of an existing SQLite database into PostgreSQL; a
  switched installation starts empty.
