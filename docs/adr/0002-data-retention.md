# ADR-0002 — Data retention and pruning

- **Status**: Accepted (D8 amended 2026-09-09)
- **Date**: 2026-09-09
- **Deciders**: Test Hub maintainers
- **Supersedes**: —
- **Amends**: [ADR-0001](0001-architecture.md) D4, D8

## Context

Test Hub has no retention, no expiry and no deletion. Nothing ages data out,
no endpoint removes a run, and there is no artifact garbage collector, so a blob
whose rows rolled back stays on disk forever. Storage grows monotonically; the
only documented remedy is `rm -rf` on the data directory
([scaling.md](../scaling.md), [security.md](../security.md)).

That is defensible for a first release and indefensible for the second. A team
publishing a nightly suite from twenty shards accumulates a screenshot per failed
test forever, and the operator's only lever is losing *all* history at once.

The two settings that look like they might help do not. `history_window_days`
bounds the overview's trend query and `flaky_window_runs` bounds flaky detection
([config.py:202-203](../../src/test_hub/config.py#L202-L203)); rows outside those
windows are still stored, still indexed, still backed up. They stop being *shown*.
Nothing about them is retention, and conflating the two would be a bad surprise
for the first operator who set `HISTORY_WINDOW_DAYS=7` expecting a disk saving.

What is being designed here is the missing third thing: an operator-controlled
policy that removes runs older than a cutoff, everything that hangs off them, and
the blobs that are left unreferenced as a result.

## Decisions

### D1 — Retention is a command the operator schedules, not a daemon that surprises them

Ship `test-hub prune` in the first cut. `TEST_HUB_RETENTION_DAYS` exists, but as
the *default for that command's cutoff* — not as a background sweep. Automatic
sweeping is phase two, behind its own explicitly-named setting.

*Why*: ADR-0001 D4 buys write correctness with a single-writer ceiling —
`BEGIN IMMEDIATE` means every transaction holds the write lock for its whole
life, and publishers queue behind it. A sweep that fires on an in-process timer
takes that lock at a moment nobody chose, and the symptom an operator sees is
import latency spiking at an hour they cannot explain. A cron entry at 04:00, or
a Compose sidecar, puts the stall where the operator put it.

*Consequence*: setting `TEST_HUB_RETENTION_DAYS` alone deletes nothing. That is a
trap unless it is stated everywhere the setting is documented, so the setting's
one-line description says so, and the configuration reference says so again.

*Rejected*: shipping the sweep first and the command second. The command is the
testable core; the scheduler is a loop around it. Building the loop first means
the interesting logic can only be tested through a timer.

### D2 — The cutoff is when the run happened, not when the row was written

Prune on the same expression the dashboard's `until` filter already uses:
`started_at` when present, `created_at` when not
([repositories.py:624-629](../../src/test_hub/storage/repositories.py#L624-L629)).

*Why*: an operator who filters the dashboard to "before 1 June", sees forty runs,
and then prunes "before 1 June" must get those forty runs. Two different notions
of a run's date in one product is a bug report that takes an afternoon to
understand.

*Consequence*: a back-dated import — a CI job uploading last quarter's archive —
is eligible for deletion the moment it lands. That is the correct reading of "keep
90 days of test history" and the wrong reading of "keep everything for 90 days
after I receive it". `--by-import-date` switches to `created_at`, which is the
non-null indexed column ([`ix_runs_created_at`](../../src/test_hub/storage/models.py#L186)),
for operators who want the storage-budget reading.

Timestamps are stored through `UtcDateTime` as fixed-width
`%Y-%m-%dT%H:%M:%S.%f+00:00` strings, always UTC
([models.py:95](../../src/test_hub/storage/models.py#L95)). Lexicographic
comparison is therefore chronological comparison, and a `<` against a cutoff is
correct without any casting. This is load-bearing and worth a comment at the
query, because it stops being true the moment anyone stores a local offset.

### D3 — The prune deletes what the schema will not

`suites`, `test_cases` and `attempts` carry
`ForeignKey("runs.id", ondelete="CASCADE")` and are removed by SQLite when the run
goes — `PRAGMA foreign_keys=ON` is set on every connection
([db.py:168](../../src/test_hub/storage/db.py#L168)), so the cascade is real.

`events`, `artifacts` and `idempotency_keys` do **not**. Their `run_id` is a plain
`sa.Uuid()` column with no foreign key
([models.py:354](../../src/test_hub/storage/models.py#L354), [374](../../src/test_hub/storage/models.py#L374) and [401](../../src/test_hub/storage/models.py#L401)). Deleting a
run leaves every one of its events and artifact rows behind, orphaned and
invisible. The prune deletes them explicitly, in this order, inside the run's
transaction:

```
artifacts        → collect sha256 set first, then delete rows
events
idempotency_keys
runs             → cascades to suites, test_cases, attempts
```

*Why explicit deletes rather than adding the missing foreign keys*: SQLite cannot
`ALTER TABLE ADD CONSTRAINT`. Alembic's `batch_alter_table` emulates it by
creating a new table, copying every row, dropping the old one and renaming — on
`events`, which is the largest table in the database and the one ADR-0001 D2 makes
the write model. A migration that rewrites the event log to save six lines of
`DELETE` is a bad trade, and it is not reversible in any way an operator would
enjoy at 2 a.m.

*Consequence*: the delete set lives in code, not in the schema, so it goes stale
silently if a table is added later. One test asserts that every table carrying a
`run_id` column is either FK-cascaded or named in the prune's delete list, and
fails on a table that is neither. That test is the actual guarantee; the list is
just its implementation.

### D4 — Blobs are unlinked after the rows commit, and only when nothing else points at them

Artifacts are content-addressed and deduplicated: `put()` returns
`created=False` when the digest is already on disk
([artifacts.py:161-165](../../src/test_hub/storage/artifacts.py#L161-L165)), so one
blob backs every row that ever uploaded those bytes — across runs, across projects.
A hundred shards uploading the same 400 KiB screenshot cost 400 KiB, which is the
whole point of the design and also the reason a per-run blob delete is wrong.

So the sweep is two phases with a commit between them:

1. In the transaction: collect the `sha256` of every artifact row about to be
   deleted, delete the rows, delete the run, **commit**.
2. After the commit, in a fresh write transaction: for each candidate digest,
   re-check `SELECT 1 FROM artifacts WHERE sha256 = ? LIMIT 1`. Unlink only on
   no rows. `ix_artifacts_sha256` already exists, so this is an index probe.

*Why this order*: unlinking before the commit means a rollback leaves live rows
pointing at missing files — a 404 on a screenshot that the database swears exists,
with nothing in the log to explain it. Deleting rows is recoverable from a backup;
deleting the bytes early is not.

*The race, and the fix*: an importer that uploads content already on disk takes no
write lock to do it — `put()` short-circuits on `final.exists()` and the row is
recorded later. So a sweeper can hold the write lock, see no referencing rows,
unlink, and release it to an importer whose row then points at nothing.

The fix is one line in `put()`: `os.utime()` the existing file on the
short-circuit path, so a blob's mtime means *last time anyone uploaded these
bytes* rather than *first time*. Phase 2 then skips any digest whose file was
touched within `TEST_HUB_ARTIFACT_GC_GRACE_HOURS` (default 24). Losing the race
now requires an import that has held an uncommitted transaction for a day, which
the busy timeout makes impossible.

*Consequence*: `put()` gains a filesystem write on its hottest path — the
dedup hit. It is a metadata update on a file the process just stat'd, and it is
the price of a GC that cannot corrupt a live run.

### D5 — Batched transactions, always

The prune walks runs oldest-first in pages of `--batch-size` (default 100),
committing each page. It never opens one transaction over the whole job.

*Why*: see D1. A prune of six months of a busy workspace is minutes of write
lock, and every publisher in CI blocks on it. Batching turns a long stall into a
queue that drains between pages, which is the same trade
`test_read_only_scope.py` documents for exports.

*Consequence*: the prune is not atomic. Interrupt it and some runs are gone and
some are not — which is fine, because it is idempotent by construction: run it
again and it resumes from the oldest remaining run. It must therefore never
report "pruned 400 runs" from a plan it did not finish; the count is what
committed.

### D6 — A floor per project, or retention deletes projects out of existence

`--keep-min` (default 5, `TEST_HUB_RETENTION_MIN_RUNS`) always keeps the N most
recent runs *per project*, however old they are.

*Why*: age alone is wrong for the quiet project. A service that ran nightly for a
year and then went stable has all of its runs on the far side of any cutoff, so a
90-day policy deletes every trace of it — the project vanishes from the dashboard
and the last known-good result is gone. Nobody who asked for "keep 90 days" meant
"and delete anything that has not run since". This is the setting operators wish
they had after the first prune rather than before it, so it defaults to on.

*Consequence*: the prune cannot promise a storage ceiling. A workspace with a
thousand dormant projects keeps five thousand runs regardless of the cutoff.
`--keep-min 0` opts out.

### D7 — A live run is not eligible, and a stuck one is reported rather than deleted

Runs where `is_live` holds — `QUEUED` or `RUNNING`
([models.py:537](../../src/test_hub/domain/models.py#L537)) — are skipped, and the
count of skipped live runs is printed and logged.

*Why*: a run older than the cutoff that is still `RUNNING` is one of two things —
a publisher that crashed months ago, or, on a short cutoff, a suite that is
genuinely still going. Deleting the second is data loss during a write; leaving
the first is a small leak that the printed count makes visible. `--include-unfinished`
covers the operator who has looked at the count and decided.

### D8 — Retention has its own scope, and never rides on the write token

**Amended 2026-09-09.** This originally said there would be *no* REST endpoint at
all, and that the CLI on the host was the only way to prune. That was the right
call given two scopes; it was the wrong call given a requirement to manage
retention from the dashboard, which is what the product was actually asked for.
The reasoning underneath it was never "no endpoint" — it was "not on the
publisher's credential". That part stands, and the endpoints are built on it.

`POST /retention/preview` and `POST /retention/prune` exist, behind
`require_admin`, which accepts exactly two things:

- **An admin token**, configured in `TEST_HUB_ADMIN_TOKEN_HASHES` — a separate
  list from `TEST_HUB_API_TOKEN_HASHES`. `require_write` is what every CI
  publisher token holds, and those tokens live in CI settings, get copied between
  repositories, and are the ones most likely to leak. A credential that can post
  a test result must not also be able to delete a year of them.
- **An unauthenticated loopback instance.** Anyone who can reach that port can
  already run `test-hub prune` in a terminal, so refusing would protect nothing
  and would make the dashboard lie about what it can do.

Explicitly *not* on that list: a public bind with `--insecure-no-auth`. Open
writes are a choice an operator makes knowingly; open deletes are a second choice
and have to be made separately.

*Why an endpoint at all*: the alternative — telling an operator to SSH to the
host to delete last quarter's runs — is not a security control, it is an
inconvenience, and inconveniences get worked around by handing more people shell
access. A narrow, separately-credentialed endpoint is the safer end state.

*Consequences*:

- A third scope now exists. `GET /retention` reports `admin` for *the calling
  request*, so the dashboard renders an explanation instead of a form that would
  403 on submit.
- `POST /retention/prune` requires `confirm: true`. A request sent by accident
  does nothing.
- The destructive control is a page (`#/settings`), not a menu item. A dropdown
  is the wrong shape for something irreversible, and the preview — a server-side
  count of exactly what would go — is what makes it safe. Changing any input
  discards the preview, so the button always describes the numbers on screen.
- `VACUUM` is deliberately not exposed. It rewrites the whole database behind an
  exclusive lock, which is not something a web request should start (D10).
- `DELETE /runs/{id}` — removing one bad import — is unblocked by the same scope
  now, but is still out of scope here and unbuilt.

### D9 — No `run.deleted` event

The prune emits nothing on the event bus and appends nothing to the log.

*Why*: `EventType` is a closed set and its docstring is explicit — "adding one is a
schema-version change" ([events.py:85](../../src/test_hub/domain/events.py#L85)).
Retention is an operator acting on storage, not a fact about a test run, and
paying a `SCHEMA_VERSION` bump to say so would put an operational concern into a
wire contract that every adapter and SDK version has to understand.

*Consequence*: a dashboard open on a pruned run gets a 404 on its next request
rather than a live update. Acceptable — the alternative is a schema change to
improve one refresh.

### D10 — Reclaiming disk is a separate, explicit step

Deleting rows does not shrink `test-hub.db`. SQLite marks pages free for reuse and
returns nothing to the filesystem without `VACUUM`, so a prune of 80% of the rows
leaves a file the same size, and the operator who ran it to free a disk concludes
it did not work. `--vacuum` runs it; the default does not.

*Why not by default*: `VACUUM` rewrites the entire database, needs up to twice the
current file size in free space, and holds an exclusive lock for the duration —
every one of which is worse than the problem on a disk that is already nearly
full. The blobs, which are usually the bulk of the bytes, are freed by D4 with no
vacuum at all.

*Consequence*: the command's output states the row counts deleted, the blobs
unlinked with their total size, and — when `--vacuum` was not passed — one line
saying the database file has not shrunk and how to shrink it.

## The cutoff grammar

Two mutually exclusive ways to say when, because operators think both ways:

| Form | Example | Meaning |
|---|---|---|
| `--older-than <duration>` | `--older-than 90d`, `6m`, `2w`, `1y` | Relative to now |
| `--before <date>` | `--before 2026-01-01`, `2026-01-01T00:00:00Z` | Absolute; a bare date is midnight UTC |

`d`/`w`/`m`/`y` are 1, 7, 30 and 365 days. **`m` is 30 days, not a calendar
month**, and `y` is 365 days, not a calendar year. Calendar arithmetic needs
`dateutil`, which is not a dependency and is not worth becoming one so that a
retention cutoff can land on the 31st; a fixed-length month is predictable, is one
line to test, and is the semantic every log-rotation tool already uses. The help
text and the reference both say the number of days each unit means, so nobody has
to guess.

A bare integer is days, so `--older-than 90` works.

## Schema change

One migration, `0002_retention_indexes`, adding one index:

```python
op.create_index("ix_idempotency_keys_run_id", "idempotency_keys", ["run_id"])
```

`IdempotencyKeyRow` declares no `__table_args__` at all
([models.py:389-408](../../src/test_hub/storage/models.py#L389-L408)), so its only
index is the `key` primary key and a delete by `run_id` is a full table scan —
once per batch, against a table that grows with every import. Every other table
the prune touches already leads an index with `run_id`: `ix_events_run_id_sequence`,
`ix_artifacts_run_id`, `ix_suites_run_id`, `ix_test_cases_run_id`,
`ix_attempts_run_id_test_id`.

Nothing else in the schema changes. That is the point of D3.

## Surface

```
test-hub prune [--older-than 90d | --before 2026-01-01]
               [--project KEY]...        restrict to projects, repeatable
               [--keep-min 5]            per-project floor, 0 to disable
               [--by-import-date]        cut on created_at instead of run time
               [--include-unfinished]    also prune live runs past the cutoff
               [--batch-size 100]
               [--dry-run]               plan only, touches nothing
               [--yes]                   skip the confirmation
               [--vacuum]                reclaim file space afterwards
               [--json]                  machine-readable result
               [--data-dir PATH]
```

`--dry-run` prints the plan — runs, per project, oldest and newest, the row counts
that would go and the bytes that would be freed — and is what the docs show first.
Without `--yes` and with a TTY the command confirms; without a TTY it refuses
rather than prompting into a pipe, so a cron entry that forgot `--yes` fails
loudly instead of hanging.

New settings, all documented in
[deployment.md](../deployment.md#configuration-reference):

| Variable | Default | Notes |
|---|---|---|
| `TEST_HUB_RETENTION_DAYS` | — | Default cutoff for `prune`. Nothing sweeps automatically; schedule the command. |
| `TEST_HUB_RETENTION_MIN_RUNS` | `5` | Per-project floor. `0` disables. |
| `TEST_HUB_ARTIFACT_GC_GRACE_HOURS` | `24` | A blob touched more recently than this is never unlinked. |

## Layering

`test_hub.service.retention`, alongside `imports` and `runs`. It takes a
`UnitOfWorkFactory` and `Settings`, and it does not import from `cli` or `api` —
the CLI is a thin caller, exactly as `ingest` is over `ImportService`. `RetentionPolicy`
(the resolved cutoff, floor and flags) and `PruneResult` (counts, bytes, skipped
live runs, batches committed) are plain dataclasses, so `--dry-run` and the real
run are one code path with a boolean, and phase two's scheduler is a loop around a
function that is already tested.

## Test plan

Each of these fails on today's code, which is the point of listing them:

1. **The cascade gap.** Import a run with an artifact, prune it, assert zero rows
   remain in `events`, `artifacts` and `idempotency_keys` for that `run_id`. This
   is D3's regression test and the one most likely to have caught the bug in
   review.
2. **The schema-drift guard.** Reflect the metadata, collect every table with a
   `run_id` column, assert each is either FK-cascaded to `runs` or in the prune's
   delete list. Fails when someone adds a table and forgets.
3. **Shared blob survives.** Two runs, same screenshot bytes, prune one: the row
   goes, the file stays, the surviving run can still open it.
4. **Unshared blob goes**, and its shard directories are pruned by the existing
   `delete()`.
5. **Grace window.** A digest with no referencing rows but a recent mtime is left
   alone; the same digest with an old mtime is unlinked.
6. **The floor.** Twenty runs in one project, all older than the cutoff,
   `--keep-min 5` → five survive, and they are the five newest.
7. **Live runs skipped**, counted in the result, and taken when
   `--include-unfinished` is passed.
8. **Cutoff boundary**, both directions, on a run with `started_at` null and
   `created_at` set — the D2 fallback.
9. **`--dry-run` writes nothing.** Snapshot every table count and the artifact
   tree before and after.
10. **Batching does not stall a publisher.** The `test_read_only_scope.py`
    pattern: prune from one thread with a small batch size, write from another,
    assert the writer completes well inside the busy timeout.
11. **Interrupted prune is resumable.** Kill after one batch, run again, end state
    matches the uninterrupted run.
12. **Duration parsing**, including that `6m` is 180 days and a bare `90` is days.

## What this does not do

- **No storage ceiling.** The policy is age, floored per project. "Keep the newest
  N GB" needs a size accounting this does not build.
- **No per-project or per-team policies.** One cutoff per invocation; `--project`
  narrows it, so different policies are different cron entries. Per-project
  retention stored as data is a schema change and a UI, and neither is warranted
  before someone asks.
- **No editable policy.** `TEST_HUB_RETENTION_DAYS` and the rest are environment
  settings, and the Settings page shows them read-only. Editing them from the
  dashboard means persisting settings somewhere, which is a store this product
  does not have — and a *saved* policy that nothing enforces on a timer would be a
  promise the server does not keep (D1).
- **No archival.** Pruned data is gone, not exported. An operator who wants a
  cold copy runs `test-hub export` for the window first — that is what the
  offline snapshot is for, and the docs say so next to the destructive command.
- **No idempotency-key expiry of its own.** Keys die with their run, so a key
  belonging to a run inside the window lives as long as the run does, which is far
  longer than the minutes it is useful for. A short independent TTL on that table
  is an obvious follow-up and needs `created_at` indexed rather than `run_id`.
- **Nothing for the WAL.** A large prune grows `test-hub.db-wal` until a
  checkpoint; the batching keeps that bounded, and
  [deployment.md](../deployment.md#backups) already tells the operator the
  database is three files.

## Consequences for the existing docs

Four claims went false the moment this shipped, and all four were load-bearing
sentences that operators were relying on. All four were rewritten with it:

- [scaling.md](../scaling.md) — "No retention, no expiry, no deletion endpoint, no
  artifact GC." The deletion endpoint stays true (D8); the rest does not. Its
  `ArtifactStore` section also listed the blob interface, which has gained a
  method, and its PostgreSQL section claimed idempotency keys "accumulate
  forever", which retention now bounds.
- [security.md](../security.md) — "There is no data retention, expiry or
  deletion… Storage grows monotonically." Now says why the *endpoint* is absent
  rather than implying nobody got round to it.
- [deployment.md](../deployment.md) — "Data-directory free space (nothing is ever
  deleted)" under **What to watch**, plus a new **Retention** section and three
  rows in the configuration reference.
- [README.md](../../README.md) — retention was in neither **Features** nor **Not
  in the first release**. It is now a feature.

One more that was not on the list and should have been:
`test_hub.cli.__init__` opened by promising that "anything you can do from the
terminal you can also do over HTTP and from Python". `prune` is the first command
for which that is false, and deliberately so (D8), so the docstring now says which
command and why.
