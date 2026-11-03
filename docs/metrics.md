# Metrics

Every number Test Hub shows is defined here and implemented once, in
[`test_hub/domain/metrics.py`](../src/test_hub/domain/metrics.py) and the
computed fields of `RunCounts` in
[`test_hub/domain/models.py`](../src/test_hub/domain/models.py). Nothing else in
the Python codebase computes a pass rate, a flake rate or a percentile — a view
that needs one calls in there, so no two parts of the product can disagree about
the same number.

`tests/test_metrics.py` is the executable version of this document. Most of the
worked examples below are lifted from it verbatim; if a definition changes, that
file and this one change together.

There is exactly one second implementation, and it is deliberate:
`src/test_hub/web/assets/js/10-provider.js` transcribes `finalizeCounts` and the
trend arithmetic into JavaScript, because a filter the API cannot express is
applied in the browser and the counts have to be recomputed there. The JavaScript carries the Python function
names in its comments so the two stay greppable from each other. That is a real
duplication and worth knowing about before you change a formula.

## The counting rule

**One test contributes exactly one outcome to a run's counts, through its
effective attempt.** The effective attempt is the *last* one — not the best, not
the worst:

```python
def effective_status(attempt_statuses):
    if not attempt_statuses:
        return TestStatus.UNKNOWN
    return attempt_statuses[-1]
```

A test that failed and then passed on retry is green *and* flaky, and it counts
as one pass. A test that passed and then failed on a rerun is red. The retry
history is never discarded, so both facts remain available — that is the whole
reason attempts are rows rather than overwrites.

Counts are always folded from the stored attempt rows
(`SqlEventRepository._refold`), never taken from a publisher's own totals. A
publisher's totals drift the moment anything is retried or a shard fails to
upload, and a front page that disagrees with the run page it links to is worse
than a slow one.

## The count fields

`RunCounts` has thirteen stored fields and four computed ones. The stored ones:

| Field | Meaning |
|---|---|
| `total` | Tests folded into this block. Equals the sum of the ten status fields. |
| `passed` `failed` `error` `skipped` `xfailed` `xpassed` `cancelled` `unknown` `running` `queued` | One per `TestStatus`, counting effective attempts. |
| `flaky` | Tests that were flaky *within this run* — see below. Not a status; a test is counted here as well as under its effective status. |
| `retried` | Tests with more than one attempt. Likewise not a status. |

`flaky` and `retried` overlap with the status counts on purpose. They answer a
different question ("how much of this run's green was earned on the second try")
and adding them to `total` would double-count.

### completed and executed

```python
completed = passed + failed + error + skipped + xfailed + xpassed + cancelled + unknown
executed = completed - skipped - cancelled
```

`completed` is `total` minus the tests still in flight, so it is the honest
denominator while a run is still streaming: `running` and `queued` are excluded.
`executed` goes further and removes the tests that never ran a body. A skipped
test cannot pass and cannot fail, and putting it in the denominator turns "we
skipped the slow suite today" into a pass-rate drop that looks like a
regression.

```python
>>> counts = RunCounts(total=5, passed=3, running=1, queued=1)
>>> counts.completed
3
>>> counts.pass_rate
100.0
```

## Pass rate and failure rate

```python
pass_rate = 100 * (passed + xfailed) / executed
failure_rate = 100 * (failed + error + xpassed) / executed
```

Both are rounded to two decimal places and both are `None` when
`executed <= 0`.

**`xfailed` counts as a success.** A test marked expected-to-fail that failed
did exactly what it was declared to do. Colouring it red trains people to ignore
red.

**`xpassed` counts as a failure.** An expectation that no longer holds is a
signal: either the bug is fixed and the marker should go, or something else
changed and nobody noticed. It is the one status where "the test passed" and
"this is fine" come apart, and the code says so in
`test_hub.domain.status.is_successful` / `is_failureish`.

**`pass_rate` is `None`, not `0.0`, when nothing executed.** A run that only
skipped has no pass rate. Rendering it as a red 0% is a lie the dashboard would
then have to explain; rendering it as "—" is the truth.

```python
>>> RunCounts(total=3, skipped=3).executed
0
>>> RunCounts(total=3, skipped=3).pass_rate is None
True
```

