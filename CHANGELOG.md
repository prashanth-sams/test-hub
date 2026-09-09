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

Nothing yet.

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
