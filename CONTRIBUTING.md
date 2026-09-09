# Contributing to Test Hub

Thanks for being here. This document is the whole onboarding: how to get it
running, how the code is arranged and why, what the gates are, and how a release
happens.

Before anything else, please read [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), and
if you have found a security problem, **stop and read
[`SECURITY.md`](SECURITY.md)** — it does not go in a pull request.

---

## Getting set up

You need **Python 3.11 or newer** and nothing else. No Node, no database, no
Docker. That is a deliberate constraint
([ADR-0001 D7](docs/adr/0001-architecture.md)), not an accident: the frontend is
hand-written CSS and classic JavaScript shipped inside the wheel, so there is no
build step to install.

### With pip

```bash
git clone https://github.com/prashanth-sams/test-hub
cd test-hub

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### With uv

Faster, and the same result:

```bash
git clone https://github.com/prashanth-sams/test-hub
cd test-hub

uv venv                          # creates .venv
uv pip install -e ".[dev]"
source .venv/bin/activate
```

Or skip the activation entirely and prefix commands with `uv run`:

```bash
uv run pytest
uv run ruff check .
uv run test-hub demo
```

### Check it works

```bash
test-hub --version
test-hub demo                    # seeds realistic sample data
test-hub serve                   # http://127.0.0.1:8000
```

`test-hub demo` writes to the platform data directory
(`~/Library/Application Support/test-hub` on macOS,
`~/.local/share/test-hub` on Linux, `%LOCALAPPDATA%\test-hub` on Windows). To
keep experiments out of it, point `TEST_HUB_DATA_DIR` at a scratch folder:

```bash
export TEST_HUB_DATA_DIR="$PWD/.local-data"     # already in .gitignore
```

Every setting is an environment variable with a `TEST_HUB_` prefix, or a line in
a `.env` file — see `Settings` in [`src/test_hub/config.py`](src/test_hub/config.py),
which is the only place any of them are defined.

---

## How the code is arranged

```
cli ──▶ api ──▶ service ──▶ storage ──▶ (sqlite / filesystem)
 │       │         │
 │       │         └──▶ domain ◀── adapters
 └──▶ sdk ─(http)─▶ api
```

**Arrows are imports, and they only point one way.** That is the single
architectural rule of this project and most review comments come back to it.

| Package | What lives there | What it may import |
|---|---|---|
| `domain/` | The contract: statuses, identity hashing, Pydantic models, events, metric formulas | **Nothing from the project.** No I/O of any kind |
| `adapters/` | Report formats → domain events | `domain` only |
| `storage/` | SQLite, SQLAlchemy models, repositories, the artifact store, Alembic | `domain` |
| `service/` | Folding events into rows, queries, the event bus | `domain`, `storage`, `adapters` |
| `api/` | FastAPI app, auth, request/response models | `service` and below |
| `cli/` | The Typer app | anything |
| `sdk/` | The Python publishing client | `domain`; talks to the API over HTTP |
| `web/` | The dashboard, shipped in the wheel | — |

Three consequences worth internalising:

- **`domain` imports nothing.** If you need a setting inside a domain function,
  pass it in as an argument. `python -c "import test_hub.domain"` must work with
  nothing else configured, and a cycle breaks it loudly.
- **Adapters do not touch the database, HTTP, or config.** An adapter turns bytes
  into events, full stop. That is what makes file import and live publishing
  literally the same code path after the adapter returns
  ([ADR-0001 D5](docs/adr/0001-architecture.md)), and it is what makes an adapter
  testable against a fixture file with no fixtures beyond the file.
- **Events are the write model; tables are the read model**
  ([ADR-0001 D2](docs/adr/0001-architecture.md)). All ingestion normalises into a
  stream of CloudEvents-shaped domain events which are stored and then folded
  into rows. Aggregate counts are always *derived* by folding, never taken from
  what a publisher claimed its totals were.

Read [`docs/adr/0001-architecture.md`](docs/adr/0001-architecture.md) once. It is
the nine decisions everything else follows from, with the rejected alternatives
and the reasons. It will save you a review round trip.

### The frozen parts

Some modules are the contract that everything else is written against, and
changing one is a much bigger deal than its diff suggests:

- `domain/models.py`, `domain/events.py` — the wire contract. Adding an optional
  field is compatible. Anything else needs a `SCHEMA_VERSION` bump in
  [`src/test_hub/version.py`](src/test_hub/version.py) and a note in
  `docs/schemas/`.
- `domain/identity.py` — **the sharpest edge in the codebase.** Changing what
  goes into `identity_source()` silently re-identifies every test that has ever
  been recorded, which throws away the history that flakiness and trends are
  computed from. There is no migration for it. If you think it needs to change,
  open an issue first.
- `errors.py` — the error taxonomy, mapped to HTTP in exactly one place and to an
  exit code in exactly one place, so a failure reads the same through
  `test-hub ingest` and through `POST /api/v1/imports`.

---

## Running the tests

```bash
pytest                                   # the suite
pytest -x -q                             # stop at the first failure
pytest tests/test_identity.py            # one file
pytest -k "junit and not slow"           # by name
pytest --cov --cov-report=term-missing   # with coverage
pytest -m slow                           # the big-input / server-starting ones
```

Tests marked `slow` build large inputs or start a server. They run in CI; skip
them locally with `-m "not slow"` when you are iterating.

The suite needs no services and no network. If a test you are writing wants
either, it is a sign the thing under test has a dependency it should not have.

### What a good test looks like here

- **Adapters**: a fixture file in `tests/fixtures/` and an assertion on the
  event stream. Invent the fixture — never paste a real report from an employer,
  which is both a licensing problem and, per [`SECURITY.md`](SECURITY.md) §5, a
  good way to commit someone's credentials.
- **Domain**: pure functions, so test the interesting values. The docstrings in
  `domain/identity.py` and `domain/status.py` contain doctests; keep them true.
- **Anything with a date**: UTC, timezone-aware, always. `utcnow()` in
  `domain/models.py` is the only clock the domain uses.
- **A bug fix**: a test that fails before your change. If you cannot write one,
  say so in the pull request and say why.

---

## The gates

CI runs these on Python 3.11, 3.12 and 3.13 on Linux, plus 3.12 on macOS and
Windows. Run them locally first; they are fast.

```bash
ruff check .            # lint
ruff format --check .   # formatting
mypy                    # types, strict
pytest --cov            # tests
```

`ruff check --fix .` and `ruff format .` fix most of what the first two complain
about.

### Style, and what reviewers actually look at

Configuration lives in `pyproject.toml`; these are the parts that are decisions
rather than defaults.

- **Line length 100.**
- **`mypy --strict` passes.** Full annotations everywhere. A new `# type: ignore`
  needs a comment beside it saying why, and ideally a link.
