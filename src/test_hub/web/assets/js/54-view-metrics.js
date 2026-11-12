/* Test Hub — test metrics: one row per test, measured.
 *
 * The run page shows what one test did once. The test page shows what one test
 * did over time. Neither answers the question this page exists for: across
 * everything that just ran, which tests cost the most, retry the most, and
 * disagree with themselves the most — sorted by whichever of those the reader
 * happens to care about this morning.
 *
 * Rows are keyed on the test IDENTITY (`test.id`, stable across runs — see
 * test_hub.domain.identity), not on the result. A test that ran in four of the
 * five loaded runs is one row that says "4 runs", never four rows.
 *
 * Two numbers here are easy to misread and are labelled so they cannot be:
 *
 *   "Time"     is the total across every attempt in the loaded runs, because
 *              that is what the pipeline actually paid for. A test that takes
 *              two seconds and retries three times cost six.
 *   "Per run"  is that total divided by the runs the test appeared in — the
 *              number to compare between tests, since not every test ran in
 *              every run.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var windows = TestHub.windows;
  var el = util.el;

  TestHub.views = TestHub.views || {};

  var FIELDS = ui.portfolioFields();

  //: Rows drawn at once. Past a few hundred the table stops being a table and
  //: the browser starts laying out more DOM than anybody scrolls through; the
  //: search box is how you get to row four hundred.
  var ROW_LIMIT = 250;

  var LENSES = [
    { value: 'all', label: 'All' },
    { value: 'failing', label: 'Failing' },
    { value: 'flaky', label: 'Flaky' },
    { value: 'retried', label: 'Retried' },
    { value: 'slow', label: 'Slowest' }
  ];

  function create(ctx) {
    var store = ctx.store;

    // View-local, like the suites page: these reorder rows already in memory.
    var search = '';
    var lens = 'all';
    var sort = { key: 'duration_ms', dir: 'desc' };
    var lastState = null;

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
        util.navigate(util.buildHash(['metrics'], {}));
      }
    });

    var subtitle = el('p', { className: 'page-head__sub', text: '' });
    var content = el('div', { className: 'stack' });
    var root = el('div', {}, [
      el('div', { className: 'page-head' }, [
        el('div', { className: 'page-head__titles' }, [
          el('h1', { text: 'Test Metrics' }),
          subtitle
        ])
      ]),
      panel.el,
      el('div', { className: 'section' }, content)
    ]);

    /* ------------------------------------------------------- derivations */

    function testDuration(test) {
      if (typeof test.duration_ms === 'number') { return test.duration_ms; }
      var attempts = test.attempts || [];
      var total = 0;
      for (var i = 0; i < attempts.length; i++) { total += attempts[i].duration_ms || 0; }
      return total;
    }

    /**
     * Fold every result in the loaded runs down to one row per test identity.
     *
     * `details` arrives newest-first, so the first result seen for an identity
     * is the most recent one — which is what `last_status` means and why it is
     * only assigned once.
     */
    function foldTests(details) {
      var index = {};
      var rows = [];
      var i, j;

      for (i = 0; i < details.length; i++) {
        var detail = details[i];
        var tests = detail.tests || [];
        for (j = 0; j < tests.length; j++) {
          var test = tests[j];
          var key = String(test.id);
          var row = index[key];
          if (!row) {
            row = index[key] = {
              id: test.id,
              name: test.full_name || test.name,
              suite_path: test.suite_path || '',
              owner: test.owner || '',
              runs: 0,
              passed: 0,
              failing: 0,
              skipped: 0,
              executed: 0,
              attempts: 0,
              retried: 0,
              flaky: 0,
              duration_ms: 0,
              max_ms: 0,
              last_status: test.status,
              last_run: detail.run
            };
            rows.push(row);
          }
          var duration = testDuration(test);
          var attempts = (test.attempts || []).length;
          row.runs += 1;
          row.attempts += attempts;
          if (attempts > 1) { row.retried += 1; }
          if (test.flaky) { row.flaky += 1; }
          if (test.status === 'passed' || test.status === 'xfailed') { row.passed += 1; }
          if (util.isFailureish(test.status)) { row.failing += 1; }
          if (test.status === 'skipped') { row.skipped += 1; }
          if (test.status !== 'skipped' && test.status !== 'cancelled') { row.executed += 1; }
          row.duration_ms += duration;
          if (duration > row.max_ms) { row.max_ms = duration; }
        }
      }

      for (i = 0; i < rows.length; i++) {
        rows[i].pass_rate = rows[i].executed > 0
          ? (100 * rows[i].passed) / rows[i].executed
          : null;
        rows[i].per_run = rows[i].runs > 0 ? rows[i].duration_ms / rows[i].runs : 0;
      }
      return rows;
    }

    function inLens(row) {
      if (lens === 'failing') { return row.failing > 0; }
      if (lens === 'flaky') { return row.flaky > 0; }
      if (lens === 'retried') { return row.retried > 0; }
      // "Slowest" is not a filter — it is the sort, applied to everything. It
      // lives beside the filters because that is where people look for it.
      return true;
    }

    function matches(row) {
      if (!search) { return true; }
      var text = (row.name + ' ' + row.suite_path + ' ' + row.owner).toLowerCase();
      return text.indexOf(search.toLowerCase()) >= 0;
    }

    function ordered(rows) {
      var key = lens === 'slow' ? 'duration_ms' : sort.key;
      var factor = lens === 'slow' ? -1 : (sort.dir === 'asc' ? 1 : -1);
      return rows.slice().sort(function (a, b) {
        if (key === 'name') {
          return factor * util.compare(String(a.name), String(b.name));
        }
        var left = a[key];
        var right = b[key];
        if (left === null || left === undefined) { return 1; }
        if (right === null || right === undefined) { return -1; }
        if (left === right) { return util.compare(String(a.name), String(b.name)); }
        return factor * (left < right ? -1 : 1);
      });
    }

    function repaint() {
      if (lastState) { render(lastState); }
    }

    /* ------------------------------------------------------------ render */

    function render(state) {
      lastState = state;

      if (state.errors.analytics) {
        util.replace(content, ui.card({
          title: 'Test Metrics',
          body: ui.errorState({
            title: 'The metrics did not load',
            error: state.errors.analytics,
            onRetry: function () { store.retry(); }
          })
        }));
        return;
      }

      if (!state.analytics || !state.overview) {
        subtitle.textContent = 'Reading the most recent finished runs…';
        util.replace(content, el('div', { className: 'stack' }, [
          el('div', { className: 'stats' }, [0, 1, 2, 3].map(function () {
            return el('div', { className: 'stat' }, ui.skeleton('card', 1));
          })),
          ui.card({ title: 'Test Metrics', body: ui.loadingBlock('Loading metrics', 'row', 6) })
        ]));
        return;
      }

      var details = state.analytics.runs || [];
      var summaries = state.analytics.summaries || [];

      if (!summaries.length) {
        subtitle.textContent = windows.across(windows.of(state.overview)) + '.';
        util.replace(content, ui.card({
          title: 'Test Metrics',
          icon: 'rows',
          body: ui.emptyState({
            title: 'No finished run in this window',
            hint: 'Metrics are measured on the runs that finished. Widen the window, ' +
              'clear a filter, or wait for the run in flight to end.'
          })
        }));
        return;
      }

      var rows = foldTests(details);
      var totals = { duration: 0, attempts: 0, results: 0, retried: 0, flaky: 0 };
      var i;
      for (i = 0; i < rows.length; i++) {
        totals.duration += rows[i].duration_ms;
        totals.attempts += rows[i].attempts;
        totals.results += rows[i].runs;
        if (rows[i].retried) { totals.retried += 1; }
        if (rows[i].flaky) { totals.flaky += 1; }
      }

      subtitle.textContent = util.formatNumber(rows.length) + ' ' +
        util.pluralize(rows.length, 'test') + ' measured across the last ' +
        util.formatNumber(details.length) + ' finished ' +
        util.pluralize(details.length, 'run') + '.';

      var visible = ordered(rows.filter(function (row) {
        return inLens(row) && matches(row);
      }));

      util.replace(content, el('div', { className: 'stack' }, [
        el('div', { className: 'stats' }, [
          ui.statTile({
            value: util.formatNumber(rows.length),
            label: 'Tests measured',
            hint: 'Distinct test identities, not results'
          }),
          ui.statTile({
            value: util.formatDuration(
              totals.results ? totals.duration / totals.results : 0
            ),
            label: 'Average result',
            hint: 'Mean time one test spends in one run'
          }),
          ui.statTile({
            value: util.formatNumber(totals.retried),
            label: 'Tests that retried',
            tone: totals.retried ? 'skipped' : 'passed',
            hint: totals.attempts > totals.results
              ? util.formatNumber(totals.attempts - totals.results) + ' extra attempts paid for'
              : 'Every test ran once'
          }),
          ui.statTile({
            value: util.formatNumber(totals.flaky),
            label: 'Flaky within a run',
            tone: totals.flaky ? 'failed' : 'passed',
            hint: 'Both failed and passed inside one run'
          })
        ]),
        metricsCard(rows, visible)
      ]));
    }

    function metricsCard(all, rows) {
      var slowest = 0;
      var i;
      for (i = 0; i < rows.length; i++) {
        if (rows[i].duration_ms > slowest) { slowest = rows[i].duration_ms; }
      }
      var shown = rows.slice(0, ROW_LIMIT);

      var lensGroup = ui.segmented({
        label: 'What to measure',
        value: lens,
        options: LENSES,
        onChange: function (value) { lens = value; repaint(); }
      });

      var finder = ui.searchField({
        id: 'th-metric-search',
        key: 'metric',
        label: 'Find a test',
        placeholder: 'Test, suite or owner',
        value: search,
        onChange: function (_key, value) { search = value; repaint(); }
      });

      var body;
      if (!rows.length) {
        body = ui.emptyState({
          icon: 'search',
          title: search ? 'Nothing matches “' + search + '”' : 'Nothing in this lens',
          hint: util.formatNumber(all.length) + ' ' + util.pluralize(all.length, 'test') +
            ' ran in these runs.'
        });
      } else {
        body = ui.table({
          caption: 'Per-test metrics across the recent finished runs',
          sort: lens === 'slow' ? { key: 'duration_ms', dir: 'desc' } : sort,
          onSort: function (key, dir) {
            sort = { key: key, dir: dir };
            // Sorting by hand leaves the "slowest" lens, which is itself a sort.
            if (lens === 'slow') { lens = 'all'; }
            repaint();
          },
          columns: [
            {
              key: 'name', label: 'Test', sortable: true, defaultDir: 'asc',
              render: function (row) {
                return el('div', {}, [
                  el('div', { className: 'cell-main' },
                    ui.link(util.buildHash(['tests', row.id], {}), row.name)),
                  el('div', {
                    className: 'cell-sub truncate mono',
                    text: [row.suite_path, row.owner].filter(Boolean).join(' · ')
                  })
                ]);
              }
            },
            {
              key: 'last_status', label: 'Last', mobileLabel: 'Last outcome',
              render: function (row) { return ui.statusPill(row.last_status); }
            },
            {
              key: 'runs', label: 'Runs', align: 'right', sortable: true,
              render: function (row) { return util.formatNumber(row.runs); }
            },
            {
              key: 'pass_rate', label: 'Pass rate', align: 'right', sortable: true,
              render: function (row) {
                return row.pass_rate === null
                  ? el('span', { className: 'subtle', text: 'never ran' })
                  : util.formatPercent(row.pass_rate, 0);
              }
            },
            {
              key: 'retried', label: 'Retries', align: 'right', sortable: true,
              render: function (row) {
                var extra = row.attempts - row.runs;
                return extra
                  ? el('span', {
                    className: 'statuspill', dataset: { status: 'xpassed' },
                    text: util.formatNumber(extra)
                  })
                  : el('span', { className: 'subtle', text: '0' });
              }
            },
            {
              key: 'flaky', label: 'Flaky', align: 'right', sortable: true,
              render: function (row) {
                return row.flaky
                  ? el('span', {
                    className: 'statuspill', dataset: { status: 'failed' },
                    text: util.formatNumber(row.flaky)
                  })
                  : el('span', { className: 'subtle', text: '0' });
              }
            },
            {
              key: 'duration_ms', label: 'Time', align: 'right', sortable: true,
              render: function (row) {
                return el('div', { className: 'row row--tight cell-metric' }, [
                  ui.meter({
                    value: row.duration_ms, max: slowest, status: 'total',
                    ariaLabel: util.formatDuration(row.duration_ms) + ' in total'
                  }),
                  el('span', { className: 'num', text: util.formatDuration(row.duration_ms) })
                ]);
              }
            },
            {
              key: 'per_run', label: 'Per run', align: 'right', sortable: true,
              render: function (row) { return util.formatDuration(row.per_run); }
            }
          ],
          rows: shown
        });
      }

      return ui.card({
        title: 'Every test',
        icon: 'rows',
        flush: true,
        hint: rows.length > shown.length
          ? 'Showing ' + util.formatNumber(shown.length) + ' of ' +
            util.formatNumber(rows.length) + ' — search to narrow it'
          : util.formatNumber(rows.length) + ' ' + util.pluralize(rows.length, 'test'),
        // The controls go in a toolbar under the head, not in it: they are
        // labelled fields, and a labelled field in a card head makes the head
        // as tall as two rows and leaves the title floating beside it.
        body: [
          el('div', { className: 'card__toolbar' }, [
            el('div', { className: 'filters__row' }, [lensGroup.el, finder])
          ]),
          body
        ]
      });
    }

    return {
      el: root,
      update: function (state, section) {
        panel.sync(state.filters, state.facets || {});
        if (section === 'facets' || section === 'status') { return; }
        render(state);
      },
      destroy: function () {}
    };
  }

  TestHub.views.metrics = { create: create };
})(window);
