# ADR-0003 — Drop the offline HTML export

- **Status**: Accepted
- **Date**: 2026-09-09
- **Deciders**: Test Hub maintainers
- **Supersedes**: [ADR-0001](0001-architecture.md) D1 (the second provider)
- **Amends**: [ADR-0001](0001-architecture.md) D5, and the "known limits" list

## Context

[ADR-0001](0001-architecture.md) named two consumption modes as requirements on
day one: a self-contained HTML file that works with no network, and a live
dashboard backed by the service. D1 is the design that made both possible from
one codebase — a `DataProvider` seam with a snapshot implementation and a live
implementation, selected at boot.

The export shipped and worked. What it did not do is make sense next to the
product the dashboard has become.

A Test Hub report is a *view onto a store that keeps changing*. Every page is
built on that: the connection pill, the run in flight, the pass-rate trend that
gains a point every night, retention that removes runs behind you. The exported
file froze all of it and then had to explain itself — a pill reading "Static
export", a settings panel that said there was nothing to manage, a run page whose
"Refresh" could not refresh. Each of those is a small apology for the file not
being the thing the reader was looking at ten seconds earlier.

The evidence pages made the mismatch concrete rather than stylistic. Screenshots,
API Logs and Test Coverage are pages about the files a run attached, and an
export carries the runs but not the files: an artifact URL points at a server the
file does not have, and inlining the blobs would turn a 1 MB report into a
hundred. So three of the nine routes could only ever tell the reader that the
thing they clicked for was elsewhere.

The cost of keeping it was not the exporter. It was that every feature had to be
designed twice — once for a server that answers questions, once for a file that
cannot — and the second design was always worse.

## Decisions

### D1 — The dashboard is served, and only served

`test-hub export`, `test_hub.export`, the `Snapshot` model and
`SnapshotDataProvider` are removed. `test-hub serve` is the only way to open the
dashboard, and the only thing it can say about its data is what the server told
it.

*Why*: one mode, designed once. Nothing in the product now has to answer "and
what does this look like when there is no server?".

*Consequence*: there is no offline artifact to hand to somebody without access to
a Test Hub. Sharing a result means sharing a URL, which means the server has to
be reachable by whoever is meant to read it. That is a real loss and it is the
price of the simplification — see "Rejected alternatives".

### D2 — The provider seam stays, with one implementation

`10-provider.js` keeps the interface, the shared base and the metric arithmetic;
`12-live-provider.js` remains its only implementation.

*Why*: the seam's second job outlived its first. It is what keeps `fetch` out of
every view — a view that cannot reach the network cannot grow a request in the
middle of a render — and it is the one place the browser-side arithmetic has to
agree with `test_hub.domain.metrics`. Collapsing it into the transport would buy
one fewer file and cost both of those.

*Consequence*: a file that exists for a seam with one implementation. Kept
deliberately; this ADR is the note explaining why it is not dead weight.

## What was removed

| Removed | Was |
|---|---|
| `test-hub export` | The CLI command, including `--input` for exporting a directory of reports without a store |
| `src/test_hub/export/` | `SnapshotExporter`, `ExportOptions` |
| `domain.models.Snapshot` | The frozen payload embedded in the file |
| `assets/js/11-snapshot-provider.js` | The second provider |
| The `static` provider status | And the "Static export" connection pill |
| The exporter markers in `index.html` | `test-hub:styles`, `test-hub:scripts`, `test-hub:snapshot` |
| `provider.summarize`, `provider.facetsFromSummaries` | Browser-side arithmetic only the snapshot needed |

`SCHEMA_VERSION` does **not** move. `Snapshot` was produced by the exporter and
read by the browser; no publisher ever sent one and no endpoint ever returned
one, so nothing on the wire between a client and this server changes shape.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| Keep the export, inline the artifacts | A gallery of screenshots is tens of megabytes; the file stops being emailable, which was its whole appeal. |
| Keep the export, hide the pages it cannot serve | A rail whose contents depend on how the page was opened is worse than a rail that is always the same. |
| A "print to PDF" report instead | A different product decision, not a replacement for this one. Nothing here forecloses it. |
| Keep the exporter, stop testing it | Untested code that ships is worse than code that does not ship. |

## Consequences

1. Somebody who could previously attach `report.html` to a ticket now sends a
   link, and the reader needs access to the server. There is no migration for an
   already-exported file: it is a frozen HTML document and it keeps working
   exactly as it did.
2. The dashboard is still built to work with no internet access — no CDN, no
   webfonts — because an air-gapped install is a real deployment. That was never
   the same requirement as the offline file, and the tests that enforce it stay.
3. ADR-0001's "known limits" item 3, about snapshot export being a full
   materialization, no longer applies.
