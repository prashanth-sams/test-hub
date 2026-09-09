# Repository plan

The layout, who owns what, and the order the first release is built in.

## Tree

```
test-hub/
├── pyproject.toml              # packaging, tool config (ruff, mypy, pytest)
├── README.md  LICENSE  CHANGELOG.md  CONTRIBUTING.md  SECURITY.md
├── Dockerfile  docker-compose.yml  .dockerignore
├── .github/workflows/          # ci.yml, release.yml (Trusted Publishing), site.yml
├── docs/                       # long-form documentation (Markdown)
│   ├── adr/0001-architecture.md
│   ├── architecture.md         # the system as built
│   ├── schemas/                # versioned wire-schema reference + generated JSON Schema
│   ├── adapters.md             # adapter-authoring guide
│   ├── api.md                  # REST reference with curl examples
│   ├── metrics.md              # pass rate, failure rate, duration, flaky — exact formulas
│   ├── events.md               # the CloudEvents envelope and every event type
│   ├── security.md  scaling.md  deployment.md
│   └── ci/github-actions.md  ci/jenkins.md
├── site/                       # the documentation website (static, no runtime deps)
│   ├── content/*.html          # body fragments with JSON front matter
│   ├── tools/build.py          # wraps fragments in the shared shell → site/docs/
│   ├── assets/css/site.css  assets/js/site.js
│   └── docs/                   # built output, committed, served by GitHub Pages
├── src/test_hub/
│   ├── config.py               # Settings (env: TEST_HUB_*), secure defaults
│   ├── logging.py              # structured logs + configurable secret redaction
│   ├── errors.py               # one error taxonomy, mapped to HTTP once
│   ├── domain/                 # the contract — no I/O in this package
│   │   ├── status.py           # canonical statuses + normalization table
│   │   ├── identity.py         # stable test identity hashing
│   │   ├── models.py           # Pydantic v2 canonical models (schema_version 1)
│   │   ├── events.py           # CloudEvents envelope + typed payloads
│   │   └── metrics.py          # pass rate / failure rate / duration / flaky
│   ├── adapters/               # base.py protocol + registry, canonical_json, junit_xml
│   ├── storage/                # base.py protocols, db, ORM models, repositories,
│   │   └── migrations/         # artifacts store, Alembic
│   ├── service/                # ingest (fold events → rows), queries, event bus
│   ├── api/                    # FastAPI app, security, public request/response models
│   ├── cli/                    # Typer app: serve, ingest, publish, export, demo, token
│   ├── sdk/                    # Python publishing client
│   ├── export/                 # standalone HTML snapshot writer
│   ├── demo/                   # realistic seed data
│   └── web/                    # static dashboard shipped in the wheel
└── tests/                      # pytest; fixtures/ holds invented reports
```

## Dependency direction

```
cli ──▶ api ──▶ service ──▶ storage ──▶ (sqlite / filesystem)
 │       │         │
 │       │         └──▶ domain ◀── adapters
 ├──▶ export ──────────▶ domain
 └──▶ sdk ─(http)─▶ api
```

`domain` imports nothing from the project. `adapters` import only `domain`.
`storage` never imports `api`. This is enforced by review, and by the fact that a
cycle breaks `python -c "import test_hub.domain"`.

## Build order (vertical slice first)

| # | Milestone | Done when |
|---|---|---|
| 1 | Spine: packaging, config, domain, protocols | `import test_hub` works; `pytest tests/test_domain*.py` green |
| 2 | Storage + migrations | `alembic upgrade head` creates the schema; repositories round-trip |
| 3 | Adapters | JUnit XML and canonical JSON produce identical event streams for equivalent input |
| 4 | Service + API | `POST /api/v1/imports` returns a run id; `GET /api/v1/runs/{id}` shows it |
| 5 | CLI `serve`/`ingest` | dashboard loads real data |
| 6 | Frontend | overview, runs, run detail, test detail render from `LiveDataProvider` |
| 7 | SSE | a run posted from `curl` appears without a refresh |
| 8 | Exporter | `test-hub export` opens from `file://` with the network off |
| 9 | `publish`, SDK, `demo` | remote publish works against `serve` |
| 10 | Hardening | auth, CSP, limits, redaction, size caps |
| 11 | Tests, docs, CI, Docker, site | everything in the definition of done |

Everything after milestone 8 is additive; nothing after milestone 4 is allowed to
change the wire contract without a `schema_version` bump.

## Naming

- Distribution: `test-hub` (override with `TEST_HUB_DIST_NAME` at build time if PyPI
  rejects it as confusable — see `docs/deployment.md#packaging-name`).
- Import package: `test_hub`. Console script: `test-hub`.
- Entry-point group for plugins: `test_hub.adapters`.
- Environment prefix: `TEST_HUB_`.