### The two rates need not add up to 100

`unknown` sits in `executed` but in neither numerator. That is deliberate: an
unmapped status word is not evidence of a pass and not evidence of a failure,
and quietly assigning it to either would change the number the dashboard shows
based on a word Test Hub failed to recognize. Every `unknown` also raises an
`unknown_status` parser warning that is attached to the run, so the gap is
visible rather than mysterious.

This is real, not theoretical. Importing `tests/fixtures/canonical/report.json`,
which contains a test whose status is the word `bananas`:

```console
$ test-hub ingest tests/fixtures/canonical/report.json --project checkout-service --json
```

```
counts  {'total': 6, 'passed': 3, 'failed': 1, 'skipped': 1, 'unknown': 1,
         'flaky': 2, 'retried': 2, 'completed': 6, 'executed': 5,
         'pass_rate': 60.0, 'failure_rate': 20.0}
warnings:
   unknown_status - status 'bananas' is not a word Test Hub knows; recorded as unknown
```

Six tests, one skipped, so `executed` is 5. Three passes over five is 60%; one
failure over five is 20%; the missing 20% is the test nobody could classify.

### A worked mixture

`RunCounts(total=12, passed=7, failed=2, error=1, skipped=1, xfailed=1)`:

| Quantity | Arithmetic | Value |
|---|---|---|
| `completed` | 7 + 2 + 1 + 1 + 1 | 12 |
| `executed` | 12 − 1 skipped − 0 cancelled | 11 |
| `pass_rate` | 100 × (7 + 1) / 11 | 72.73 |
| `failure_rate` | 100 × (2 + 1 + 0) / 11 | 27.27 |

And the two-line version from the test suite, which is the case people get wrong
by hand — eight passed, one failed, one skipped is 88.89%, not 80%:

```python
>>> counts = RunCounts(total=10, passed=8, failed=1, skipped=1)
>>> counts.executed, counts.pass_rate
(9, 88.89)
```

### Rates over many runs

The overview and the trend chart merge count blocks with `RunCounts.merge` and
*then* compute the rate, which makes the result test-weighted rather than a mean
of per-run rates. A 2000-test nightly and a 3-test smoke run do not carry equal
weight in the portfolio number. If you want the unweighted average of run pass
rates, that is a different statistic and Test Hub does not compute it.

## Durations

### A run

```python
def run_duration_ms(started_at, finished_at, attempt_durations_ms=None):
    if started_at is not None and finished_at is not None:
        return max(0, round((finished_at - started_at).total_seconds() * 1000))
    if attempt_durations_ms is not None:
        total = sum(attempt_durations_ms)
        return int(total) if total else None
    return None
```

Wall clock wins whenever both ends are known, because with parallel workers the
sum of test durations is not a duration anyone means — eight xdist workers make
it roughly eight times the number on the wall.

```python
>>> start = utcnow(); end = start + timedelta(seconds=30)
>>> run_duration_ms(start, end, [20_000, 25_000, 18_000])
30000
```

The sum there is 63 seconds for a run that took 30. Falling back to it is a
last resort, for formats that report no timestamps at all — JUnit XML being the
obvious one.

The fallback and its floor:

```python
>>> run_duration_ms(None, None, [100, 200])
300
>>> run_duration_ms(None, None, []) is None
True
>>> run_duration_ms(None, None, None) is None
True
```

An empty list yields `None` rather than `0`: zero milliseconds is a claim about
how long something took, and "we do not know" is not that claim.

Here is the rule doing its job on a real run published over HTTP, where the two
test attempts reported 4100 ms and 3900 ms but the run itself was open for a
fraction of a second:

```console
$ curl -sX POST "$HUB/api/v1/runs/$RUN/finish" -H 'content-type: application/json' -d '{}'
```

```
final status passed duration_ms 61
counts {'total': 1, 'passed': 1, 'flaky': 1, 'retried': 1,
        'completed': 1, 'executed': 1, 'pass_rate': 100.0, 'failure_rate': 0.0}
```

61 ms of wall clock, not 8000 ms of test time.

### A test

`TestCase.duration_ms` is the **sum over every attempt**:

```python
return sum(attempt.duration_ms or 0 for attempt in self.attempts)
```

Retries are time the run actually spent. A test that took 30 seconds to time out
and then 6 seconds to pass cost the run 36 seconds, and reporting only the
successful 6 hides the most expensive tests in the suite. If you want the
effective attempt's duration on its own, it is `attempts[-1].duration_ms` and it
is in the payload.

`TestAttempt` fills in whichever of `started_at`, `finished_at` and
`duration_ms` is missing when the other two are present, and the fold in
`SqlEventRepository._on_test_finished` does the same on the way in, so an
adapter that can only supply two of the three does not lose the third.

## Flakiness

Test Hub has two flakiness definitions, at two altitudes, and they answer
different questions.

### Within a run

```python
def is_flaky_within_run(attempt_statuses):
    if len(attempt_statuses) < 2:
        return False
    saw_success = any(is_successful(s) for s in attempt_statuses)
    saw_failure = any(is_failureish(s) for s in attempt_statuses)
    return saw_success and saw_failure
```

The test both succeeded and failed inside one execution of the suite. This is
the strict definition: it needs two attempts, because a single attempt cannot
disagree with itself, and it needs genuine disagreement, not merely a retry.

```python
>>> is_flaky_within_run([S.FAILED, S.PASSED])
True
>>> is_flaky_within_run([S.PASSED, S.FAILED, S.PASSED])
True
>>> is_flaky_within_run([S.FAILED, S.FAILED])
False
>>> is_flaky_within_run([S.PASSED])
False
```

A skip does not count as either side, so a test that was skipped once and passed
once is not flaky:

```python
>>> is_flaky_within_run([S.SKIPPED, S.PASSED])
False
```

This is what `TestCase.flaky` and `RunCounts.flaky` mean, and it is what the
JUnit adapter's handling of Surefire's `flakyFailure`/`rerunFailure` elements
exists to make visible.

### Across runs — `flake_rate`

`TestDetail.flake_rate` is the percentage of runs in a trailing window in which
a test misbehaved. The window is `Settings.flaky_window_runs` (default 20) for
the overview's flakiest-tests list, and the `history` argument otherwise.

```python
def flake_rate(per_run):
    considered = [
        (status, within)
        for status, within in per_run
        if status not in (SKIPPED, CANCELLED, UNKNOWN)
    ]
    if len(considered) < 2:
        return None

    successes = sum(1 for s, _ in considered if is_successful(s))
    failures = sum(1 for s, _ in considered if is_failureish(s))
    majority_is_success = successes >= failures

    disagreements = 0
    for status, within_run in considered:
        agreed = is_successful(status) if majority_is_success else is_failureish(status)
        if within_run or not agreed:
            disagreements += 1
    return round(100.0 * disagreements / len(considered), 2)
```

Four rules, each worth stating on its own:

1. **Runs where the test did not execute are dropped**, not counted as
   agreement. `skipped`, `cancelled` and `unknown` never enter the numerator or
   the denominator.
2. **Two executed runs are the minimum.** One data point cannot describe
   consistency, and returning `0.0` for it would put every brand-new test at the
   bottom of the flakiest list where it belongs — but for the wrong reason.
   `None` is the honest answer and the dashboard renders it as "—".
3. **The majority is the baseline.** With ties broken toward success
   (`successes >= failures`), a run disagrees when its effective outcome is not
   the majority outcome.
4. **A run in which the test was flaky within the run always counts as a
   disagreement**, even when its final outcome agreed with the majority. A test
   that reached green only because it was retried is not the same as a test that
   was green.

`per_run` is ordered oldest first. The repository reverses its newest-first
history before calling in, and says so in a comment, because every list it
returns is newest-first and one of them has to flip.

Worked examples, all run against the shipped code:

| History (oldest first) | Considered | Majority | Disagreements | `flake_rate` |
|---|---|---|---|---|
| pass, pass, pass, fail | 4 | success | 1 | 25.0 |
| pass, pass, fail, fail | 4 | success (tie) | 2 | 50.0 |
| pass, fail, fail, fail | 4 | failure | 1 | 25.0 |
| pass, pass *(retried)* | 2 | success | 1 | 50.0 |
| skip, pass, fail | 2 | success (tie) | 1 | 50.0 |
| pass ×10 | 10 | success | 0 | 0.0 |
| pass | 1 | — | — | `None` |
| skip, pass | 1 | — | — | `None` |