- **`from __future__ import annotations` at the top of every module**, and
  `__all__` on every module.
- **`pathlib`, never `os.path`.** Enforced by ruff's `PTH` rules.
- **No `print()` outside `cli/`.** Enforced by `T20`. Everything else uses
  `get_logger(__name__)` from [`src/test_hub/logging.py`](src/test_hub/logging.py),
  which is where redaction is installed — a `print` bypasses it and can put a
  secret on stdout.
- **No bare `except`.** Catch something specific, or `except Exception` with a
  reason.
- **UTC everywhere**, always timezone-aware.
- **No new runtime dependencies** beyond what `pyproject.toml` already lists.
  Every one of them is in the Docker image, in every user's site-packages and in
  our security surface. Dev dependencies are an easier conversation.

**Comments explain *why*, never *what*.** A comment that restates the line under
it will be asked about in review. Module docstrings are substantial: what the
module is for, the decision behind it, the trap a reader would otherwise fall
into. [`domain/identity.py`](src/test_hub/domain/identity.py) and
[`adapters/base.py`](src/test_hub/adapters/base.py) set the register — match it.

---

## Adding a report format

This is the part of the project most likely to grow, and it is designed so that
it can grow **outside** this repository.

Adapters are entry-point plugins, and Test Hub's own two are registered through
exactly the same mechanism a third-party package uses — there is no privileged
built-in path ([ADR-0001 D5](docs/adr/0001-architecture.md)):

```toml
[project.entry-points."test_hub.adapters"]
my-format = "my_package.adapter:MyAdapter"
```

So your first decision is where it should live. **Your own package is usually the
right answer**: it releases on your schedule, you own the fixtures, and users
install it with `pip install test-hub-myformat`. Propose it for core when the
format is one most teams have (the bar the two shipped adapters clear) and you
can supply anonymised fixtures we can keep forever.

Either way, the guide is **[`docs/adapters.md`](docs/adapters.md)**. The short
version:

1. Implement the `ReportAdapter` protocol from
   [`src/test_hub/adapters/base.py`](src/test_hub/adapters/base.py). It is a
   `Protocol`, so there is no base class you must inherit — though `BaseAdapter`
   has the boring extension/content-type detection already written.
2. `detect(stream, filename, content_type) -> float` returns a confidence in
   `[0.0, 1.0]`. The scale is documented in that module's docstring: `0.3` for "the
   extension fits", `0.6` for "the content sniffs right", `0.9` for "unmistakably
   mine". **Read a few kilobytes, not the whole file** — `detect` runs for every
   installed adapter on every import.
3. `parse(stream, context) -> Iterable[DomainEvent]` does the work. Use
   `context.next_sequence()` for sequence numbers rather than counting yourself,
   and put everything you cannot represent exactly on `context.warnings` — a
   warning on the import, never a silent approximation and never an exception.
4. Map statuses with `normalize_status()` from `domain/status.py`, and compare the
   result against `UNKNOWN` so an unrecognised dialect surfaces as a warning
   rather than as a quietly wrong pass rate. The original string is always kept in
   `TestAttempt.original_status`.
5. **If the format is XML, parse it with `defusedxml`.** `xml.etree`, `lxml` and
   `minidom` are not allowed anywhere in `src/` — see
   [`SECURITY.md`](SECURITY.md) §2.
6. Add a fixture and a test that asserts on the event stream.

[`src/test_hub/adapters/junit_xml.py`](src/test_hub/adapters/junit_xml.py) is a
complete worked example of all of the above.

