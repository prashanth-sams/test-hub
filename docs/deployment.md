# Deployment

Running Test Hub for a team, rather than for yourself. Everything here is about a
single instance serving a group of people and their CI — which is what the first
release is built for. When you outgrow it, [scaling.md](scaling.md) says what
replaces which piece.

Three decisions, in this order:

1. **Where does it run?** A container, or a `pip install` under a service
   manager. Both are supported; the container is the one with a tested recipe.
2. **How is it authenticated?** Anything not on loopback needs tokens. Test Hub
   refuses to start otherwise — see [security.md](security.md#binding).
3. **What terminates TLS?** Test Hub speaks plain HTTP and always will. A bearer
   token on plain HTTP across a network is a token you have given away.
4. **Which database?** SQLite, the default, needs nothing installed and is the
   faster of the two for one instance. PostgreSQL is for when you want the
   database on a server — see [Choosing a database](#choosing-a-database).

## Docker

The [`Dockerfile`](../Dockerfile) is two stages. Stage one builds a wheel from
the checkout; stage two installs *only* that wheel into a fresh
`python:3.12-slim`. The runtime image therefore contains exactly what
`pip install test-hub` gives a stranger — which makes the image a continuous test
that the wheel is installable and complete, and is why a missing data file or an
entry point that does not resolve shows up here rather than in a bug report.

```shell
$ docker build -t test-hub .
$ docker run --rm -p 127.0.0.1:8000:8000 -v test-hub-data:/data \
    -e TEST_HUB_INSECURE_NO_AUTH=true test-hub
```

Things in the image worth knowing before you change them:

- **It binds `0.0.0.0`**, because a container that binds `127.0.0.1` is a
  container you cannot reach. Inside its own network namespace that is still not
  a public interface — but Test Hub cannot know that, so a bare `docker run`
  with no token configuration exits **78** with a message telling you what to do.
  That is working as designed. Give it token hashes, or opt out explicitly with
  `TEST_HUB_INSECURE_NO_AUTH=true` *and* publish the port to loopback only.
- **It runs as uid 10001, gid 0**, non-system and non-root. `VOLUME ["/data"]` is
  declared *after* the `chown`, so a fresh named or anonymous volume is seeded
  with the right ownership; declared before, the volume would come up root-owned
  and the service would fail to write its database.
- **`WORKDIR` is `/data`.** Not cosmetic: `docker compose exec test-hub test-hub
  export -o report.html` would otherwise write to `/`, which the Compose file
  mounts read-only. Landing on the volume is what lets you retrieve the file.
- **The healthcheck uses `python -c urllib.request`**, not `curl`. The slim base
  has no curl, and adding one is 5 MB and another CVE feed for something the
  runtime already does.
- **No token is baked in, and none ever should be.** An image layer is readable
  by anyone who can pull it and survives every `docker rmi` of the tag.
- [`.dockerignore`](../.dockerignore) keeps `data/`, `*.db`, `artifacts/` and
  `.env` out of the build context. Copying a laptop database into an image would
  also copy every secret its test output ever leaked.

## Compose

[`docker-compose.yml`](../docker-compose.yml) is a working local deployment.
Verified with the Compose CLI (no Docker daemon was available in the environment
these docs were written in, so the image was **not** built or run here — only the
file was parsed and validated):

```shell
$ docker compose config
name: test-hub
services:
  test-hub:
    build:
      context: /Users/…/test-hub
      dockerfile: Dockerfile
    container_name: test-hub
    environment:
      TEST_HUB_INSECURE_NO_AUTH: "true"
      TEST_HUB_LOG_FORMAT: json
      TEST_HUB_LOG_LEVEL: info
    healthcheck:
      test: [CMD, python, -c, "import sys, urllib.request; sys.exit(0 if …/healthz… else 1)"]
      interval: 30s
      timeout: 5s
      start_period: 15s
      retries: 3
    image: test-hub:local
    ports:
      - mode: ingress
        host_ip: 127.0.0.1
        target: 8000
        published: "8000"
        protocol: tcp
    read_only: true
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    …
```

Two lines carry the whole security model of that file.

**`"127.0.0.1:8000:8000"`.** Written as `"8000:8000"`, Docker publishes on
`0.0.0.0` and — because it inserts its own DNAT rules *ahead* of the host
firewall — hands an unauthenticated Test Hub to the network, straight past
`ufw` or `firewalld`. This is the single most common way a development Compose
file becomes an incident. Only widen it once something in front terminates TLS
and authenticates.

**`TEST_HUB_INSECURE_NO_AUTH: "true"`.** Safe *only* because of the line above.
It logs a warning on every start and banners the dashboard; that noise is
deliberate.

Also set: `read_only: true` with a 64 MB tmpfs on `/tmp` (Test Hub writes only to
`/data` and never shells out), `no-new-privileges`, log rotation at 10 MB × 3
files, and a named volume rather than a bind mount so the uid-10001 ownership
baked into the image carries over without a host-side `chown`.

### Turning it into a real deployment

Three changes:

1. Delete `TEST_HUB_INSECURE_NO_AUTH`.
2. Generate a token and configure its **hash**:
   ```shell
   $ docker compose exec test-hub test-hub token create --label ci
   ```
   The token goes to the CI job; the `pbkdf2_sha256$…` line goes in the Compose
   file. Comma-separate several so tokens can be rotated without downtime.
3. Widen the published port only once TLS terminates in front of it, and set
   `TEST_HUB_ROOT_PATH` if the proxy mounts Test Hub on a sub-path.

**The `$$` trap, and it is a real one.** Compose treats a single `$` as the start
of a variable reference, so a four-field PBKDF2 hash pasted verbatim is silently
truncated. Verified:

```yaml
environment:
  SINGLE: "pbkdf2_sha256$120000$SALT$HASH"
  DOUBLED: "pbkdf2_sha256$$120000$$SALT$$HASH"
```

```shell
$ docker compose config | grep -A2 environment
    environment:
      DOUBLED: pbkdf2_sha256$$120000$$SALT$$HASH
      SINGLE: pbkdf2_sha256$$120000
```

`SINGLE` lost everything after the second field, because `$SALT` and `$HASH`
resolved to nothing. (`docker compose config` re-escapes on output, so `$$` in
that listing means one literal `$`.) The server then rejects every token with a
message about the *token*, and you go looking in the wrong place. Write `$$` for
each literal `$`, and check with `docker compose config | grep
TEST_HUB_API_TOKEN_HASHES` before you deploy. The same applies to the `.env` file
beside the Compose file and to `env_file:` — Compose interpolates those too.

## Without Docker

```shell
$ python -m venv /opt/test-hub
$ /opt/test-hub/bin/pip install test-hub
$ /opt/test-hub/bin/test-hub token create --label ci
```

A systemd unit, minimal and sufficient:

```ini
[Unit]
Description=Test Hub
After=network.target

[Service]
Type=exec
User=test-hub
Environment=TEST_HUB_DATA_DIR=/var/lib/test-hub
Environment=TEST_HUB_HOST=127.0.0.1
Environment=TEST_HUB_PORT=8000
Environment=TEST_HUB_LOG_FORMAT=json
EnvironmentFile=/etc/test-hub/tokens.env
ExecStart=/opt/test-hub/bin/test-hub serve
Restart=on-failure
RestartSec=5s

# Test Hub writes only to its data directory.
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=/var/lib/test-hub

[Install]
WantedBy=multi-user.target
```

`/etc/test-hub/tokens.env` holds one line — `TEST_HUB_API_TOKEN_HASHES=pbkdf2_sha256$…`
— and should be mode `0600`, owned by root. No `$$` doubling here: systemd's
`EnvironmentFile` does not interpolate. That trap is Compose's alone.

Add `RestartSec` and *not* `Restart=always`: a `configuration_error` exits 78,
and a supervisor that restarts a misconfiguration forever will hide it.

**Do not run more than one worker.** The live-update bus is in-process; two
workers means two buses and half the dashboards missing half the events. There is
no `--workers` flag on `test-hub serve` for this reason.
[scaling.md](scaling.md#the-bus) explains what to replace when that bites.

## Choosing a database

<a id="choosing-a-database"></a>

| | SQLite (default) | PostgreSQL |
|---|---|---|
| Set up | Nothing. A file in the data directory. | A server, and `TEST_HUB_DATABASE_URL`. |
| Concurrent publishers | One write at a time, database-wide. The rest queue (`TEST_HUB_SQLITE_BUSY_TIMEOUT_MS`). | Writes to different runs run in parallel; only writes to the **same** run take turns. |
| Readers vs writers | Never block each other (WAL). | Never block each other (MVCC). |
| Backups | Copy three files the right way — [Backups](#backups). | `pg_dump`, plus the artifacts directory. |
| Imports, one Test Hub process | Faster: no round trip per statement. | Slower — the process's CPU is the limit either way, and each statement crosses a socket. [Measured](scaling.md#sqlite-or-postgresql). |
| Choose it when | One Test Hub instance, which is what Test Hub runs as today. | You want the database on a server: managed backups, replication, other tools querying it, storage that is not a local disk. |

**SQLite** needs no configuration at all: leave `TEST_HUB_DATABASE_URL` unset.

**PostgreSQL** is selected by pointing `TEST_HUB_DATABASE_URL` at a server.
Nothing else changes — the API, the CLI, the dashboard and every adapter are the
same — and the schema is created on first start, exactly as it is for SQLite.

### With Docker

The image already contains the PostgreSQL driver. `docker-compose.postgres.yml`
adds a PostgreSQL container and points Test Hub at it:

```shell
$ echo "TEST_HUB_POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env
$ docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
$ docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec test-hub test-hub db check
  database  postgresql+psycopg://test_hub@postgres:5432/test_hub
  server    PostgreSQL 17.11
  schema    0003_postgresql_bigint (up to date)
  ready
```

Add `COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml` to the same
`.env` to make it the default, so plain `docker compose up -d`, `logs` and `exec`
include it. The password is required and has no default; it reaches Test Hub as
`PGPASSWORD`, never inside the URL, so it needs no URL-encoding and never appears
in the banner or the logs. The PostgreSQL container publishes no port — only Test
Hub can reach it.

To use a PostgreSQL server you already run, skip the override file and set the
URL on the `test-hub` service in `docker-compose.yml` instead:

```yaml
environment:
  TEST_HUB_DATABASE_URL: "postgresql://test_hub@db.internal:5432/test_hub"
  PGPASSWORD: "${TEST_HUB_DB_PASSWORD}"
```

### Without Docker

```shell
$ pip install "test-hub[postgres]"          # adds the psycopg driver
$ export TEST_HUB_DATABASE_URL='postgresql://test_hub:PASSWORD@db.internal:5432/test_hub'
$ test-hub db check                         # which database, and is it ready?
$ test-hub serve
```

Test Hub needs one database and a role that owns it; it creates and migrates its
own tables:

```sql
CREATE ROLE test_hub LOGIN PASSWORD '…';
CREATE DATABASE test_hub OWNER test_hub;
```

A few things worth knowing:

- **`postgres://` and `postgresql://` both work.** They are given the psycopg 3
  driver automatically; a URL that names another driver (`postgresql+psycopg2://`)
  keeps it.
- **The password** can sit in the URL (percent-encode `@`, `:` and `/`), or be
  left out of it and supplied the standard libpq ways — `PGPASSWORD` or a
  `~/.pgpass` entry. `sslmode`, `connect_timeout` and other libpq options go in
  the URL's query string: `…/test_hub?sslmode=require`.
- **`--database-url`** on `serve`, `ingest`, `demo`, `prune` and `db` chooses the
  database for one command. It refuses a URL with a password in it, for the same
  reason tokens are never taken as arguments: a command line is visible to every
  user in `ps` and stays in shell history.
- **Connections** are pooled: `TEST_HUB_DATABASE_POOL_SIZE` (10) kept open, plus
  up to `TEST_HUB_DATABASE_MAX_OVERFLOW` (20) under a burst. Each Test Hub
  process can therefore hold 30 connections; check that against the server's
  `max_connections`.
- **Migrations** take an advisory lock, so two instances starting at once against
  an empty database do not both try to create it; the second waits and finds the
  schema at head.
- **Artifacts still live on the filesystem**, under `TEST_HUB_DATA_DIR`. Moving
  the database to PostgreSQL does not make the data volume optional, or remove it
  from the backup plan.
- **Still one Test Hub process.** PostgreSQL removes the single-writer limit, not
  the in-process live-update bus: two API processes behind a load balancer would
  each hold their own bus, and dashboards would miss each other's events. See
  [scaling.md](scaling.md#the-bus).
- **Switching an existing install starts empty.** Runs already in SQLite are not
  copied to PostgreSQL. Keep the old instance running for its history until it
  ages out, and point CI at the new one.

If anything is wrong, Test Hub says so in one line and exits `78` (`EX_CONFIG`)
before it binds a port: an unsupported backend, a missing driver
(`pip install 'test-hub[postgres]'`), or a server it cannot reach — with the
reason the server gave, and the URL with its password masked.

## Persistent data

Everything Test Hub keeps lives under `TEST_HUB_DATA_DIR` (`/data` in the image):

```
/var/lib/test-hub/
├── test-hub.db          the database
├── test-hub.db-wal      the write-ahead log — part of the database
├── test-hub.db-shm      the shared-memory index — part of the database
└── artifacts/
    ├── .incoming/       partial uploads, safe to delete when nothing is running
    └── aa/bb/<sha256>   content-addressed blobs
```

`ensure_directories()` chmods the data directory to `0700` and blobs are written
`0600`, where the platform supports it. One volume covers both the database and
the artifacts; keeping them apart is possible with `TEST_HUB_ARTIFACTS_DIR` and
buys you nothing unless the blobs need different storage.

**The database is three files, not one.** That is the single most important
sentence on this page for anyone writing a backup script.

On PostgreSQL the three `test-hub.db*` files are not there — the database lives
on the server — but `artifacts/` is, and it still needs backing up.

## Backups

<a id="backups"></a>

WAL mode means committed data lives in `test-hub.db-wal` until a checkpoint moves
it into `test-hub.db`. Copying the `.db` alone, while the service is running,
gives you a database that is internally consistent and **missing recent writes**.
Demonstrated on a live instance:

```shell
$ cp /var/lib/test-hub/test-hub.db /tmp/naive.db
$ sqlite3 /tmp/naive.db 'select count(*) from runs;'
31
$ curl -s "$HUB/api/v1/runs?limit=1" | jq .total
38
```

Seven runs were sitting in the WAL. Nothing errored; the copy just quietly
predates them.

Two correct options.

**Online, with `.backup`.** SQLite's backup API takes a consistent snapshot of a
live database, WAL included, without stopping anything:

```shell
$ sqlite3 /var/lib/test-hub/test-hub.db ".backup '/backup/test-hub-$(date +%F).db'"
$ sqlite3 /backup/test-hub-$(date +%F).db 'select count(*) from runs; pragma integrity_check;'
38
ok
```

That is the same instant as the previous listing, and it captured all 38.
`.backup` produces a single file with no `-wal` beside it, which is exactly what
you want to restore from.

**Offline, with a stop.** Stop the service, then copy all three files together —
or copy just the `.db` after a clean shutdown, which checkpoints and removes the
WAL.

Whichever you choose, the artifacts are a separate, easy problem: they are
content-addressed and immutable, so `rsync -a` or a tarball is safe at any time,
and an artifact that arrives during the copy simply lands in the next one.

```shell
# Everything, from a Compose deployment, with the service running.
$ docker compose exec -T test-hub \
    sqlite3 /data/test-hub.db ".backup '/data/backup.db'"
$ docker run --rm -v test-hub-data:/data -v "$PWD:/backup" busybox \
    tar czf /backup/test-hub-$(date +%F).tgz /data/backup.db /data/artifacts
$ docker compose exec -T test-hub rm /data/backup.db
```

**Restoring** is a file copy with the service stopped: put the backed-up `.db` in
place as `test-hub.db`, delete any stale `-wal` and `-shm` beside it, restore the
artifact tree, and start. If the artifact tree is older than the database, the
rows will reference blobs that are not there and downloads will 404 — the run
data itself is unaffected. Restore artifacts first if you are doing them
separately.

### On PostgreSQL

`pg_dump` takes a consistent snapshot of a live database, so there is no
three-file trap — but the artifacts are still on the filesystem and still need
the same `rsync` or tarball as above.

```shell
# The PostgreSQL compose deployment, with everything running.
$ docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
    pg_dump -U test_hub -Fc test_hub > test-hub-$(date +%F).dump
$ docker run --rm -v test-hub-data:/data -v "$PWD:/backup" busybox \
    tar czf /backup/test-hub-artifacts-$(date +%F).tgz /data/artifacts
```

Restore with `pg_restore --clean --if-exists -d test_hub` into a stopped Test
Hub, artifacts first, as above.

## Retention

<a id="retention"></a>

Nothing ages out until you schedule it. Retention is not a daemon: it runs when
somebody runs it, either from the terminal or from the dashboard's Settings page.
The reasoning is in [ADR-0002](adr/0002-data-retention.md).

### From the dashboard

The gear in the header → **Retention and data…** opens `#/settings`, which shows
the configured policy, lets you pick a cutoff in hours, days, weeks, months or
years, and — this is the part that matters — **previews exactly what would be
deleted before anything is**. Changing any field throws the preview away, so the
delete button always describes the numbers on screen.

**Who may use it.** Deleting needs an administrator token, which is a *different*
list from the tokens CI publishes with:

```shell
$ test-hub token create --admin
$ export TEST_HUB_ADMIN_TOKEN_HASHES="pbkdf2_sha256$..."
```

A publisher token cannot prune, and gets a 403 if it tries. The dashboard asks
`GET /api/v1/retention` first and renders an explanation rather than a form when
the answer is no.

Two exceptions to needing that token, both deliberate:

- **A loopback instance with no auth** — the laptop case — is admin already.
  Anyone who can reach that port can run `test-hub prune` in a terminal anyway.
- **A public bind with `--insecure-no-auth` is not.** Open writes are a choice you
  made knowingly; open deletes are a second choice, and it has to be made
  separately.

`VACUUM` is not exposed over HTTP — it rewrites the whole database behind an
exclusive lock. Use the CLI for that.

### From the terminal

Always look first. `--dry-run` touches nothing:

```shell
$ test-hub prune --older-than 90d --dry-run

  47 run(s) would be removed
  spanning  2026-01-04 to 2026-06-10
    payments-api                 22
    storefront-web               15
    ledger-worker                10
  rows      18402 total
            412 suites, 6180 tests, 6903 attempts, 4880 events, 27 artifact rows
  artifacts 24 blob(s), 61.4 MiB, 3 kept (still referenced or inside the grace window)
  25 run(s) held back by the per-project floor (--keep-min)
```

Then run it for real. `--yes` is required when there is no terminal, and the
command refuses rather than prompting into a pipe — a cron entry that forgot it
fails on the first scheduled run instead of hanging:

```shell
$ test-hub prune --older-than 90d --yes
```

**The cutoff.** `--older-than 12h`, `90d`, `2w`, `6m`, `1y`, or a bare number of
days; or `--before 2026-01-01` for an absolute date. `m` is 30 days and `y` is
365 — fixed lengths, not calendar arithmetic, so `6m` is exactly 180 days. Hours
are there because "clear the last hour" is a real thing to want after a bad
import.

A run is dated by when it *ran* (`started_at`, falling back to `created_at`),
which is the same date the dashboard filters on — so what you see under a date
filter is what a prune to that date removes. `--by-import-date` switches to when
Test Hub received it, which is what you want if a CI job uploads back-dated
archives that should still be kept for 90 days after arrival.

**What is protected.** The newest `--keep-min` runs of every project survive
whatever their age (default 5, `0` disables). Without it, a 90-day policy deletes
a project that ran nightly for a year and then went quiet out of the dashboard
entirely. Runs that are still `queued` or `running` are skipped and counted; a
count that does not go down is a publisher that died, and `--include-unfinished`
takes them.

**Artifacts.** Blobs are content-addressed, so one file backs every row that ever
uploaded those bytes. A blob is unlinked only once no row anywhere points at it,
after the row deletions have committed — so pruning one project does not remove a
screenshot another project shares. Blobs written within
`TEST_HUB_ARTIFACT_GC_GRACE_HOURS` are left alone regardless, which closes the
race against an import that is mid-flight.

**Disk space.** Deleting rows does not shrink `test-hub.db`; SQLite keeps the
freed pages and reuses them. The blobs are freed immediately and are usually the
bulk of the bytes. To return the database pages to the filesystem, pass
`--vacuum` — it rewrites the whole file, wants up to twice its current size in
free space, and holds an exclusive lock throughout, so run it when the service is
quiet and never on a disk that is already nearly full.

**Nothing is archived.** Pruned data is gone. If you want a cold copy first, take
a database backup — [Backups](#backups) covers how, and a copy taken before the
prune is the only thing that can bring a pruned run back.

**Scheduling it.** A cron entry on the host, at an hour you chose — the prune
takes SQLite's write lock in batches, and publishers queue behind each one:

```shell
17 4 * * *  test-hub prune --older-than 90d --yes >> /var/log/test-hub-prune.log 2>&1
```

Or, against a Compose deployment:

```shell
$ docker compose exec test-hub test-hub prune --older-than 90d --yes
```

## Reverse proxy

<a id="reverse-proxy"></a>

### The SSE buffering trap

nginx buffers proxied responses by default. Applied to `/api/v1/events/stream`,
that turns a live stream into a stream that arrives all at once, at the end — the
dashboard connects, shows nothing for minutes, then everything at once, and
nothing in any log says why.

Test Hub already sends `X-Accel-Buffering: no` on that response, verified on the
wire:

```
$ curl -sD - -o /dev/null --max-time 2 http://127.0.0.1:8123/api/v1/events/stream
HTTP/1.1 200 OK
content-type: text/event-stream; charset=utf-8
cache-control: no-cache, no-transform
connection: keep-alive
x-accel-buffering: no
transfer-encoding: chunked
```

nginx honours that header, so a plain `proxy_pass` mostly works. **Set
`proxy_buffering off` anyway**, for two reasons: the header is nginx-specific and
does nothing for HAProxy, Traefik, Caddy or an ingress controller, and it is
easily lost — an intermediate proxy, a `proxy_hide_header`, or a CDN in front
will drop it and you will be debugging an invisible failure. Belt and braces on
the one path where the failure mode is silence.

```nginx
server {
    listen 443 ssl http2;
    server_name test-hub.internal;

    ssl_certificate     /etc/ssl/certs/test-hub.crt;
    ssl_certificate_key /etc/ssl/private/test-hub.key;

    # Reports are up to TEST_HUB_MAX_REPORT_BYTES; nginx defaults to 1 MB and
    # answers 413 itself, with an HTML body no publisher can parse.
    client_max_body_size 128m;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }

    # Server-Sent Events. Everything in this block exists because of a specific
    # failure mode.
    location /api/v1/events/stream {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Connection        "";   # keep-alive to the upstream

        proxy_buffering    off;    # or the stream arrives at the end, all at once
        proxy_cache        off;
        chunked_transfer_encoding on;

        # Heartbeats are every 15s by default (TEST_HUB_SSE_HEARTBEAT_SECONDS).
        # A read timeout shorter than that disconnects an idle-but-healthy
        # stream; one hour means a genuinely dead upstream is still noticed.
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

`proxy_set_header Connection ""` is needed because nginx sends `Connection:
close` upstream by default under HTTP/1.1, which defeats keep-alive on a
long-lived stream.

Do not add security headers at the proxy. Test Hub sends its own CSP,
`X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`,
`Cross-Origin-Opener-Policy` and `Permissions-Policy` on every response,
including the static dashboard; a second, weaker set from the proxy is how a
restrictive CSP becomes a permissive one. Check with `curl -sI` after any proxy
change.

### Caddy

Caddy does not buffer streaming responses by default, which makes this short:

```caddy
test-hub.internal {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1        # never buffer; required for SSE
    }
    request_body {
        max_size 128MB
    }
}
```

TLS is automatic. `flush_interval -1` is the SSE line.

### A sub-path

Serving Test Hub at `https://ci.internal/test-hub/` needs
`TEST_HUB_ROOT_PATH=/test-hub` so the app generates correct URLs, plus the
matching proxy rewrite. Prefer a subdomain if you have the choice — a sub-path is
one more thing that can be subtly wrong, and the failure shows up as a dashboard
that loads and then cannot fetch anything.

### TLS

Terminate it at the proxy. Test Hub has no TLS listener and no plan for one.
Everything about the deployment assumes the hop between the proxy and Test Hub is
loopback or a trusted private network; if it is not, encrypt that hop too.

## Upgrades and migrations

```shell
$ pip install -U test-hub          # or: docker compose pull && docker compose up -d
```

**Migrations run themselves.** Every entry point — `serve`, `ingest`, `export`,
`demo` — calls `Database.upgrade()`, which runs Alembic to head against the
packaged migration scripts before doing anything else. A user who upgrades and
runs `serve` never needs to learn that Alembic exists. The migration environment
resolves the script location from inside the installed package rather than from
`alembic.ini`, so this works from a wheel where the repo's ini file does not
exist.

Check what a database is stamped with — SQLite or PostgreSQL alike:

```shell
$ test-hub db check
  database  sqlite+pysqlite:////var/lib/test-hub/test-hub.db
  server    SQLite 3.46.1
  schema    0003_postgresql_bigint (up to date)
  ready
```

It exits `1` when migrations are pending, so a deploy script can tell. To run
them as a deliberate step of their own rather than on the next start, use
`test-hub db upgrade`; it is safe to run repeatedly, and on PostgreSQL from two
places at once.

The upgrade procedure that respects the fact that migrations are one-way:

1. **Back up first** (see [above](#backups)). There is no downgrade path tested
   for the first release, and the safe rollback is "restore the backup and
   reinstall the old version".
2. Stop the service. A migration on a live database is a write, and it will
   queue behind — or block — every publisher.
3. Upgrade the package or pull the image.
4. Start. Watch the first few log lines for the Alembic revision.

**Downgrading Test Hub after an upgrade that migrated the schema will not work.**
The old code does not know the new schema. Restore the backup.

**A database created by `create_all()`** — which is what the test suite and the
demo seeder use for speed — is stamped at head at creation, so it can still be
upgraded later instead of failing on the first migration with "table already
exists".

## Configuration reference

Everything is `TEST_HUB_` + the setting name, from the environment, a `.env` file
beside the working directory, or a CLI flag. Flags beat the environment; the
environment beats `.env`.

| Variable | Default | Notes |
|---|---|---|
| `TEST_HUB_HOST` | `127.0.0.1` | Anything else requires tokens or `--insecure-no-auth`. |
| `TEST_HUB_PORT` | `8000` | |
| `TEST_HUB_ROOT_PATH` | `""` | Set when behind a proxy on a sub-path. |
| `TEST_HUB_DATA_DIR` | platform user-data dir | `/data` in the image. |
| `TEST_HUB_DATABASE_URL` | SQLite inside `data_dir` | `postgresql://user:password@host:5432/dbname` selects PostgreSQL (needs `pip install "test-hub[postgres]"`; the image has it). See [Choosing a database](#choosing-a-database). |
| `TEST_HUB_DATABASE_POOL_SIZE` | `10` | PostgreSQL: connections kept open. Ignored for SQLite. |
| `TEST_HUB_DATABASE_MAX_OVERFLOW` | `20` | PostgreSQL: extra connections under a burst, on top of the pool. |
| `TEST_HUB_ARTIFACTS_DIR` | `data_dir/artifacts` | |
| `TEST_HUB_SQLITE_BUSY_TIMEOUT_MS` | `10000` | SQLite: how long a writer waits behind another writer. |
| `TEST_HUB_API_TOKEN_HASHES` | — | Comma-separated, or a JSON array. The preferred form. |
| `TEST_HUB_API_TOKENS` | — | Plaintext; hashed at startup. Use the hashes instead. |
| `TEST_HUB_ADMIN_TOKEN_HASHES` | — | Tokens allowed to **delete** data. A separate list on purpose — see [Retention](#retention). |
| `TEST_HUB_ADMIN_TOKENS` | — | Plaintext admin tokens. Use the hashes instead. |
| `TEST_HUB_AUTH_ENABLED` | auto | Turns itself on when any token is configured. |
| `TEST_HUB_REQUIRE_AUTH_FOR_READS` | `false` | Reads are open by default even with auth on. |
| `TEST_HUB_INSECURE_NO_AUTH` | `false` | Explicit opt-in to an unauthenticated non-loopback bind. |
| `TEST_HUB_CORS_ALLOW_ORIGINS` | — | Empty means no CORS headers at all. |
| `TEST_HUB_TRUSTED_HOSTS` | — | Adds Starlette's `TrustedHostMiddleware`. |
| `TEST_HUB_CSP` | restrictive | Only loosen it if you know why. |
| `TEST_HUB_REDACTION_PATTERNS` | see [security.md](security.md#adding-your-own) | Replaces the defaults; a pattern containing a comma must use the JSON-array form. |
| `TEST_HUB_MAX_REPORT_BYTES` | 64 MiB | Also raise `client_max_body_size` in the proxy. |
| `TEST_HUB_MAX_ARTIFACT_BYTES` | 128 MiB | |
| `TEST_HUB_MAX_TEXT_FIELD_BYTES` | 256 KiB | Per stack trace or log body. |
| `TEST_HUB_MAX_EVENTS_PER_BATCH` | `5000` | |
| `TEST_HUB_STORE_RAW_REPORTS` | `true` | Keeps the original upload as an artifact. Turn off to save a write and a file per import. |
| `TEST_HUB_SSE_HEARTBEAT_SECONDS` | `15` | Keep the proxy's read timeout comfortably above this. |
| `TEST_HUB_SSE_REPLAY_BUFFER` | `1000` | Events kept per workspace for `Last-Event-ID`. |
| `TEST_HUB_SSE_MAX_CLIENTS` | `200` | |
| `TEST_HUB_HISTORY_WINDOW_DAYS` | `30` | What the overview *shows*. Deletes nothing. |
| `TEST_HUB_FLAKY_WINDOW_RUNS` | `20` | |
| `TEST_HUB_RETENTION_DAYS` | — | Default cutoff for `test-hub prune`. **Nothing sweeps on its own**; schedule the command. |
| `TEST_HUB_RETENTION_MIN_RUNS` | `5` | Runs always kept per project, however old. `0` disables the floor. |
| `TEST_HUB_ARTIFACT_GC_GRACE_HOURS` | `24` | A blob written more recently than this is never unlinked. |
| `TEST_HUB_LOG_LEVEL` | `info` | |
| `TEST_HUB_LOG_FORMAT` | `text` | `json` for a log shipper. |

**List-valued settings** (`API_TOKENS`, `API_TOKEN_HASHES`, `CORS_ALLOW_ORIGINS`,
`TRUSTED_HOSTS`, `REDACTION_PATTERNS`) accept both `a,b,c` and `["a","b","c"]`
from the environment. Use the JSON form when a value itself contains a comma —
which for a regex `{8,64}` quantifier it will.

## Operating it

**Logs.** `TEST_HUB_LOG_FORMAT=json` gives one JSON object per line with `ts`,
`level`, `logger` and `message`. Uvicorn's access log is off by default because it
duplicates Test Hub's own logging and is noisy in a terminal that is also showing
test output; if you need per-request logs, take them from the reverse proxy,
which is also the only place you will get a client IP.

Everything logged passes through the same redaction as stored test output.

**Health.** `GET /healthz` touches the database and returns
`{"status": "ok"|"degraded", "database": "ok"|"error", "live_subscribers": N, …}`
with HTTP 200 either way — a healthcheck that raises tells you nothing. Alert on
the body, not the status code. `live_subscribers` is how many dashboards are
watching, which is a cheap way to notice that the SSE path broke.

**What to watch.** Data-directory free space (nothing is deleted unless you have
scheduled `test-hub prune` — see [Retention](#retention)), import
latency at p95 (the single-writer queue shows up here first —
[scaling.md](scaling.md)), and `status != "ok"`.

## Packaging name

<a id="packaging-name"></a>

`test-hub` is a good distribution name and a risky one. PyPI rejects an upload
whose name is "too similar to an existing project": PEP 503 normalisation folds
runs of `-`, `_` and `.` together and lowercases, so `test_hub`, `testhub` and
`Test.Hub` are all the same name to it. That rejection arrives at the worst
possible moment — mid-release, with a tag already pushed.

[`tools/set_dist_name.py`](../tools/set_dist_name.py) is the pre-agreed escape
hatch. It renames the *distribution* and nothing else:

| | | |
|---|---|---|
| Distribution name | `pip install test-hub` | **this is what changes** |
| Import package | `import test_hub` | never changes |
| Console script | `test-hub …` | never changes |

So a rename is invisible to users' code and to their shell. It changes the row on
PyPI and the string in `pip install`.

```shell
$ python tools/set_dist_name.py --show
test-hub

$ python tools/set_dist_name.py testhub-io --dry-run
--- /path/to/test-hub/pyproject.toml  (line 10)
  before: test-hub   (PyPI compares this as 'test-hub')
  after:  testhub-io   (PyPI compares this as 'testhub-io')

--- a/pyproject.toml
+++ b/pyproject.toml
@@ -8,5 +8,5 @@
 # — it rewrites this field and nothing else. The import package (`test_hub`) and
 # the console script (`test-hub`) are unaffected by that change.
-name = "test-hub"
+name = "testhub-io"
 dynamic = ["version"]

dry run — nothing written

$ python tools/set_dist_name.py testhub-io      # actually write it
```

It rewrites exactly one line: the `name = "..."` assignment inside the
`[project]` table. Not `[tool.poetry] name`, not `name` inside
`[[project.authors]]`, not a `[project]`-looking table inside a comment. Before
writing it re-reads its own output and asserts that precisely one line differs;
if anything else moved it writes nothing and exits 3. A formatting-preserving
line edit rather than a TOML round-trip, because `tomllib` cannot write and every
library that can will also reflow comments, reorder keys and normalise quotes
across a file full of deliberate prose.

Bad input is rejected before anything is touched:

```shell
$ python tools/set_dist_name.py 'not a name!'
error: 'not a name!' is not a valid PEP 508 distribution name. Names must start and end
with a letter or digit and may contain only letters, digits, '-', '_' and '.' in between.
$ echo $?
2
```

Exit codes: `0` changed or already correct, `2` invalid name or malformed
`pyproject.toml`, `3` the safety check tripped — which means the file is not what
the script expected and a human should look at it rather than retrying with a
different argument.

After a rename: run `python -m build` to confirm the wheel is named as expected,
update the PyPI Trusted Publisher to the new project name **before** tagging a
release, and note the change in `CHANGELOG.md` so users learn why
`pip install test-hub` stopped finding new versions.
