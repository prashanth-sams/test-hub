# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two version numbers move independently and it is worth knowing which is which:

- **`__version__`** — the release, in [`src/test_hub/version.py`](src/test_hub/version.py).
  What the headings below refer to.
- **`SCHEMA_VERSION`** — the public wire contract: everything in
  `test_hub.domain.models` and `test_hub.domain.events`. It moves only on a
  breaking change to that contract, and a release that moves it says so in its
  own **Wire contract** section.

While Test Hub is pre-1.0, a minor version may contain a breaking change. Every
one of those is listed under **Changed** or **Removed** with the migration.

## [Unreleased]

### Added

- **Six more pages in the rail, and a rail that can hold them.** Test Suites
  (every suite folded by path across the recent runs, sortable, searchable, and
  expandable into its tests), Test Metrics (one row per test identity with runs,
  pass rate, retries, flakiness and where its time goes), Screenshots, API Logs,
  Test Coverage and Test Steps. The rail is now dark in both themes, grouped
  under *Results* and *Evidence*, and on a phone it becomes a full-width
  scrolling strip that brings the current route into view. All seven pages that
  read run detail share one fetch, so moving between them costs nothing; see
  [the architecture notes](docs/architecture.md#what-the-detail-pages-cost).
- **Honest empty states for what Test Hub does not model.** API Logs says there
  is no request/response object in the schema and shows the log evidence there
  is; Test Coverage says the report is stored rather than parsed, so it prints no
  percentage; Test Steps says the finest grain recorded is the attempt, and
  renders `attempt.metadata.steps` when a publisher supplies it. In an offline
  export, the pages that read attachments say the export carries the runs and not
  the files — rather than reporting that nothing was captured.
- **Sample data with something attached.** `test-hub demo` now publishes the
  files a run leaves behind: screenshots for the browser suite's failures, a
  request log, a coverage report and a trace. They are generated, never captured
  — including the PNGs, which are drawn by a thirty-line encoder in
  `test_hub.demo.evidence` rather than by adding an image dependency.
- **An analytics page.** A third view in the rail, one level below the portfolio
  overview and above the run drill-down. Six tiles (stability score, pass rate of
  the last run and its delta, flaky tests, what is failing now, runs analysed,
  time in tests), failures grouped by the exception that raised them with the
  tests behind each type, suite hotspots, ownership, where the run's time goes,
  and a per-test verdict — stable, flaky, broken or quiet — with the shape of its
  recent history. It reads the five most recent finished runs in full and caches
  them; see [the architecture notes](docs/architecture.md#what-the-detail-pages-cost).
- **A richer headline on the overview and the run page.** The health card leads
  with the number of results, the donut and a strip of outcome counters covering
  every status a test can end on, plus flaky and retried. On a run page those
  counters are buttons: pressing one filters the test table below to that outcome
  and scrolls to it.
- **Data retention.** `test-hub prune` deletes runs older than a cutoff, everything
  under them, and the content-addressed artifact blobs left unreferenced as a
  result. `--older-than 90d|6m|2w|1y`, or `--before <date>`; `--dry-run` prints the
  plan and touches nothing. New settings `TEST_HUB_RETENTION_DAYS` (the command's
  default cutoff — nothing sweeps on its own), `TEST_HUB_RETENTION_MIN_RUNS`
  (runs always kept per project, default 5) and `TEST_HUB_ARTIFACT_GC_GRACE_HOURS`
  (default 24). Deliberately CLI-only: the write scope is what CI publisher tokens
  hold. See [ADR-0002](docs/adr/0002-data-retention.md).
- `ArtifactStore.collect_unreferenced()`, which frees a content-addressed blob only
  once no row anywhere still names its digest.
- **Retention from the dashboard.** The gear menu's *Retention and data…* opens a
  Settings page that shows the configured policy and clears old data by a cutoff in
  hours, days, weeks, months or years. It previews exactly what would be deleted —
  runs, tests, attempts, events and artifact bytes, broken down by project — before
  anything is, and changing any field discards the preview so the delete button
  always describes the numbers on screen.
- **An administrator scope.** `POST /api/v1/retention/preview` and
  `/retention/prune` are the only destructive endpoints and are guarded by
  `TEST_HUB_ADMIN_TOKEN_HASHES`, a separate list from the publisher tokens —
  create one with `test-hub token create --admin`. An unauthenticated loopback
  instance is admin already; a public bind with `--insecure-no-auth` is not.
  `GET /api/v1/retention` reports whether the calling request may prune, so the
  dashboard never draws a control that would 403.
- **Hours are a retention unit.** `--older-than 12h`, for clearing up after a bad
  import. `UNIT_DAYS` became `UNIT_HOURS`.
- **A left navigation rail.** Overview and Runs moved out of the header into a
  216px column that also carries the brand and, at its foot, the dark-mode
  switch; each route has an icon from the dashboard's own icon set. Below 860px
  the rail folds back into a horizontal bar, so a phone still spends its width
  on content.
- **A settings menu in the dashboard header**, replacing the light/dark toggle in
  the header. It holds the doorway to retention and an About block that says
  whether the page is a live server or an offline export, and which version and
  schema it is. The theme itself is a single "Dark mode" switch at the foot of
  the rail, reading the theme actually on screen — so a reader who has never
  chosen sees their operating system reflected back.
  Built on a new `ui.menu()` primitive with the keyboard contract the dashboard
  did not previously have anywhere: arrow keys, Home/End, and Escape returning
  focus to the trigger.

### Fixed

- **The dashboard was served without `Cache-Control`.** `StaticFiles` sends an
  `ETag` and a `Last-Modified` but no freshness directive, which browsers are free
  to cache heuristically — so an upgraded Test Hub could keep running the previous
  release's JavaScript against the current release's markup, with no error and no
  console message. `/` and `/assets/*` now send `no-cache`, which revalidates and
  still answers 304. The API is unchanged.
- The live dashboard never knew its own version: `LiveDataProvider.meta` carried
  only the API base, so the footer showed no version or schema in live mode. It
  now reads both from `/healthz` at startup.
- The theme preference is three-state underneath the switch — light, dark, or no
  stored choice at all — so an untouched dashboard still follows
  `prefers-color-scheme` in both directions, including a change made while the
  page is open.
- `FilesystemArtifactStore.put()` now updates the mtime of content it finds already
  stored, instead of leaving it at the first upload's. Nothing read that mtime
  before; the artifact sweep does, and without the bump it could unlink bytes an
  in-flight import was about to claim.

### Changed

- New migration `0002_retention_indexes` adds `ix_idempotency_keys_run_id`. That
  table had no index but its primary key, so deleting by run scanned it whole.
- ADR-0002 D8 is amended. It originally ruled out a REST endpoint for pruning
  entirely; the reasoning underneath it was never "no endpoint" but "not on the
  publisher's credential", and that is what the admin scope implements.

<!--
Add entries here as you merge, under these headings, in this order. Delete the
ones you do not use.

### Added        — new capability
### Changed      — behaviour that was already there and now differs
### Deprecated   — still works, will not in a future release
### Removed      — gone
### Fixed        — bug fixes
### Security     — anything with a security consequence

Write for the person reading `pip list --outdated` and deciding whether to
upgrade, not for the person who wrote the patch. Link the issue or pull request.
-->

## [0.1.0] — 2026-09-09

First release. A complete vertical slice rather than scaffolding: ingest a real
report, see it in a dashboard, export it as one file, and publish to it live.

### Added

**Ingestion**

- Import test reports from a file, from `POST /api/v1/imports`, or from a live
  publisher — all three normalise into the same stream of CloudEvents-shaped
  domain events, so there is one code path after the adapter returns.
- **JUnit / xUnit XML** adapter, covering the dialects that Surefire, Jest,
  pytest, Playwright, PHPUnit and Go's `gotestsum` actually emit. Parsed with
  `defusedxml`: no DTDs, no external entities, no billion laughs.
- **Canonical JSON** adapter — Test Hub's own format, for publishers that can
  emit it directly and skip the lossy trip through someone else's XML.
- Format auto-detection by scored confidence, with `--format` to override it.
  Unrecognised input lists the installed adapters and what they accept instead of
  only saying no.
- Adapters are entry-point plugins (`test_hub.adapters`). The two built-in ones
  register through exactly the mechanism a third-party package uses — there is no
  privileged built-in path.
- Parser warnings: anything an adapter could not represent exactly is recorded on
  the run and shown on the import, rather than silently approximated.

**Model and metrics**

- One canonical model across every framework — workspace, project, run, suite,
  test, attempt — with the original status string always kept beside the
  normalised one.
- Stable test identity: a UUIDv5 over project, suite path, class, test name and
  normalised parameters, deliberately excluding timestamps, line numbers, shard
  ids and run ids. History survives re-runs, re-ordering, re-parameterisation and
  a move to a different machine. The hashed string is documented and reproducible
  from another language.
- Retries are first-class. A test that failed and then passed is green *and*
  flaky, and both facts stay available because the attempts are never overwritten.
- Pass rate, failure rate, duration percentiles and flake rate, with the exact
  formulas written down in `docs/metrics.md`.

**Dashboard**

- Overview, run list, run detail and test detail, with history and trends.
- Live updates over Server-Sent Events, with `Last-Event-ID` replay on reconnect
  and a `resync` directive when a gap is larger than the buffer.
- No framework, no bundler, no CDN, no Node at build or run time. Hand-written
  CSS and classic JavaScript shipped inside the wheel; charts are inline SVG.

**Snapshot export**

- `test-hub export` writes a single self-contained HTML file that opens from
  `file://` with the network off — one file to attach to a ticket or email to
  someone who will never install anything.
- It is the *same* dashboard code as the live one, behind a different data
  provider, so the two cannot drift.

**CLI and SDK**

- `test-hub serve`, `ingest`, `publish`, `export`, `demo`, `token` and `formats`.
- `test-hub demo` seeds realistic sample data, so the dashboard can be evaluated
  in about a minute without wiring up CI.
- A Python SDK for publishing results from a test run as it happens.

**Deployment**

- SQLite with WAL and a content-addressed filesystem artifact store, behind
  repository interfaces. `pip install test-hub` and run — no services.
- Multi-stage `Dockerfile` (non-root uid 10001, `/data` volume, healthcheck) and
  a `docker-compose.yml` that works on `127.0.0.1:8000` out of the box.
- `tools/set_dist_name.py`, the documented escape hatch if PyPI rejects the
  distribution name as confusable.

### Security

These are release-one properties rather than fixes, and they are listed here
because they are the reason several of the design decisions look the way they do.
[`SECURITY.md`](SECURITY.md) has the full threat model.

- **Refuses to bind a non-loopback interface without authentication.** Either
  configure tokens or pass `--insecure-no-auth`, which warns on every start and
  banners the dashboard. There is no silent path to an open, unauthenticated Test
  Hub on a network.
- **Secret redaction on the way in**, applied to both log records and stored
  failure text: bearer and basic credentials, PEM private keys, database URLs
  with inline passwords, AWS keys, GitHub and Slack tokens, JWTs, and generic
  `password=` / `token=` shapes. Extend it with
  `TEST_HUB_REDACTION_PATTERNS`. It is a strong second line of defence, not a
  guarantee — `SECURITY.md` §5 says so plainly.
- **XSS defences for untrusted test output.** Test names and failure messages
  reach the DOM only through escaping helpers; snapshot JSON is embedded with
  `<`, `>`, `&`, U+2028 and U+2029 rewritten as `\uXXXX` escapes, so a
  `</script>` in a stack trace cannot break out of the block it lives in; and a
  restrictive default CSP (`default-src 'none'`, no `unsafe-inline`, no
  `unsafe-eval`, `frame-ancestors 'none'`).
- **XXE closed** — `defusedxml` everywhere XML is parsed.
- **Size caps enforced while streaming**, including a separate ceiling
  (`TEST_HUB_MAX_DECOMPRESSED_BYTES`) on what a gzipped upload may expand to —
  checked during expansion, so a decompression bomb never lands in memory. A
  truncated gzip stream is reported as such rather than passed on as half a
  report. Archives (zip, tar) are not accepted at all: "which file in here is the
  report?" has no safe default.
- **No path traversal surface for artifacts**: storage is content-addressed by
  SHA-256, so a user-supplied filename never touches the filesystem.
- Tokens stored as salted **PBKDF2-HMAC-SHA256** hashes (120 000 iterations),
  compared in constant time, shown once, never accepted as a command-line
  argument. No token is baked into the Docker image.
- CORS off by default.

### Wire contract

`SCHEMA_VERSION` is **1**. Everything in `test_hub.domain.models` and
`test_hub.domain.events` carries it. The CloudEvents `specversion` is `1.0`.

### Known limits

Documented rather than hidden, with the interfaces to replace them already in
place — see [ADR-0001](docs/adr/0001-architecture.md) and `docs/scaling.md`.

- Single-process write throughput. SQLite serialises writers; that is correct,
  not fast.
- The SSE bus is in-process, so `serve` runs one worker and says why.
- Snapshot export fully materialises the data set — it is not meant for a million
  tests.
- One workspace. Every row carries `workspace_id` from the first migration, but
  the open-source edition creates only `default` and offers no UI to change it.

[Unreleased]: https://github.com/prashanth-sams/test-hub/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/prashanth-sams/test-hub/releases/tag/v0.1.0
