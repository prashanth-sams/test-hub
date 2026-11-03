/* Test Hub — test suites: the inventory, one row per suite.
 *
 * Analytics ranks the ten worst suites and stops there, because a ranking that
 * runs to two hundred rows is a list. This page is the list: every suite the
 * recent finished runs reported, searchable, sortable, and expandable into the
 * tests it contains.
 *
 * Suites are keyed on `path`, not on `id`. A suite row gets a fresh UUID in
 * every run — `tests/api/test_billing.py` in Tuesday's run and in Wednesday's
 * are two different `id`s and the same suite to everybody who reads it, so the
 * path is the identity and the aggregate spans the loaded runs.
 *
 * The search box and the sort are deliberately NOT in the URL, unlike the
 * portfolio filters above them. A filter changes which runs are fetched; a sort
 * changes the order of rows already in memory, and routing it through the hash
 * would put a round trip behind a column header.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

  TestHub.views = TestHub.views || {};

  var FIELDS = ui.portfolioFields();

  //: Tests shown inside one expanded suite before it says "and N more". The
  //: panel is a sample to recognise the suite by, not a second test list.
  var TEST_LIMIT = 40;

  function create(ctx) {
    var store = ctx.store;

    // View-local controls. See the header: these order and filter rows that are
    // already in memory, so they never touch the hash.
    var search = '';
    var sort = { key: 'failing', dir: 'desc' };
    var openPath = null;
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
        util.navigate(util.buildHash(['suites'], {}));
      }
    });

    var subtitle = el('p', { className: 'page-head__sub', text: '' });
    var content = el('div', { className: 'stack' });
    var root = el('div', {}, [
      el('div', { className: 'page-head' }, [
        el('div', { className: 'page-head__titles' }, [
          el('h1', { text: 'Test Suites' }),
          subtitle
        ])
      ]),
      panel.el,
      el('div', { className: 'section' }, content)
    ]);

    /* ------------------------------------------------------- derivations */

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

    /**
     * Every suite in the loaded runs, folded by path.
     *
     * The tests carry `suite_path` themselves, so the suite rows are only
     * consulted for a display name and for suites that reported no tests at all
     * — which is worth showing rather than dropping: an empty suite is usually
     * a collection error, and a page that silently omits it hides the problem.
     */
    function foldSuites(details) {
      var index = {};
      var rows = [];
      var i, j;

      function bucket(path, name) {
        var row = index[path];
        if (!row) {
          row = index[path] = {
            path: path,
            name: name || path,
            runs: 0,
            seenIn: {},
            tests: [],
            total: 0,
            passed: 0,
            failing: 0,
            skipped: 0,
            flaky: 0,
            retried: 0,
            executed: 0,
            duration_ms: 0
          };
          rows.push(row);
        }
        return row;
      }

      for (i = 0; i < details.length; i++) {
        var detail = details[i];
        var runId = String(detail.run.id);
        var byId = {};
        var suites = detail.suites || [];
        for (j = 0; j < suites.length; j++) {
          byId[String(suites[j].id)] = suites[j];
          var declared = bucket(suites[j].path || suites[j].name, suites[j].name);
          if (!declared.seenIn[runId]) { declared.seenIn[runId] = true; declared.runs += 1; }
        }

        var tests = detail.tests || [];
        for (j = 0; j < tests.length; j++) {
          var test = tests[j];
          var parent = byId[String(test.suite_id)];
          var path = test.suite_path || (parent && parent.path) || 'Uncollected';
          var row = bucket(path, (parent && parent.name) || path);
          if (!row.seenIn[runId]) { row.seenIn[runId] = true; row.runs += 1; }
          row.tests.push({ test: test, detail: detail });
          row.total += 1;
          if (test.status === 'passed' || test.status === 'xfailed') { row.passed += 1; }
          if (isFailure(test.status) || test.status === 'xpassed') { row.failing += 1; }
          if (test.status === 'skipped') { row.skipped += 1; }
          if (test.status !== 'skipped' && test.status !== 'cancelled') { row.executed += 1; }
          if (test.flaky) { row.flaky += 1; }
          if ((test.attempts || []).length > 1) { row.retried += 1; }
          row.duration_ms += testDuration(test);
        }
      }

      for (i = 0; i < rows.length; i++) {
        rows[i].pass_rate = rows[i].executed > 0
          ? (100 * rows[i].passed) / rows[i].executed
          : null;
      }
      return rows;
    }

    function matches(row, needle) {
      if (!needle) { return true; }
      var text = (row.path + ' ' + row.name).toLowerCase();
      return text.indexOf(needle.toLowerCase()) >= 0;
    }

    /** Sort by the chosen column, with a stable tiebreak so rows never jitter. */
    function ordered(rows) {
      var key = sort.key;
      var factor = sort.dir === 'asc' ? 1 : -1;
      return rows.slice().sort(function (a, b) {
        var left = a[key];
        var right = b[key];
        if (key === 'name') {
          return factor * util.compare(String(a.name), String(b.name));
        }
        // A suite that ran nothing has no pass rate. It sorts to the end of the
        // list in either direction rather than being read as a zero.
        if (left === null || left === undefined) { return 1; }
        if (right === null || right === undefined) { return -1; }
        if (left === right) { return util.compare(String(a.path), String(b.path)); }
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
          title: 'Test Suites',
          body: ui.errorState({
            title: 'The suites did not load',
            error: state.errors.analytics,
            onRetry: function () { store.retry(); }
          })
        }));
        return;
      }

      if (!state.analytics || !state.overview) {
        subtitle.textContent = 'Reading the most recent finished runs…';
        util.replace(content, ui.card({
          title: 'Test Suites',
          body: ui.loadingBlock('Loading suites', 'row', 6)
        }));
        return;
      }

      var details = state.analytics.runs || [];
      var summaries = state.analytics.summaries || [];

      if (!summaries.length) {
        subtitle.textContent = 'Across the last ' + state.overview.window_days + ' days.';
        util.replace(content, ui.card({
          title: 'Test Suites',
          icon: 'briefcase',
          body: ui.emptyState({
            title: 'No finished run in this window',
            hint: 'Suites are read from the runs that finished. Widen the window, ' +
              'clear a filter, or wait for the run in flight to end.'
          })
        }));
        return;
      }

      var rows = foldSuites(details);
      var visible = ordered(rows.filter(function (row) { return matches(row, search); }));
      var failingSuites = 0;
      var totalTests = 0;
      var i;
      for (i = 0; i < rows.length; i++) {
        totalTests += rows[i].total;
        if (rows[i].failing) { failingSuites += 1; }
      }

      subtitle.textContent = util.formatNumber(rows.length) + ' ' +
        util.pluralize(rows.length, 'suite') + ' across the last ' +
        util.formatNumber(details.length) + ' finished ' +
        util.pluralize(details.length, 'run') + ', newest first.';

      var nodes = [
        el('div', { className: 'stats' }, [
          ui.statTile({
            value: util.formatNumber(rows.length),
            label: 'Suites',
            hint: 'Folded by path across the loaded runs'
          }),
          ui.statTile({
            value: util.formatNumber(totalTests),
            label: 'Results in them',
            hint: 'One per test per run, retries included'
          }),
          ui.statTile({
            value: util.formatNumber(failingSuites),
            label: 'Suites with a failure',
            tone: failingSuites ? 'failed' : 'passed',
            hint: failingSuites
              ? 'At least one failed, errored or unexpectedly passed test'
              : 'Nothing failed in any suite'
          }),
          ui.statTile({
            value: util.formatDuration(rows.reduce(function (sum, row) {
              return sum + row.duration_ms;
            }, 0)),
            label: 'Time in these suites',
            hint: 'Summed across every attempt'
          })
        ])
      ];

      nodes.push(suitesCard(rows, visible));
      if (openPath) {
        var open = null;
        for (i = 0; i < rows.length; i++) {
          if (rows[i].path === openPath) { open = rows[i]; }
        }
        // The selection survives a live update only while the suite does. A
        // suite that is no longer in the loaded runs closes rather than
        // rendering the last thing we happened to know about it.
        if (open) { nodes.push(testsCard(open)); } else { openPath = null; }
      }

      util.replace(content, el('div', { className: 'stack' }, nodes));
    }

    function suitesCard(all, rows) {
      var largest = 0;
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].total > largest) { largest = rows[i].total; }
      }

      var finder = ui.searchField({
        id: 'th-suite-search',
        key: 'suite',
        label: 'Find a suite',
        placeholder: 'Path or name',
        value: search,
        onChange: function (_key, value) {
          search = value;
          repaint();
        }
      });

      var body = rows.length
        ? ui.table({
          caption: 'Suites across the recent finished runs',
          sort: sort,
          onSort: function (key, dir) { sort = { key: key, dir: dir }; repaint(); },
          onRowClick: function (row) {
            openPath = openPath === row.path ? null : row.path;
            repaint();
          },
          columns: [
            {
              key: 'name', label: 'Suite', sortable: true, defaultDir: 'asc',
              render: function (row) {
                // A button, not a link: the panel it opens is on this page, and
                // a hash that only this view understands is not a URL worth
                // handing to somebody else.
                return el('div', {}, [
                  el('div', { className: 'cell-main' }, el('button', {
                    className: 'linkbtn',
                    attrs: {
                      type: 'button',
                      'aria-expanded': String(openPath === row.path)
                    },
                    text: row.name,
                    on: {
                      click: function () {
                        openPath = openPath === row.path ? null : row.path;
                        repaint();
                      }
                    }
                  })),
                  row.path === row.name
                    ? null
                    : el('div', { className: 'cell-sub truncate mono', text: row.path })
                ]);
              }
            },
            {
              key: 'runs', label: 'Runs', align: 'right', sortable: true,
              render: function (row) { return util.formatNumber(row.runs); }
            },
            {
              key: 'total', label: 'Results', align: 'right', sortable: true,
              render: function (row) {
                return el('div', { className: 'row row--tight cell-metric' }, [
                  ui.meter({
                    value: row.total, max: largest, status: 'total',
                    ariaLabel: row.total + ' results'
                  }),
                  el('span', { className: 'num', text: util.formatNumber(row.total) })
                ]);
              }
            },
            {
              key: 'pass_rate', label: 'Pass rate', align: 'right', sortable: true,
              render: function (row) {
                return row.pass_rate === null
                  ? el('span', { className: 'subtle', text: 'nothing ran' })
                  : util.formatPercent(row.pass_rate, 1);
              }
            },
            {
              key: 'failing', label: 'Failing', align: 'right', sortable: true,
              render: function (row) {
                return row.failing
                  ? el('span', {
                    className: 'statuspill', dataset: { status: 'failed' },
                    text: util.formatNumber(row.failing)
                  })
                  : el('span', { className: 'subtle', text: '0' });
              }
            },
            {
              key: 'flaky', label: 'Flaky', align: 'right', sortable: true,
              render: function (row) {
                return row.flaky
                  ? el('span', {
                    className: 'statuspill', dataset: { status: 'xpassed' },
                    text: util.formatNumber(row.flaky)
                  })
                  : el('span', { className: 'subtle', text: '0' });
              }
            },
            {
              key: 'duration_ms', label: 'Time', align: 'right', sortable: true,
              render: function (row) { return util.formatDuration(row.duration_ms); }
            }
          ],
          rows: rows
        })
        : ui.emptyState({
          icon: 'search',
          title: 'No suite matches “' + search + '”',
          hint: util.formatNumber(all.length) + ' ' + util.pluralize(all.length, 'suite') +
            ' in these runs. Clear the box to see them all.'
        });

      return ui.card({
        title: 'Suites',
        icon: 'briefcase',
        flush: true,
        hint: util.formatNumber(rows.length) + ' of ' + util.formatNumber(all.length) + ' shown',
        body: [
          el('div', { className: 'card__toolbar' }, [
            el('div', { className: 'filters__row' }, [finder])
          ]),
          body
        ]
      });
    }

    /** The tests of one suite, worst first — the reason anybody opened it. */
    function testsCard(row) {
      var entries = row.tests.slice().sort(function (a, b) {
        var left = util.isFailureish(a.test.status) ? 0 : 1;
        var right = util.isFailureish(b.test.status) ? 0 : 1;
        if (left !== right) { return left - right; }
        return testDuration(b.test) - testDuration(a.test);
      });
      var shown = entries.slice(0, TEST_LIMIT);

      return ui.card({
        title: row.name,
        icon: 'list',
        flush: true,
        hint: util.formatNumber(entries.length) + ' ' +
          util.pluralize(entries.length, 'result') + ' in ' +
          util.formatNumber(row.runs) + ' ' + util.pluralize(row.runs, 'run') +
          (entries.length > shown.length
            ? ' — showing the first ' + util.formatNumber(shown.length)
            : ''),
        actions: [
          el('button', {
            className: 'btn btn--sm',
            attrs: { type: 'button' },
            text: 'Close',
            on: { click: function () { openPath = null; repaint(); } }
          })
        ],
        body: ui.table({
          caption: 'Tests in ' + row.name,
          columns: [
            {
              key: 'name', label: 'Test',
              render: function (entry) {
                return ui.link(
                  util.buildHash(['tests', entry.test.id], {}),
                  entry.test.full_name || entry.test.name,
                  'cell-main'
                );
              }
            },
            {
              key: 'status', label: 'Status',
              render: function (entry) { return ui.statusPill(entry.test.status); }
            },
            {
              key: 'attempts', label: 'Attempts', align: 'right',
              render: function (entry) {
                var count = (entry.test.attempts || []).length;
                return count > 1
                  ? el('span', {
                    className: 'statuspill', dataset: { status: 'xpassed' },
                    text: util.formatNumber(count)
                  })
                  : el('span', { className: 'subtle', text: String(count) });
              }
            },
            {
              key: 'duration', label: 'Time', align: 'right',
              render: function (entry) { return util.formatDuration(testDuration(entry.test)); }
            },
            {
              key: 'run', label: 'Run',
              render: function (entry) {
                return ui.link(
                  util.buildHash(['runs', entry.detail.run.id], {}),
                  entry.detail.run.name || util.shortId(entry.detail.run.id)
                );
              }
            }
          ],
          rows: shown
        })
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

  TestHub.views.suites = { create: create };
})(window);
