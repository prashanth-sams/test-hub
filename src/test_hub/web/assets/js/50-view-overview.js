/* Test Hub — the executive overview.
 *
 * This is the altitude where nobody wants a stack trace. It answers four
 * questions and refuses to answer any others: how much testing happened, how
 * much of it passed, is it getting better or worse, and what is on fire right
 * now. Everything else is one click away in the drill-down views.
 *
 * The KPI row deliberately shows counts of *tests*, not of runs, and the pass
 * rate deliberately renders an em dash when nothing executed — a suite that
 * only skipped is not a 0% suite, and showing it in red trains people to
 * ignore red.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var charts = TestHub.charts;
  var el = util.el;

  TestHub.views = TestHub.views || {};

  //: The seven dimensions the portfolio slices by, shared with the analytics
  //: page — see `ui.portfolioFields`.
  var FIELDS = ui.portfolioFields();

  function create(ctx) {
    var store = ctx.store;
    var pending = [];

    var panel = ui.filterPanel({
      label: 'Portfolio filters',
      fields: FIELDS,
      values: store.state.filters,
      onChange: function (key, value) {
        util.navigate(store.hashWith(defineFilter(key, value)));
      },
      onReset: function () {
        util.navigate(util.buildHash(['overview'], {}));
      }
    });

    var content = el('div', { className: 'stack' });
    var root = el('div', {}, [
      el('div', { className: 'page-head' }, [
        el('div', { className: 'page-head__titles' }, [
          el('h1', { text: 'Portfolio overview' }),
          el('p', { className: 'page-head__sub', text: 'Every project, team and pipeline in one place.' })
        ])
      ]),
      panel.el,
      el('div', { className: 'section' }, content)
    ]);

    function defineFilter(key, value) {
      var patch = {};
      patch[key] = value;
      return patch;
    }

    function chart(spec, className) {
      var holder = el('div', { className: 'chart' + (className ? ' ' + className : '') });
      pending.push(function () { charts.draw(holder, spec); });
      return holder;
    }

    function render(state) {
      pending = [];
      var overview = state.overview;

      if (state.errors.overview) {
        util.replace(content, ui.card({
          title: 'Overview',
          body: ui.errorState({
            title: 'The overview did not load',
            error: state.errors.overview,
            onRetry: function () { store.retry(); }
          })
        }));
        return;
      }

      if (!overview) {
        util.replace(content, el('div', { className: 'stack' }, [
          el('div', { className: 'kpis' }, [0, 1, 2, 3, 4, 5].map(function () {
            return el('div', { className: 'kpi' }, ui.skeleton('card', 1));
          })),
          ui.card({ title: 'Pass rate trend', body: ui.loadingBlock('Loading the overview', 'chart', 1) })
        ]));
        return;
      }

      var counts = overview.counts;
      var nodes = [];

      if (overview.derived) {
        nodes.push(ui.banner({
          tone: null,
          title: 'Recomputed in your browser',
          text: overview.derived_note ||
            'These totals were recalculated in the browser from the runs that match the filters.'
        }));
      }

      nodes.push(kpiRow(counts, overview));
      nodes.push(headlineRow(overview));
      nodes.push(trendRow(overview));
      nodes.push(liveRow(overview));
      nodes.push(projectsCard(overview));
      nodes.push(testListsRow(overview));

      util.replace(content, el('div', { className: 'stack' }, nodes));
      for (var i = 0; i < pending.length; i++) { pending[i](); }
      pending = [];
    }

    function share(value, total) {
      if (!total) { return util.DASH; }
      return util.formatPercent((value / total) * 100, 1) + ' of tests';
    }

    function kpiRow(counts, overview) {
      var total = counts.total || 0;
      return el('div', { className: 'kpis' }, [
        ui.kpi({
          label: 'Tests', status: 'total', value: util.formatNumber(total),
          meta: [util.formatNumber(overview.total_runs) + ' ' + util.pluralize(overview.total_runs, 'run')]
        }),
        ui.kpi({
          label: 'Passed', status: 'passed', value: util.formatNumber(counts.passed),
          meta: [share(counts.passed, total)]
        }),
        ui.kpi({
          label: 'Failed', status: 'failed', value: util.formatNumber(counts.failed),
          meta: [share(counts.failed, total)]
        }),
        ui.kpi({
          label: 'Error', status: 'error', value: util.formatNumber(counts.error),
          meta: [share(counts.error, total)]
        }),
        ui.kpi({
          label: 'Skipped', status: 'skipped', value: util.formatNumber(counts.skipped),
          meta: [share(counts.skipped, total)]
        }),
        ui.kpi({
          label: 'Running', status: 'running', value: util.formatNumber(counts.running + counts.queued),
          meta: [counts.running ? ui.liveBadge('now') : el('span', { text: 'idle' })]
        })
      ]);
    }

    function headlineRow(overview) {
      var counts = overview.counts;
      var segments = [];
      for (var i = 0; i < util.STATUS_ORDER.length; i++) {
        var status = util.STATUS_ORDER[i];
        if (!counts[status]) { continue; }
        segments.push({
          key: status, label: util.statusLabel(status), value: counts[status],
          color: '--status-' + status
        });
      }

      var donut = chart({
        kind: 'donut',
        title: 'Test outcomes',
        description: 'Share of each outcome across ' + util.formatNumber(counts.total) + ' tests.',
        segments: segments,
        size: 148,
        centerValue: util.formatPercent(counts.pass_rate, 1),
        centerLabel: 'pass rate'
      });

      var legend = el('div', { className: 'legend' }, segments.map(function (segment) {
        return el('span', { className: 'legend__item' }, [
          el('span', { className: 'legend__swatch', dataset: { status: segment.key } }),
          el('span', { text: segment.label + ' · ' + util.formatNumber(segment.value) })
        ]);
      }));

      // The headline card, in the proportions the eye reads them: the size of
      // the thing first, its shape second, the breakdown third. The pass rate
      // stays in the middle of the donut rather than competing with the total
      // for the same corner — one number per level of detail.
      var live = overview.live_runs || [];
      var failing = (counts.failed || 0) + (counts.error || 0);
      var pills = [
        pill('clock', util.formatNumber(overview.total_runs) + ' ' +
          util.pluralize(overview.total_runs, 'run') + ' in ' + overview.window_days + ' days'),
        pill('grid', util.formatNumber((overview.projects || []).length) + ' ' +
          util.pluralize((overview.projects || []).length, 'project')),
        // Not the pass rate: the donut is already saying that, two inches to
        // the right and in larger type.
        pill(failing ? 'alert' : 'check', util.formatNumber(failing) + ' failed or errored')
      ];
      if (live.length) {
        pills.push(el('span', { className: 'hero__pill' }, [
          ui.liveBadge(),
          el('span', { text: util.formatNumber(live.length) + ' in flight' })
        ]));
      }

      return el('div', { className: 'grid grid--2' }, [
        ui.card({
          title: 'Health',
          hint: 'Last ' + overview.window_days + ' days',
          body: el('div', { className: 'stack--sm' }, [
            el('div', { className: 'hero' }, [
              el('div', { className: 'hero__figure' }, [
                el('div', { className: 'hero__value', text: util.formatNumber(counts.total) }),
                el('div', { className: 'hero__label', text: 'test results' }),
                el('div', { className: 'hero__meta' }, pills)
              ]),
              donut
            ]),
            ui.countBar(counts),
            ui.statStrip(counts, {
              extras: [
                { label: 'Flaky', value: counts.flaky, status: 'xpassed',
                  title: 'Tests that failed and then passed inside one run' },
                { label: 'Retried', value: counts.retried, status: 'running',
                  title: 'Tests that ran more than once' }
              ]
            })
          ])
        }),
        ui.card({
          title: 'Volume by day',
          hint: util.formatNumber(overview.total_runs) + ' runs',
          body: chart({
            kind: 'stackedBar',
            title: 'Tests executed per day',
            description: 'Failed and errored tests stacked against everything else, per day.',
            height: 196,
            buckets: (overview.trend || []).map(function (point) {
              return {
                label: util.formatDay(point.bucket),
                values: {
                  rest: Math.max(0, (point.total || 0) - (point.failed || 0)),
                  failed: point.failed || 0
                }
              };
            }),
            keys: [
              { key: 'rest', label: 'Passed, skipped or expected', color: '--status-passed' },
              { key: 'failed', label: 'Failed or errored', color: '--status-failed' }
            ],
            emptyMessage: 'No runs in this window'
          })
        })
      ]);
    }

    function trendRow(overview) {
      var points = (overview.trend || []).map(function (point) {
        return {
          label: util.formatDay(point.bucket),
          values: {
            pass_rate: typeof point.pass_rate === 'number' ? point.pass_rate : null,
            failure_rate: typeof point.failure_rate === 'number' ? point.failure_rate : null,
            p50: typeof point.duration_ms_p50 === 'number' ? point.duration_ms_p50 : null,
            p95: typeof point.duration_ms_p95 === 'number' ? point.duration_ms_p95 : null
          }
        };
      });

      return el('div', { className: 'grid grid--2' }, [
        ui.card({
          title: 'Pass rate trend',
          hint: 'Days with no runs are gaps, not zeroes',
          body: [
            chart({
              kind: 'trend',
              title: 'Pass rate by day',
              description: 'Pass rate and failure rate per day over the selected window.',
              points: points,
              yMin: 0,
              yMax: 100,
              formatY: function (value) { return Math.round(value) + '%'; },
              series: [
                {
                  key: 'pass_rate', label: 'Pass rate', color: '--status-passed', area: true,
                  format: function (value) { return util.formatPercent(value, 1); }
                },
                {
                  key: 'failure_rate', label: 'Failure rate', color: '--status-failed', dashed: true,
                  format: function (value) { return util.formatPercent(value, 1); }
                }
              ],
              emptyMessage: 'No completed runs in this window'
            }),
            el('div', { className: 'legend' }, [
              legendItem('passed', 'Pass rate'),
              legendItem('failed', 'Failure rate')
            ])
          ]
        }),
        ui.card({
          title: 'Run duration',
          hint: 'Wall clock, p50 and p95',
          body: [
            chart({
              kind: 'trend',
              title: 'Run duration by day',
              description: 'Median and 95th percentile wall-clock run duration per day.',
              points: points,
              yFloor: 0,
              formatY: function (value) { return util.formatDuration(value); },
              series: [
                {
                  key: 'p50', label: 'p50', color: '--chart-line', area: true,
                  format: util.formatDuration
                },
                {
                  key: 'p95', label: 'p95', color: '--chart-line-alt', dashed: true,
                  format: util.formatDuration
                }
              ],
              emptyMessage: 'No timed runs in this window'
            }),
            el('div', { className: 'legend' }, [
              legendItem('line', 'p50'),
              legendItem('line-alt', 'p95')
            ])
          ]
        })
      ]);
    }

    function pill(iconName, text) {
      return el('span', { className: 'hero__pill' }, [ui.icon(iconName, 13), el('span', { text: text })]);
    }

    function legendItem(status, label) {
      return el('span', { className: 'legend__item' }, [
        el('span', { className: 'legend__swatch', dataset: { status: status } }),
        el('span', { text: label })
      ]);
    }

    function liveRow(overview) {
      var live = overview.live_runs || [];
      var body;
      if (!live.length) {
        body = ui.emptyState({
          title: 'Nothing running',
          hint: 'Runs appear here the moment a publisher reports them, and update as results arrive.',
          icon: 'clock'
        });
      } else {
        body = el('div', { className: 'stack--sm' }, live.map(function (run) {
          var counts = run.counts;
          return el('div', { className: 'card' }, el('div', { className: 'card__body' }, [
            el('div', { className: 'row' }, [
              ui.liveBadge(),
              ui.link(util.buildHash(['runs', run.id]), run.name || run.project_key, 'cell-main'),
              el('span', { className: 'subtle', text: run.project_key }),
              run.branch ? el('span', { className: 'tag', text: run.branch }) : null,
              el('span', { className: 'filters__spacer' }),
              el('span', {
                className: 'subtle num',
                text: 'started ' + util.timeAgo(run.started_at)
              })
            ]),
            el('div', { className: 'row' }, [
              ui.countBar(counts),
              el('span', {
                className: 'subtle num nowrap',
                text: util.formatNumber(counts.completed) + ' of ' +
                  util.formatNumber(counts.total) + ' reported'
              })
            ])
          ]));
        }));
      }
      return ui.card({
        title: 'Active runs',
        hint: live.length ? util.formatNumber(live.length) + ' in flight' : null,
        badge: live.length ? ui.liveBadge() : null,
        body: body
      });
    }

    function projectsCard(overview) {
      var projects = overview.projects || [];
      if (!projects.length) {
        return ui.card({
          title: 'Project health',
          body: ui.emptyState({
            title: 'No projects in this window',
            hint: 'Widen the window or clear a filter to see project health.'
          })
        });
      }
      return ui.card({
        title: 'Project health',
        hint: util.formatNumber(projects.length) + ' ' + util.pluralize(projects.length, 'project'),
        flush: true,
        body: ui.table({
          caption: 'Project health over the selected window',
          columns: [
            {
              key: 'name', label: 'Project',
              render: function (row) {
                return el('div', {}, [
                  el('div', { className: 'cell-main' }, ui.link(
                    util.buildHash(['runs'], { project: row.project_key }), row.name || row.project_key)),
                  el('div', { className: 'cell-sub', text: row.team || 'no team' })
                ]);
              }
            },
            {
              key: 'last_status', label: 'Last run',
              render: function (row) {
                return el('div', { className: 'row row--tight' }, [
                  ui.statusPill(row.last_status),
                  row.live_runs ? ui.liveBadge() : null,
                  el('span', { className: 'subtle num', text: util.timeAgo(row.last_run_at) })
                ]);
              }
            },
            {
              key: 'pass_rate', label: 'Pass rate', align: 'right',
              render: function (row) {
                return el('div', { className: 'row row--tight' }, [
                  el('span', { className: 'num', text: util.formatPercent(row.pass_rate, 1) }),
                  row.pass_rate_delta === null || row.pass_rate_delta === undefined
                    ? null
                    : ui.deltaText(row.pass_rate_delta, { unit: 'pct' })
                ]);
              }
            },
            {
              key: 'runs_24h', label: 'Runs 24h', align: 'right',
              render: function (row) { return util.formatNumber(row.runs_24h); }
            },
            {
              key: 'flaky_tests', label: 'Flaky', align: 'right',
              render: function (row) {
                return row.flaky_tests
                  ? el('span', { className: 'statuspill', dataset: { status: 'xpassed' },
                    text: util.formatNumber(row.flaky_tests) })
                  : el('span', { className: 'subtle', text: '0' });
              }
            }
          ],
          rows: projects
        })
      });
    }

    function testListsRow(overview) {
      // "Empty" and "not computed for this slice" are different answers, and a
      // ranking that quietly shows nothing under a filter reads as good news.
      var unavailable = overview.test_lists === 'unavailable';
      var note = 'Per-test rankings need history the filtered view does not carry — ' +
        'clear the filters to see them.';
      return el('div', { className: 'grid grid--2' }, [
        testListCard('Most failing tests', unavailable ? [] : (overview.top_failing_tests || []),
          'failure_rate', unavailable ? note : 'Nothing has failed in this window.',
          unavailable ? 'Not available for this filter' : null),
        testListCard('Flakiest tests', unavailable ? [] : (overview.flakiest_tests || []),
          'flake_rate', unavailable ? note : 'No test disagreed with itself in this window.',
          unavailable ? 'Not available for this filter' : null)
      ]);
    }

    function testListCard(title, tests, field, emptyHint, emptyTitle) {
      if (!tests.length) {
        return ui.card({
          title: title,
          body: ui.emptyState({ title: emptyTitle || 'Nothing to report', hint: emptyHint })
        });
      }
      return ui.card({
        title: title,
        flush: true,
        body: ui.table({
          caption: title,
          cards: false,
          columns: [
            {
              key: 'name', label: 'Test',
              render: function (row) {
                return el('div', {}, [
                  el('div', { className: 'cell-main' },
                    ui.link(util.buildHash(['tests', row.test_id]), row.name)),
                  el('div', { className: 'cell-sub truncate', text: row.suite_path || '' })
                ]);
              }
            },
            {
              key: field, label: field === 'flake_rate' ? 'Flake rate' : 'Failure rate', align: 'right',
              render: function (row) {
                return el('span', {
                  className: 'statuspill',
                  dataset: { status: field === 'flake_rate' ? 'xpassed' : 'failed' },
                  text: util.formatPercent(row[field], 1)
                });
              }
            }
          ],
          rows: tests
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
      destroy: function () {
        charts.forget(content);
      }
    };
  }

  TestHub.views.overview = { create: create };
})(window);
