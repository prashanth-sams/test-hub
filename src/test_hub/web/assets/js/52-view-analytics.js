/* Test Hub — analytics: why the suite behaves the way it does.
 *
 * The overview answers "how is it going". This page answers the four questions
 * that come immediately afterwards and that no KPI row can answer:
 *
 *   what actually broke      — failures grouped by exception type, not by test
 *   where it broke           — which suites carry the failures
 *   whose it is              — the same numbers per owner
 *   what it costs            — the tests the run spends its time in
 *
 * and one across-run question, kept last because it is the one people act on
 * over weeks rather than today: which tests are stable, flaky or simply broken.
 *
 * Two altitudes are mixed on this page and the copy never lets them blur. The
 * taxonomy, suite hotspots, ownership and time cards describe the last few
 * FINISHED RUNS — five of them, named in the page subtitle — because one run's
 * failures are one run's luck and the pattern is the thing worth acting on.
 * The verdicts table describes history over the whole window, computed by the
 * same code that feeds the overview. Every card says which of the two it is in
 * its hint, since "22 failures" means nothing until you know whether that is
 * this afternoon or this month.
 *
 * Nothing here is computed twice: the store fetches those runs and the
 * overview, and every panel below is a pure function of those objects.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var charts = TestHub.charts;
  var windows = TestHub.windows;
  var el = util.el;

  TestHub.views = TestHub.views || {};

  var FIELDS = ui.portfolioFields();

  //: How many pieces of evidence a taxonomy row shows before it says "and N more".
  var CHIP_LIMIT = 6;
  //: Suites, owners and slow tests are rankings, not inventories. Past a dozen
  //: rows nobody is reading them, and the bars stop being comparable.
  var RANK_LIMIT = 12;
  //: The bar chart carries a label under every bar, so it runs out of width
  //: before a table of the same rows would.
  var SUITE_LIMIT = 10;

  function create(ctx) {
    var store = ctx.store;
    var pending = [];

    var panel = ui.filterPanel({
      label: 'Portfolio filters',
      fields: FIELDS,
      values: store.state.filters,
      onChange: function (key, value) {
        var patch = {};
        patch[key] = value;
        util.navigate(store.hashWith(patch));
      },
      onReset: function () {
        util.navigate(util.buildHash(['analytics'], {}));
      }
    });

    var subtitle = el('p', { className: 'page-head__sub', text: '' });
    var content = el('div', { className: 'stack' });
    var root = el('div', {}, [
      el('div', { className: 'page-head' }, [
        el('div', { className: 'page-head__titles' }, [
          el('h1', { text: 'Analytics' }),
          subtitle
        ])
      ]),
      panel.el,
      el('div', { className: 'section' }, content)
    ]);

    function chart(spec, className) {
      var holder = el('div', { className: 'chart' + (className ? ' ' + className : '') });
      pending.push(function () { charts.draw(holder, spec); });
      return holder;
    }

    /* ------------------------------------------------------- derivations */

    /**
     * What to file a failure under. The exception class when the framework
     * reported one; otherwise the honest answer, which is that we do not know —
     * never the test's own name, which would give every failure its own bucket
     * and make the taxonomy a list.
     */
    function failureType(test) {
      var attempts = test.attempts || [];
      for (var i = attempts.length - 1; i >= 0; i--) {
        var failure = attempts[i].failure;
        if (failure && failure.type) { return failure.type; }
      }
      return 'Unclassified';
    }

    function isFailure(status) {
      return status === 'failed' || status === 'error';
    }

    function testDuration(test) {
      if (typeof test.duration_ms === 'number') { return test.duration_ms; }
      var attempts = test.attempts || [];
      var total = 0;
      for (var i = 0; i < attempts.length; i++) { total += attempts[i].duration_ms || 0; }
      return total;
    }

    function testHash(test) {
      return util.buildHash(['tests', test.id], {});
    }

    /** Group callback over a list, keyed by a string, preserving first-seen order. */
    function groupBy(items, keyFor) {
      var index = {};
      var groups = [];
      for (var i = 0; i < items.length; i++) {
        var key = keyFor(items[i]);
        var group = index[key];
        if (!group) {
          group = index[key] = { key: key, items: [] };
          groups.push(group);
        }
        group.items.push(items[i]);
      }
      return groups;
    }

    /** Totals over `{test, detail}` entries — one entry is one result, not one test. */
    function tally(entries) {
      var out = { total: entries.length, failing: 0, flaky: 0, passed: 0, duration_ms: 0, executed: 0 };
      for (var i = 0; i < entries.length; i++) {
        var test = entries[i].test;
        if (isFailure(test.status) || test.status === 'xpassed') { out.failing += 1; }
        if (test.status === 'passed' || test.status === 'xfailed') { out.passed += 1; }
        if (test.status !== 'skipped' && test.status !== 'cancelled') { out.executed += 1; }
        if (test.flaky) { out.flaky += 1; }
        out.duration_ms += testDuration(test);
      }
      out.pass_rate = out.executed > 0 ? (100 * out.passed) / out.executed : null;
      return out;
    }

    /**
     * Pass rate, less how often the suite disagrees with itself.
     *
     * A suite where every test passes on the second attempt is not a healthy
     * suite, and a pass rate alone says it is. The penalty is the share of
     * executed tests that flaked, so a 100%-passing suite with one test in ten
     * flipping scores 90, not 100 — which is the number people would have given
     * it by hand.
     */
    function stabilityScore(counts) {
      if (!counts || typeof counts.pass_rate !== 'number' || !counts.executed) { return null; }
      var penalty = (100 * (counts.flaky || 0)) / counts.executed;
      return Math.max(0, Math.min(100, counts.pass_rate - penalty));
    }

    function scoreTone(score) {
      if (score === null) { return null; }
      if (score >= 90) { return 'passed'; }
      if (score >= 75) { return 'skipped'; }
      return 'failed';
    }

    /* ------------------------------------------------------------ render */

    function render(state) {
      pending = [];

      if (state.errors.analytics) {
        util.replace(content, ui.card({
          title: 'Analytics',
          body: ui.errorState({
            title: 'The analytics did not load',
            error: state.errors.analytics,
            onRetry: function () { store.retry(); }
          })
        }));
        return;
      }

      if (!state.analytics || !state.overview) {
        subtitle.textContent = 'Reading the most recent finished runs…';
        util.replace(content, el('div', { className: 'stack' }, [
          el('div', { className: 'stats' }, [0, 1, 2, 3, 4, 5].map(function () {
            return el('div', { className: 'stat' }, ui.skeleton('card', 1));
          })),
          ui.card({ title: 'Why runs fail', body: ui.loadingBlock('Loading analytics', 'row', 4) })
        ]));
        return;
      }

      var overview = state.overview;
      var analytics = state.analytics;
      var details = analytics.runs || [];
      var summaries = analytics.summaries || [];
      var latest = details.length ? details[0] : null;
      // Every test of every loaded run, each tagged with the run it came from
      // so a chip can link back to it and so nothing double-counts silently.
      var tests = [];
      for (var d = 0; d < details.length; d++) {
        var runTests = details[d].tests || [];
        for (var t = 0; t < runTests.length; t++) {
          tests.push({ test: runTests[t], detail: details[d] });
        }
      }

      subtitle.textContent = describe(overview, summaries, details, tests.length);

      var nodes = [statsRow(overview, latest, summaries[0], tests)];

      if (!summaries.length) {
        nodes.push(ui.card({
          title: 'Why runs fail',
          icon: 'bug',
          body: ui.emptyState({
            title: 'No finished run in this window',
            hint: 'Analytics reads the runs that finished. Widen the window, clear a ' +
              'filter, or wait for the run in flight to end.'
          })
        }));
        util.replace(content, el('div', { className: 'stack' }, nodes));
        return;
      }

      if (analytics.partial) {
        nodes.push(ui.banner({
          tone: 'warn',
          title: 'Some runs did not load',
          text: 'These numbers cover ' + util.formatNumber(details.length) + ' of the ' +
            util.formatNumber(summaries.length) + ' recent runs. The rest did not answer.'
        }));
      }

      nodes.push(taxonomyCard(details, tests));
      nodes.push(el('div', { className: 'grid grid--2' }, [
        suitesCard(tests),
        ownersCard(tests)
      ]));
      nodes.push(el('div', { className: 'grid grid--2' }, [
        slowestCard(tests, details.length),
        verdictsCard(overview)
      ]));

      util.replace(content, el('div', { className: 'stack' }, nodes));
      for (var i = 0; i < pending.length; i++) { pending[i](); }
      pending = [];
    }

    function describe(overview, summaries, details, testCount) {
      var scope = util.formatNumber(overview.total_runs) + ' ' +
        util.pluralize(overview.total_runs, 'run') + ' ' + windows.during(windows.of(overview));
      if (!summaries.length) { return 'Across ' + scope + ' — none of them finished.'; }
      var newest = summaries[0];
      return 'Across ' + scope + '. The run detail below covers the last ' +
        util.formatNumber(details.length) + ' finished ' + util.pluralize(details.length, 'run') +
        ' — ' + util.formatNumber(testCount) + ' ' + util.pluralize(testCount, 'result') +
        ', newest ' + util.timeAgo(newest.finished_at || newest.started_at) + '.';
    }

    /* ------------------------------------------------------------- tiles */

    function statsRow(overview, latest, newest, entries) {
      var counts = overview.counts;
      var runCounts = (newest && newest.counts) || {};
      var comparison = (latest && latest.comparison) || {};
      var score = stabilityScore(counts);
      var totals = tally(entries);
      var failingNow = (runCounts.failed || 0) + (runCounts.error || 0);
      var latestTests = (latest && latest.tests) || [];

      return el('div', { className: 'stats' }, [
        ui.statTile({
          value: score === null ? util.DASH : String(Math.round(score)),
          label: 'Stability score',
          tone: scoreTone(score),
          hint: 'pass rate, less how often tests flip'
        }),
        ui.statTile({
          value: util.formatPercent(
            typeof runCounts.pass_rate === 'number' ? runCounts.pass_rate : null, 1),
          label: 'Pass rate, last run',
          delta: typeof comparison.pass_rate_delta === 'number' ? comparison.pass_rate_delta : null,
          hint: comparison.baseline_run_id
            ? 'against the previous comparable run'
            : 'no comparable run to measure against'
        }),
        ui.statTile({
          value: util.formatNumber(counts.flaky || 0),
          label: 'Flaky tests',
          tone: counts.flaky ? 'xpassed' : null,
          hint: 'failed, then passed, inside one run'
        }),
        ui.statTile({
          value: util.formatNumber(failingNow),
          label: 'Failing in the last run',
          tone: failingNow ? 'failed' : 'passed',
          hint: failingNow
            ? util.formatNumber(distinctSuites(latestTests, true)) + ' of ' +
              util.formatNumber(distinctSuites(latestTests, false)) + ' suites affected'
            : 'nothing red when it finished'
        }),
        ui.statTile({
          value: util.formatNumber(overview.total_runs),
          label: 'Runs analysed',
          hint: windows.during(windows.of(overview))
        }),
        ui.statTile({
          value: util.formatDuration(totals.duration_ms),
          label: 'Time in tests',
          hint: 'every attempt of the runs below'
        })
      ]);
    }

    function distinctSuites(tests, failingOnly) {
      var seen = {};
      var count = 0;
      for (var i = 0; i < tests.length; i++) {
        if (failingOnly && !isFailure(tests[i].status)) { continue; }
        var key = tests[i].suite_path || '';
        if (seen[key]) { continue; }
        seen[key] = true;
        count += 1;
      }
      return count;
    }

    /* ---------------------------------------------------------- taxonomy */

    /**
     * Failures grouped by what raised them. Forty failures that are all one
     * AssertionError in one fixture is one bug; forty spread over six exception
     * types is six. That distinction is the whole point of the card, so the
     * types are ordered by weight and each carries its own evidence — and a
     * test that failed the same way in four of the five runs says so with a
     * multiplier rather than appearing four times.
     */
    function taxonomyCard(details, entries) {
      var failed = entries.filter(function (entry) { return isFailure(entry.test.status); });
      var newly = {};
      var newlyFailing = (details.length && details[0].comparison &&
        details[0].comparison.newly_failing) || [];
      for (var n = 0; n < newlyFailing.length; n++) { newly[String(newlyFailing[n])] = true; }

      if (!failed.length) {
        return ui.card({
          title: 'Why runs fail',
          icon: 'bug',
          hint: 'The last ' + util.formatNumber(details.length) + ' ' +
            util.pluralize(details.length, 'run'),
          body: ui.emptyState({
            title: 'Nothing failed in those runs',
            hint: 'When something does, it is grouped here by the exception that raised it.',
            icon: 'check'
          })
        });
      }

      var groups = groupBy(failed, function (entry) { return failureType(entry.test); })
        .sort(function (a, b) { return b.items.length - a.items.length; });

      var rows = groups.map(function (group) {
        var share = (100 * group.items.length) / failed.length;
        var distinct = dedupeTests(group.items);
        var chips = distinct.slice(0, CHIP_LIMIT).map(function (item) {
          return ui.chip(item.times > 1 ? item.test.name + ' ×' + item.times : item.test.name,
            testHash(item.test));
        });
        if (distinct.length > CHIP_LIMIT) {
          chips.push(el('span', {
            className: 'subtle',
            text: 'and ' + util.formatNumber(distinct.length - CHIP_LIMIT) + ' more'
          }));
        }
        var isNew = distinct.some(function (item) { return newly[String(item.test.id)] === true; });
        return el('div', { className: 'taxonomy__row' }, [
          el('div', { className: 'taxonomy__head' }, [
            el('span', { className: 'taxonomy__type', text: group.key }),
            el('span', { className: 'taxonomy__count num', text: util.formatNumber(group.items.length) }),
            el('span', { className: 'subtle num', text: util.formatPercent(share, 0) }),
            // Only when they differ: "3 failures, 3 tests" is the same fact
            // twice, while "12 failures, 3 tests" is the interesting one.
            distinct.length === group.items.length ? null
              : el('span', { className: 'subtle', text: util.formatNumber(distinct.length) + ' ' +
                util.pluralize(distinct.length, 'test') }),
            isNew ? ui.verdictBadge('new', 'At least one of these was passing in the run before last')
              : null
          ]),
          ui.meter({
            // Against every failure, not against the biggest bucket: the bar is
            // answering "how much of the red is this", and a page where the top
            // row is always full-width answers nothing.
            value: group.items.length, max: failed.length, status: 'failed',
            ariaLabel: group.key + ': ' + group.items.length + ' of ' + failed.length + ' failures'
          }),
          el('div', { className: 'chips' }, chips)
        ]);
      });

      return ui.card({
        title: 'Why runs fail',
        icon: 'bug',
        hint: util.formatNumber(failed.length) + ' ' + util.pluralize(failed.length, 'failure') +
          ' across the last ' + util.formatNumber(details.length) + ' ' +
          util.pluralize(details.length, 'run'),
        body: el('div', { className: 'taxonomy' }, rows)
      });
    }

    /**
     * Collapse entries onto the test identity they share. `id` is stable across
     * runs by construction (test_hub.domain.identity), which is exactly what
     * makes "this failed in four of the last five" a sentence we can write.
     */
    function dedupeTests(entries) {
      var index = {};
      var out = [];
      for (var i = 0; i < entries.length; i++) {
        var key = String(entries[i].test.id);
        if (index[key]) {
          index[key].times += 1;
          continue;
        }
        index[key] = { test: entries[i].test, detail: entries[i].detail, times: 1 };
        out.push(index[key]);
      }
      return out.sort(function (a, b) { return b.times - a.times; });
    }

    /* ------------------------------------------------------------ suites */

    function suitesCard(entries) {
      var ranked = groupBy(entries, function (entry) {
        return entry.test.suite_path || 'unattributed';
      }).map(function (group) {
        return { label: group.key, counts: tally(group.items) };
      }).sort(function (a, b) {
        if (b.counts.failing !== a.counts.failing) { return b.counts.failing - a.counts.failing; }
        return b.counts.total - a.counts.total;
      }).slice(0, SUITE_LIMIT);

      if (!ranked.length) {
        return ui.card({
          title: 'Suite hotspots',
          icon: 'layers',
          body: ui.emptyState({ title: 'Those runs reported no tests' })
        });
      }

      return ui.card({
        title: 'Suite hotspots',
        icon: 'layers',
        hint: 'Recent runs, worst first',
        body: [
          chart({
            kind: 'stackedBar',
            title: 'Results per suite across the recent runs',
            description: 'Failing results stacked against the rest, for the busiest suites.',
            height: 230,
            xLabel: 'Suite',
            buckets: ranked.map(function (entry) {
              return {
                label: shortSuite(entry.label),
                values: {
                  rest: Math.max(0, entry.counts.total - entry.counts.failing),
                  failing: entry.counts.failing
                }
              };
            }),
            keys: [
              { key: 'rest', label: 'Everything else', color: '--status-passed' },
              { key: 'failing', label: 'Failed or errored', color: '--status-failed' }
            ],
            emptyMessage: 'No suites in these runs'
          }),
          el('div', { className: 'legend' }, [
            el('span', { className: 'legend__item' }, [
              el('span', { className: 'legend__swatch', dataset: { status: 'passed' } }),
              el('span', { text: 'Everything else' })
            ]),
            el('span', { className: 'legend__item' }, [
              el('span', { className: 'legend__swatch', dataset: { status: 'failed' } }),
              el('span', { text: 'Failed or errored' })
            ])
          ])
        ]
      });
    }

    /**
     * The identifying tail of a suite path, for an axis label.
     *
     * `e2e/basket.spec.ts` is "basket.spec.ts", not "ts": the file name is what
     * people call the suite. A dotted path with no slashes is a class name
     * (`com.example.ledger.PostingTest`), and there the tail after the last dot
     * is the identifying part.
     */
    function shortSuite(path) {
      var text = String(path);
      var tail = text.indexOf('/') >= 0
        ? text.slice(text.lastIndexOf('/') + 1)
        : text.slice(text.lastIndexOf('.') + 1);
      tail = tail || text;
      // Twelve characters is what fits between two bars before the axis labels
      // start overlapping each other; the full path is in the chart's title.
      return tail.length > 12 ? tail.slice(0, 11) + '…' : tail;
    }

    /* ------------------------------------------------------------ owners */

    function ownersCard(entries) {
      var rows = groupBy(entries, function (entry) { return entry.test.owner || ''; })
        .map(function (group) {
          var counts = tally(group.items);
          return {
            owner: group.key,
            total: counts.total,
            failing: counts.failing,
            flaky: counts.flaky,
            pass_rate: counts.pass_rate,
            duration_ms: counts.duration_ms
          };
        }).sort(function (a, b) { return b.total - a.total; });

      if (!rows.length) {
        return ui.card({
          title: 'Who owns what',
          icon: 'users',
          body: ui.emptyState({ title: 'Those runs reported no tests' })
        });
      }

      var owned = rows.filter(function (row) { return row.owner !== ''; });
      var unclaimed = rows.filter(function (row) { return row.owner === ''; });
      var largest = rows[0].total;

      return ui.card({
        title: 'Who owns what',
        icon: 'users',
        flush: true,
        hint: owned.length
          ? util.formatNumber(owned.length) + ' ' + util.pluralize(owned.length, 'owner') +
            (unclaimed.length
              ? ', ' + util.formatNumber(unclaimed[0].total) + ' results unclaimed'
              : '')
          : 'Nothing in these runs declares an owner',
        body: ui.table({
          caption: 'Ownership across the recent runs',
          columns: [
            {
              key: 'owner', label: 'Owner',
              render: function (row) {
                return row.owner
                  ? ui.tag(row.owner)
                  : el('span', { className: 'subtle', text: 'Unclaimed' });
              }
            },
            {
              key: 'total', label: 'Results', align: 'right',
              render: function (row) {
                return el('div', { className: 'row row--tight cell-metric' }, [
                  ui.meter({ value: row.total, max: largest, status: 'total',
                    ariaLabel: row.total + ' results' }),
                  el('span', { className: 'num', text: util.formatNumber(row.total) })
                ]);
              }
            },
            {
              key: 'pass_rate', label: 'Pass rate', align: 'right',
              render: function (row) { return util.formatPercent(row.pass_rate, 1); }
            },
            {
              key: 'failing', label: 'Failing', align: 'right',
              render: function (row) {
                return row.failing
                  ? el('span', { className: 'statuspill', dataset: { status: 'failed' },
                    text: util.formatNumber(row.failing) })
                  : el('span', { className: 'subtle', text: '0' });
              }
            },
            {
              key: 'flaky', label: 'Flaky', align: 'right',
              render: function (row) {
                return row.flaky
                  ? el('span', { className: 'statuspill', dataset: { status: 'xpassed' },
                    text: util.formatNumber(row.flaky) })
                  : el('span', { className: 'subtle', text: '0' });
              }
            },
            {
              key: 'duration_ms', label: 'Time', align: 'right',
              render: function (row) { return util.formatDuration(row.duration_ms); }
            }
          ],
          rows: rows
        })
      });
    }

    /* ----------------------------------------------------------- slowest */

    /**
     * Time is summed per test identity across the loaded runs, not per result.
     * A test that takes four seconds every run costs twenty seconds of the last
     * five, and that — not its single worst appearance — is the number that
     * decides whether it is worth fixing.
     */
    function slowestCard(entries, runCount) {
      var index = {};
      var rows = [];
      var total = 0;
      for (var i = 0; i < entries.length; i++) {
        var test = entries[i].test;
        var duration = testDuration(test);
        total += duration;
        var key = String(test.id);
        if (!index[key]) {
          index[key] = { test: test, duration_ms: 0, runs: 0, failing: 0 };
          rows.push(index[key]);
        }
        index[key].duration_ms += duration;
        index[key].runs += 1;
        if (isFailure(test.status)) { index[key].failing += 1; }
      }

      var ranked = rows.filter(function (row) { return row.duration_ms > 0; })
        .sort(function (a, b) { return b.duration_ms - a.duration_ms; })
        .slice(0, RANK_LIMIT);

      if (!ranked.length) {
        return ui.card({
          title: 'Where the time goes',
          icon: 'clock',
          body: ui.emptyState({
            title: 'Those runs reported no durations',
            hint: 'Not every framework times individual tests; the run totals still stand.'
          })
        });
      }

      var slowest = ranked[0].duration_ms;
      var rankedTotal = 0;
      for (var r = 0; r < ranked.length; r++) { rankedTotal += ranked[r].duration_ms; }
      var share = total > 0 ? (100 * rankedTotal) / total : null;

      return ui.card({
        title: 'Where the time goes',
        icon: 'clock',
        flush: true,
        hint: share === null ? null
          : util.formatPercent(share, 0) + ' of the last ' + util.formatNumber(runCount) + ' ' +
            util.pluralize(runCount, 'run') + ' is these ' + ranked.length,
        body: ui.table({
          caption: 'The slowest tests across the recent runs',
          columns: [
            {
              key: 'name', label: 'Test',
              render: function (row) {
                return el('div', { className: 'cell-name' }, [
                  el('div', { className: 'cell-main truncate' },
                    ui.link(testHash(row.test), row.test.name)),
                  el('div', { className: 'cell-sub truncate', text: row.test.suite_path || '' })
                ]);
              }
            },
            {
              key: 'runs', label: 'Ran', align: 'right',
              render: function (row) {
                return el('span', { className: 'subtle num nowrap',
                  text: util.formatNumber(row.runs) + '×' });
              }
            },
            {
              key: 'duration_ms', label: 'Total time', align: 'right',
              render: function (row) {
                return el('div', { className: 'row row--tight cell-metric' }, [
                  ui.meter({ value: row.duration_ms, max: slowest,
                    status: row.failing ? 'failed' : 'running',
                    ariaLabel: util.formatDuration(row.duration_ms) }),
                  el('span', { className: 'num nowrap', text: util.formatDuration(row.duration_ms) })
                ]);
              }
            }
          ],
          rows: ranked
        })
      });
    }

    /* ---------------------------------------------------------- verdicts */

    /**
     * The one card on the page that is about history rather than the last run.
     *
     * `broken` outranks `flaky` outranks `stable` because that is the order
     * anybody works through them in: a test that has never passed is not a
     * flaky test, it is an unfinished one.
     */
    function verdictFor(test) {
      var history = test.history || [];
      var executed = history.filter(function (point) {
        return point.status !== 'skipped' && point.status !== 'cancelled';
      });
      if (executed.length < 2) { return 'quiet'; }
      var passed = 0;
      var failed = 0;
      for (var i = 0; i < executed.length; i++) {
        if (util.isSuccessful(executed[i].status)) { passed += 1; }
        if (util.isFailureish(executed[i].status)) { failed += 1; }
      }
      if (!passed) { return 'broken'; }
      if (test.flake_rate || (passed && failed)) { return 'flaky'; }
      return 'stable';
    }

    function verdictsCard(overview) {
      var byId = {};
      var merged = [];
      var lists = [overview.top_failing_tests || [], overview.flakiest_tests || []];
      for (var l = 0; l < lists.length; l++) {
        for (var t = 0; t < lists[l].length; t++) {
          var test = lists[l][t];
          var key = String(test.test_id);
          if (byId[key]) { continue; }
          byId[key] = true;
          merged.push(test);
        }
      }

      if (overview.test_lists === 'unavailable' || !merged.length) {
        return ui.card({
          title: 'What history says',
          icon: 'chart',
          body: ui.emptyState({
            title: overview.test_lists === 'unavailable'
              ? 'Not available for this filter'
              : 'Nothing has failed or flipped',
            hint: overview.test_lists === 'unavailable'
              ? 'Per-test history is not carried for this slice — clear the filters to see it.'
              : 'Every test in this window agreed with itself every time it ran.'
          })
        });
      }

      merged.sort(function (a, b) {
        return (b.failure_rate || 0) + (b.flake_rate || 0) - (a.failure_rate || 0) - (a.flake_rate || 0);
      });

      return ui.card({
        title: 'What history says',
        icon: 'chart',
        flush: true,
        hint: windows.across(windows.of(overview)),
        body: ui.table({
          caption: 'Per-test verdicts over the window',
          columns: [
            {
              key: 'name', label: 'Test',
              render: function (row) {
                // Capped, and ellipsised when it has to be: a test name is as
                // long as somebody felt like making it, and four columns of
                // history do not fit beside an uncapped one.
                return el('div', { className: 'cell-name' }, [
                  el('div', { className: 'cell-main truncate' },
                    ui.link(util.buildHash(['tests', row.test_id]), row.name)),
                  el('div', { className: 'cell-sub truncate', text: row.suite_path || '' })
                ]);
              }
            },
            {
              key: 'history', label: 'Recent runs',
              render: function (row) {
                // History arrives newest-first; a streak reads left to right.
                var points = (row.history || []).slice().reverse().map(function (point) {
                  return {
                    status: point.status,
                    title: util.statusLabel(point.status) + ' · ' + util.formatDate(point.started_at)
                  };
                });
                if (!points.length) { return el('span', { className: 'subtle', text: util.DASH }); }
                return ui.streak(points, {
                  limit: 8,
                  ariaLabel: row.name + ': the last ' + points.length + ' runs'
                });
              }
            },
            {
              key: 'verdict', label: 'Verdict',
              render: function (row) {
                var verdict = verdictFor(row);
                return ui.verdictBadge(verdict, VERDICT_HINTS[verdict]);
              }
            },
            {
              key: 'failure_rate', label: 'Fail rate', align: 'right',
              render: function (row) { return util.formatPercent(row.failure_rate, 1); }
            }
          ],
          rows: merged
        })
      });
    }

    var VERDICT_HINTS = {
      stable: 'Passed every time it ran in this window',
      flaky: 'Both passed and failed in this window',
      broken: 'Has not passed in this window',
      quiet: 'Too few runs in this window to say'
    };

    return {
      el: root,
      update: function (state, section) {
        panel.sync(state.filters, state.facets || {});
        if (section === 'facets' || section === 'status') { return; }
        render(state);
      },
      destroy: function () {
        charts.forget(content);
      }
    };
  }

  TestHub.views.analytics = { create: create };
})(window);