The fourth row is the case that motivates rule 4: both runs ended green, so a
majority-only rule would report 0%, and the test would stay invisible despite
needing a retry half the time.

### `is_flaky_across_runs`

`metrics.py` also exports a boolean `is_flaky_across_runs(statuses)` — true when
a sequence of effective outcomes contains both a success and a failure. Nothing
in the server uses it; `flake_rate` is what the repository calls, because a rate
is more useful than a boolean when you are ranking twenty thousand tests. It is
exported and tested for consumers that want the cheap answer over a window they
chose themselves.

`aggregate_counts(statuses)` is in the same position: it is exported and returns
a plain dict of count fields, but the fold uses `RunCounts.add` directly.

## `TestDetail.failure_rate`

Not the same denominator as `RunCounts.failure_rate`, and not in `metrics.py` —
it is computed in `SqlRunRepository.get_test_detail`:

```python
executed = [s for s, _ in oldest_first if s not in (SKIPPED, CANCELLED, UNKNOWN)]
failure_pct = round(100.0 * sum(1 for s in executed if is_failureish(s)) / len(executed), 2)
```

It is "in what fraction of the runs where this test actually ran did it fail",
over the same window `flake_rate` uses. `RunCounts.failure_rate` is "what
fraction of the tests in this run failed". Both are called *failure rate* in the
UI because in their own contexts both readings are the obvious one, but they are
different quantities and a comparison between them means nothing.

`TestDetail.average_duration_ms` is the arithmetic mean of the per-run
`TestCase.duration_ms` values that were recorded — which, per the rule above,
are already sums across attempts.

## Percentiles

The trend chart's `duration_ms_p50` and `duration_ms_p95` come from:

```python
def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight
```

Linear interpolation between the two nearest ranks, with `fraction` clamped into
`[0, 1]`. It is written out rather than taken from `statistics.quantiles` so the
trend numbers are identical on every supported Python and reproducible by hand
from the raw durations the API returns.

```python
>>> percentile([10], 0.95)
10
>>> percentile([], 0.5) is None
True
>>> percentile([1, 2, 3, 4, 5], 0.5)
3
>>> percentile([1, 2, 3, 4], 0.0)
1
>>> percentile([1, 2, 3, 4], 1.0)
4
```

The interpolation is visible on an even-length list — `percentile([1,2,3,4],
0.5)` is `2.5`, not `2` or `3` — and it can produce the usual binary-floating
artefacts: `percentile([100, 200, 300, 400, 500], 0.95)` is
`479.99999999999994`. `TrendPoint` stores the rounded integer, so what reaches
the wire is `480`.

A single run in a bucket makes p50 and p95 the same number. That is correct and
it is also why the trend chart is not worth reading at a one-day bucket on a
quiet project.

## Where a run's status comes from

The run's own `status` is folded too, in `SqlEventRepository._fold_status`, and
it does not use `worst_status`:

| Condition, in order | Result |
|---|---|
| the run was explicitly cancelled | `cancelled` |
| `finished_at` is set and `error > 0` | `error` |
| `finished_at` is set and (`failed > 0` or `xpassed > 0`) | `failed` |
| `finished_at` is set and `total == 0` | `unknown` |
| `finished_at` is set | `passed` |
| any attempt exists, or `started_at` is set | `running` |
| otherwise | `queued` |

`cancelled` is the one outcome a publisher can dictate, because a cancelled run
is indistinguishable from a run that stopped reporting; everything else is
derived. Note that a finished run with no tests at all is `unknown`, not
`passed` — an empty suite has no outcome.

`worst_status` in `domain/status.py` folds a set of attempt statuses into the
most severe one and is used by the JUnit adapter to give a single verdict to a
`<testcase>` carrying several outcome elements. It orders
`unknown < skipped < xfailed < passed < xpassed < queued < running < cancelled <
failed < error`, and an empty iterable is `unknown`.