---

## Commits and pull requests

We use [Conventional Commits](https://www.conventionalcommits.org/). The subject
line is a sentence you could put after "this commit will…":

```
<type>(<optional scope>): <imperative summary, lower case, no full stop>

<body: why, not what — the diff already says what>

<footer: Fixes #123 / BREAKING CHANGE: …>
```

| Type | For |
|---|---|
| `feat` | a new capability a user can see |
| `fix` | a bug fix |
| `docs` | documentation only |
| `refactor` | behaviour-preserving change |
| `perf` | a measured speed or memory improvement |
| `test` | tests only |
| `build` | packaging, dependencies, Docker |
| `ci` | workflows |
| `chore` | everything else |

Useful scopes: `adapters`, `domain`, `storage`, `service`, `api`, `cli`, `web`,
`demo`, `sdk`, `docs`, `docker`.

```
feat(adapters): add TRX adapter for MSTest and NUnit output
fix(storage): keep attempt ordering stable when two events share a timestamp
docs(security): document the redaction limits honestly
```

A change to the wire contract needs `BREAKING CHANGE:` in the footer **and** a
`SCHEMA_VERSION` bump.

For pull requests, the template asks the questions we care about. Two of them
matter more than the rest: *what is the wire-contract impact* (answer it even
when the answer is "none") and *what did you actually run*.

Small, focused pull requests get reviewed quickly. A branch that renames things
and fixes a bug is two pull requests.

Add an entry to [`CHANGELOG.md`](CHANGELOG.md) under `## [Unreleased]` for
anything a user would notice.

---

## Releasing

For maintainers. The release is fully automated from a tag, with no API token
anywhere — see [`.github/workflows/release.yml`](.github/workflows/release.yml).

### One-time: configure Trusted Publishing on PyPI

**Do this before the first tag.** If it is not set up, the publish job fails with
a `403` that reads like a permissions bug and is not one.

1. Sign in to <https://pypi.org> and go to **Your projects → Publishing** (for a
   project that does not exist yet: **Account settings → Publishing → Add a new
   pending publisher**).
2. Add a **GitHub Actions** publisher with exactly these values:

   | Field | Value |
   |---|---|
   | PyPI project name | `test-hub` |
   | Owner | `prashanth-sams` |
   | Repository | `test-hub` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   All five are checked. A mismatch in any one of them — including the
   environment — is a rejected upload.
3. In the GitHub repository, create an environment named **`pypi`**
   (*Settings → Environments*) and add yourself as a required reviewer. This is
   what makes publishing a decision someone makes rather than a side effect of
   pushing a tag.

There is a commented TestPyPI variant at the bottom of `release.yml` for
rehearsing the pipeline. It needs its own pending publisher at
<https://test.pypi.org> with environment name `testpypi`, and its own GitHub
environment of that name.

### Cutting a release

1. Update `__version__` in
   [`src/test_hub/version.py`](src/test_hub/version.py) — the single source of
   truth, read by Hatchling at build time.
2. Move `## [Unreleased]` in `CHANGELOG.md` to the new version with today's date,
   and update the link definitions at the bottom.
3. Merge that to `main` and let CI go green.
4. Tag and push:

   ```bash
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```

5. The `Release` workflow builds, runs `twine check --strict`, and **verifies the
   tag matches `__version__`** before anything is published — PyPI never lets you
   re-upload a filename, so that check has to fail before the upload rather than
   after.
6. Approve the `pypi` environment when GitHub asks.
7. Write the GitHub release notes from the changelog entry.

### If PyPI rejects the name

PEP 541 confusability checks can refuse `test-hub` as too similar to an existing
project (PyPI folds `-`, `_` and `.` together and lowercases, so `test_hub` and
`testhub` are the same name to it). That verdict tends to arrive mid-release. The
escape hatch is pre-agreed:

```bash
python tools/set_dist_name.py testhub-io --dry-run   # see the diff first
python tools/set_dist_name.py testhub-io
```

It rewrites exactly one line — `name` in the `[project]` table — and refuses to
write anything if any other line would change. The import package (`test_hub`)
and the console script (`test-hub`) are unaffected, so nobody's code or shell
history breaks; only the string in `pip install` changes.

After renaming, **register the Trusted Publisher for the new project name** and
update the environment URL in `release.yml`.

---

## Docs and the website

- `docs/` is long-form Markdown, read on GitHub.
- `site/` is the documentation website: body fragments with JSON front matter in
  `site/content/`, wrapped by `site/tools/build.py` into `site/docs/`. The built
  output **is committed** — GitHub Pages serves it straight from the repository —
  so rebuild and commit both when you change a fragment.
- No runtime dependencies, no build tooling, no CDN. Same rule as the dashboard.

---

## Getting help

- **Questions and ideas** — [Discussions](https://github.com/prashanth-sams/test-hub/discussions).
  Answers there tend to become documentation.
- **Bugs** — an issue, with a minimal report file if you can.
- **Security** — [`SECURITY.md`](SECURITY.md). Never a public issue.

If something in this document is wrong or out of date, that is a bug worth
fixing, and a very welcome first pull request.
