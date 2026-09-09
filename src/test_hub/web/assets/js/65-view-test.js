/* Test Hub — one test identity, across every run that executed it.
 *
 * The identity is stable by construction (test_hub.domain.identity), which is
 * the only reason this page can exist: a test that gets a new id every run has
 * no history, cannot be called flaky, and cannot be compared with itself.
 *
 * Attempts are shown as attempts. A retry is a separate execution with its own
 * output, not a correction of the one before it, so each gets its own panel and
 * the failure that was later "fixed" by a rerun is still on the page. That is
 * the whole argument for the event model in ADR-0001 D2, made visible.
 *
 * Every stack trace, log line and stdout block on this page arrives as a text
 * node inside a <pre>. There is no formatting pass, no ANSI translation and no
 * markup: the bytes the framework produced are the bytes you read.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var charts = TestHub.charts;
  var el = util.el;

  TestHub.views = TestHub.views || {};

  function create(ctx) {
    var store = ctx.store;
    var pending = [];
    var expanded = {};
    var currentId = null;

    var content = el('div', { className: 'stack' });
    var root = el('div', {}, content);

    function chart(spec) {
      var holder = el('div', { className: spec.kind === 'sparkline' ? 'sparkline' : 'chart' });
      pending.push(function () { charts.draw(holder, spec); });
      return holder;
    }

    function render(state) {
      pending = [];
      if (state.errors.test) {
        util.replace(content, [crumbs(null), ui.card({
          title: 'Test',
          body: ui.errorState({
            title: 'That test did not load',
            error: state.errors.test,
            onRetry: function () { store.retry(); }
          })
        })]);
        return;
      }
      if (!state.test) {
        util.replace(content, [crumbs(null),
          ui.card({ title: 'Test', body: ui.loadingBlock('Loading test', 'row', 5) })]);
        return;
      }

      var detail = state.test;
      if (currentId !== String(detail.test_id)) {
        currentId = String(detail.test_id);
        expanded = {};
      }

      var nodes = [crumbs(detail), header(detail), statsRow(detail)];
      if (detail.partial) {
        nodes.push(ui.banner({
          tone: 'warn',
          title: 'Partial history',
          text: 'This snapshot does not include an indexed history for this test, so only the ' +
            'run it was found in is shown.'
        }));
      }
      nodes.push(latestCard(detail));
      nodes.push(historyCard(detail));

      util.replace(content, nodes);
      for (var i = 0; i < pending.length; i++) { pending[i](); }
      pending = [];
    }

    function crumbs(detail) {
      var items = [
        ui.link(util.buildHash(['overview'], {}), 'Overview'),
        el('span', { className: 'crumbs__sep', text: '/' }),
        ui.link(util.buildHash(['runs'], {}), 'Runs')
      ];
      if (detail && detail.latest && detail.latest.run_id) {
        items.push(el('span', { className: 'crumbs__sep', text: '/' }));
        items.push(ui.link(util.buildHash(['runs', detail.latest.run_id]), 'This run'));
      }
      return el('div', { className: 'crumbs' }, items);
    }

    function header(detail) {
      var latest = detail.latest;
      return el('div', { className: 'page-head' }, [
        el('div', { className: 'page-head__titles' }, [
          el('h1', {}, [
            document.createTextNode(detail.name),
            latest ? ui.statusPill(latest.status) : null,
            latest && latest.flaky
              ? el('span', { className: 'statuspill', dataset: { status: 'xpassed' }, text: 'flaky' })
              : null
          ]),
          el('div', { className: 'page-head__sub' }, [
            el('span', { className: 'mono', text: detail.suite_path || '' }),
            detail.class_name ? el('span', { className: 'mono subtle', text: detail.class_name }) : null,
            detail.params ? el('span', { className: 'tag mono', text: detail.params }) : null,
            detail.owner ? el('span', { text: 'owner: ' + detail.owner }) : null,
            (detail.tags || []).length ? ui.tagList(detail.tags) : null
          ])
        ]),
        el('div', { className: 'page-head__actions' }, [
          el('span', { className: 'subtle mono', text: util.shortId(detail.test_id) })
        ])
      ]);
    }

    function statsRow(detail) {
      var history = detail.history || [];
      var durations = history.map(function (point) { return point.duration_ms; })
        .filter(function (value) { return typeof value === 'number'; });

      return el('div', { className: 'kpis' }, [
        ui.kpi({
          label: 'Flake rate',
          status: detail.flake_rate ? 'skipped' : null,
          value: util.formatPercent(detail.flake_rate, 1),
          meta: [detail.flake_rate === null || detail.flake_rate === undefined
            ? 'needs two or more runs'
            : 'of runs disagreed with themselves']
        }),
        ui.kpi({
          label: 'Failure rate',
          status: detail.failure_rate ? 'failed' : 'passed',
          value: util.formatPercent(detail.failure_rate, 1),
          meta: [util.formatNumber(history.length) + ' ' + util.pluralize(history.length, 'run') + ' in history']
        }),
        ui.kpi({
          label: 'Average duration',
          value: util.formatDuration(detail.average_duration_ms),
          meta: durations.length > 1
            ? [chart({
              kind: 'sparkline',
              values: durations.slice().reverse(),
              ariaLabel: 'Duration across the last ' + durations.length + ' runs',
              title: 'Duration trend'
            })]
            : ['not enough history']
        }),
        ui.kpi({
          label: 'Attempts (latest run)',
          value: detail.latest ? String((detail.latest.attempts || []).length) : util.DASH,
          meta: [detail.latest && detail.latest.flaky ? 'flaky within the run' : 'no retries needed']
        })
      ]);
    }

    /* --------------------------------------------------------- attempts */

    function latestCard(detail) {
      var latest = detail.latest;
      if (!latest) {
        return ui.card({
          title: 'Latest result',
          body: ui.emptyState({
            title: 'No result recorded yet',
            hint: 'The test identity is known, but no attempt has been reported for it.'
          })
        });
      }
      var attempts = (latest.attempts || []).slice();
      if (!attempts.length) {
        return ui.card({
          title: 'Latest result',
          body: ui.emptyState({ title: 'No attempts recorded', hint: 'The run reported this test without an outcome.' })
        });
      }

      // Newest first: the last attempt is what the run counted.
      attempts.reverse();

      // Open the attempt that explains the result, not simply the newest one.
      // On a flaky test the newest attempt is the retry that passed, and
      // opening it shows "no output was captured" while the traceback that
      // made the test interesting sits collapsed underneath — which is the one
      // thing this page exists to prevent.
      var openIndex = 0;
      for (var k = 0; k < attempts.length; k++) {
        if (hasSomethingToRead(attempts[k])) { openIndex = k; break; }
      }

      var nodes = [];
      for (var i = 0; i < attempts.length; i++) {
        nodes.push(attemptPanel(attempts[i], attempts.length - i, i === openIndex));
      }
      return ui.card({
        title: 'Latest result',
        hint: util.formatNumber(attempts.length) + ' ' + util.pluralize(attempts.length, 'attempt'),
        actions: latest.run_id
          ? [ui.link(util.buildHash(['runs', latest.run_id]), 'Open run', 'btn btn--sm')]
          : null,
        body: el('div', {}, nodes)
      });
    }

    function hasSomethingToRead(attempt) {
      return !!(attempt.failure || attempt.stdout || attempt.stderr ||
        attempt.logs || attempt.skip_reason);
    }

    function attemptPanel(attempt, number, defaultOpen) {
      var key = String(attempt.id || number);
      var isOpen = expanded[key] === undefined ? defaultOpen : expanded[key];

      var body = el('div', { className: 'attempt__body' });
      var head = el('button', {
        className: 'attempt__head',
        attrs: { type: 'button', 'aria-expanded': String(isOpen) }
      }, [
        el('span', { className: 'tree__chevron' }, ui.icon('chevron', 14)),
        el('span', { className: 'attempt__n', text: 'Attempt ' + (attempt.attempt_number || number) }),
        ui.statusPill(attempt.status),
        attempt.original_status && attempt.original_status !== attempt.status
          ? el('span', { className: 'tag', text: 'reported as ' + attempt.original_status })
          : null,
        el('span', { className: 'attempt__spacer' }),
        attempt.worker ? el('span', { className: 'tag', text: attempt.worker }) : null,
        el('span', { className: 'num subtle nowrap', text: util.formatDuration(attempt.duration_ms) })
      ]);

      var built = false;
      function build() {
        if (built) { return; }
        built = true;
        util.appendChild(body, attemptBody(attempt));
      }
      if (isOpen) { build(); }
      body.hidden = !isOpen;

      head.addEventListener('click', function () {
        var open = head.getAttribute('aria-expanded') === 'true';
        expanded[key] = !open;
        head.setAttribute('aria-expanded', String(!open));
        if (!open) { build(); }
        body.hidden = open;
      });

      return el('div', { className: 'attempt' }, [head, body]);
    }

    function attemptBody(attempt) {
      var nodes = [];
      nodes.push(ui.metaGrid([
        ['Started', el('span', { text: util.formatDateTime(attempt.started_at) })],
        ['Finished', el('span', { text: util.formatDateTime(attempt.finished_at) })],
        ['Duration', el('span', { className: 'num', text: util.formatDuration(attempt.duration_ms) })],
        ['Worker', el('span', { text: attempt.worker || util.DASH })]
      ]));

      if (attempt.skip_reason) {
        nodes.push(ui.banner({ tone: 'warn', title: 'Skipped', text: attempt.skip_reason }));
      }

      var failure = attempt.failure;
      if (failure) {
        var failureNodes = [];
        if (failure.type || failure.message) {
          failureNodes.push(el('div', { className: 'stack--sm' }, [
            failure.type ? el('div', { className: 'pre__label mono', text: failure.type }) : null,
            failure.message
              ? el('pre', { className: 'pre pre--wrap pre--fail', text: failure.message })
              : null
          ]));
        }
        if (failure.expected !== null && failure.expected !== undefined) {
          failureNodes.push(block('Expected', failure.expected));
        }
        if (failure.actual !== null && failure.actual !== undefined) {
          failureNodes.push(block('Actual', failure.actual));
        }
        if (failure.stack_trace) {
          failureNodes.push(block('Stack trace', failure.stack_trace, true));
        }
        if (failure.truncated) {
          failureNodes.push(el('p', { className: 'subtle',
            text: 'This failure text was truncated when it was stored.' }));
        }
        nodes.push(el('div', { className: 'stack--sm' }, failureNodes));
      }

      if (attempt.stdout) { nodes.push(block('stdout', attempt.stdout, true)); }
      if (attempt.stderr) { nodes.push(block('stderr', attempt.stderr, true)); }
      if (attempt.logs) { nodes.push(block('Logs', attempt.logs, true)); }

      if ((attempt.artifact_ids || []).length) {
        nodes.push(el('p', { className: 'subtle',
          text: util.formatNumber(attempt.artifact_ids.length) + ' ' +
            util.pluralize(attempt.artifact_ids.length, 'artifact') + ' attached — see the run page.' }));
      }

      if (!failure && !attempt.stdout && !attempt.stderr && !attempt.logs && !attempt.skip_reason) {
        nodes.push(el('p', { className: 'subtle', text: 'No output was captured for this attempt.' }));
      }
      return nodes;
    }

    function block(label, value, mono) {
      return el('div', {}, [
        el('div', { className: 'metagrid__label', text: label }),
        el('pre', {
          className: 'pre' + (mono ? '' : ' pre--wrap'),
          text: String(value)
        })
      ]);
    }

    /* ---------------------------------------------------------- history */

    function historyCard(detail) {
      var history = (detail.history || []).slice();
      if (!history.length) {
        return ui.card({
          title: 'History',
          body: ui.emptyState({
            title: 'No history yet',
            hint: 'History appears once this test identity has been seen in more than one run.'
          })
        });
      }

      var ordered = util.sortBy(history, 'started_at', 'desc');
      var strip = el('div', { className: 'history-strip' }, ordered.slice(0, 60).map(function (point) {
        return el('a', {
          dataset: { status: point.status },
          attrs: {
            href: util.buildHash(['runs', point.run_id]),
            title: util.statusLabel(point.status) + ' · ' + util.formatDateTime(point.started_at) +
              ' · ' + util.formatDuration(point.duration_ms),
            'aria-label': util.statusLabel(point.status) + ' on ' + util.formatDateTime(point.started_at)
          }
        });
      }));

      return ui.card({
        title: 'History',
        hint: util.formatNumber(history.length) + ' ' + util.pluralize(history.length, 'run'),
        flush: true,
        body: [
          el('div', { className: 'card__body' }, [
            strip,
            el('p', { className: 'kpi__meta', text: 'Newest on the left. Each square links to its run.' })
          ]),
          ui.table({
            caption: 'This test across recent runs',
            columns: [
              {
                key: 'status', label: 'Status',
                render: function (row) { return ui.statusPill(row.status); }
              },
              {
                key: 'started_at', label: 'Run',
                render: function (row) {
                  return el('div', {}, [
                    el('div', { className: 'cell-main' },
                      ui.link(util.buildHash(['runs', row.run_id]), util.formatDateTime(row.started_at))),
                    el('div', { className: 'cell-sub truncate',
                      text: [row.branch, row.environment].filter(Boolean).join(' · ') })
                  ]);
                }
              },
              {
                key: 'attempts', label: 'Attempts', align: 'right',
                render: function (row) { return String(row.attempts || 1); }
              },
              {
                key: 'duration_ms', label: 'Duration', align: 'right',
                render: function (row) {
                  return el('span', { className: 'num', text: util.formatDuration(row.duration_ms) });
                }
              }
            ],
            rows: ordered,
            onRowClick: function (row) { util.navigate(util.buildHash(['runs', row.run_id])); }
          })
        ]
      });
    }

    return {
      el: root,
      update: function (state, section) {
        if (section === 'status' || section === 'facets') { return; }
        render(state);
      },
      destroy: function () { return undefined; }
    };
  }

  TestHub.views.test = { create: create };
})(window, document);
