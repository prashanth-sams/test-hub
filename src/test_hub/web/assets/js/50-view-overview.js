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
 *
 * The two rows under the KPIs are "what is on fire right now". The timeline is
 * every run as it happens — a bar that pops up at the right-hand edge when a
 * run starts, grows while its results arrive, and slides left with the clock.
 * The status row is the same question asked project by project: each one's
 * newest run as one bar, wherever it ran, CI or a laptop. They are the parts
 * of this page that are never rebuilt. Everything else is re-rendered from
 * state on every update; these two are created once and patched, so their
 * bars move into their new places instead of blinking every second of a live
 * run, and the link under a keyboard user's focus stays put.
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

  //: The seven dimensions the portfolio slices by, shared with the analytics
  //: page — see `ui.portfolioFields`.
  var FIELDS = ui.portfolioFields();

  //: How a run's trigger reads under its project's name. Anything not listed
  //: is title-cased as sent.
  var TRIGGER_LABELS = {
    push: 'Push', pull_request: 'Pull request', schedule: 'Schedule', retry: 'Retry',
    manual: 'Manual', local: 'Local', api: 'API'
  };

  /** Where a run came from and when: the line under each project's name. */
  function runMeta(run) {
    var parts = [];
    // The trigger is what tells a CI run from one started on a laptop. It is
    // shown as the publisher reported it rather than sorted into "CI" and
    // "not CI" here — that line would be drawn in the wrong place for somebody.
    if (run.trigger && run.trigger !== 'unknown') {
      parts.push(TRIGGER_LABELS[run.trigger] || util.titleCase(run.trigger));
    }
    if (run.status === 'queued') {
      parts.push('queued');
    } else if (run.is_live) {
      parts.push('started ' + util.timeAgo(run.started_at));
    } else {
      parts.push(util.timeAgo(run.finished_at || run.started_at));
    }
    return parts.join(' · ');
  }

  /** Which run a bar is, for the readout: name, type, branch, environment. */
  function runDetail(run) {
    var parts = [];
    if (run.name) { parts.push(run.name); }
    if (run.test_type && run.test_type !== 'other') { parts.push(run.test_type); }
    if (run.branch) { parts.push(run.branch); }
    if (run.environment) { parts.push(run.environment); }
    return parts.join(' · ');
  }

  /**
   * The status row: every project's newest run in the window, one bar each.
   *
   * Created once per mount and updated in place — see the header. Its rows
   * are `ProjectHealth.last_run`, so a run that is still going *is* its
   * project's status until it finishes, and a project with nothing inside the
   * window has no bar rather than an empty one: the hint counts them instead.
   * The filters reach it like everything else on the page, which is how it
   * answers "and only the e2e runs" or "only staging".
   */
  function statusRow() {
    var chart = ui.statusBars({
      label: 'Latest run of each project',
      emptyTitle: 'No runs in this window',
      emptyHint: 'Widen the window or clear a filter. A project appears here as soon as a run ' +
        'reports — from CI or from anywhere else — and its bar moves while the results arrive.'
    });
    var badge = el('span');
    var hint = el('span', { className: 'card__hint' });
    var card = ui.card({
      title: 'Test status by project',
      badge: badge,
      actions: [hint],
      body: chart.el
    });
    var wasLive = false;

    function update(overview) {
      var projects = overview.projects || [];
      var entries = [];
      var live = false;
      for (var i = 0; i < projects.length; i++) {
        var run = projects[i].last_run;
        if (!run) { continue; }
        live = live || !!run.is_live;
        entries.push({
          key: projects[i].project_key,
          title: projects[i].name || projects[i].project_key,
          href: util.buildHash(['runs', run.id]),
          meta: runMeta(run),
          detail: runDetail(run),
          live: !!run.is_live,
          counts: run.counts
        });
      }
      // By name, never by what the bars say: rows that re-sort whenever a
      // live result lands are rows nobody can keep an eye on.
      entries.sort(function (a, b) {
        return a.title.localeCompare(b.title, undefined, { sensitivity: 'base' }) ||
          (a.key < b.key ? -1 : (a.key > b.key ? 1 : 0));
      });
      chart.update(entries);

      // Replaced only when it changes: a new badge restarts its pulse.
      if (live !== wasLive) {
        util.replace(badge, live ? ui.liveBadge() : null);
        wasLive = live;
      }
      var idle = projects.length - entries.length;
      var text = 'Newest run per project' + (idle > 0
        ? ' · ' + util.formatNumber(idle) + ' without a run ' + windows.during(windows.of(overview))
        : '');
      if (hint.textContent !== text) { hint.textContent = text; }
    }

    return { el: card, update: update };
  }

  /**
   * The timeline row: every run in the span as a bar on a moving clock — see
   * 42-timeline.js. Like the status row it is created once and patched, and
   * unlike it it is redrawn on its own store section, so a live event moves
   * its bars without the rest of the page re-rendering around it.
   */
  function timelineRow(store) {
    var spans = store.TIMELINE_SPANS;
    var chart = ui.timeline({ label: 'Test runs over time, across every project' });
    var control = ui.segmented({
      label: 'Timeline span',
      bare: true,
      options: Object.keys(spans).map(function (key) { return { value: key, label: key }; }),
      value: store.DEFAULT_TIMELINE_SPAN,
      onChange: function (value) {
        util.navigate(store.hashWith({
          span: value === store.DEFAULT_TIMELINE_SPAN ? null : value
        }));
      }
    });
    var badge = el('span');
    var hint = el('span', { className: 'card__hint' });
    var card = ui.card({
      title: 'Test execution timeline',
      badge: badge,
      actions: [hint, control.el],
      body: chart.el
    });
    var wasLive = false;

    function update(state) {
      var span = store.timelineSpan();
      var timeline = state.timeline;
      var runs = timeline ? timeline.runs : [];
      control.sync(span.key);
      chart.update({ runs: runs, span: span, error: state.errors.timeline });

      var cutoff = Date.now() - span.ms;
      var count = 0;
      var live = false;
      for (var i = 0; i < runs.length; i++) {
        var started = util.toDate(runs[i].started_at);
        if (!started || started.getTime() < cutoff) { continue; }
        count += 1;
        live = live || !!runs[i].is_live;
      }
      if (live !== wasLive) {
        util.replace(badge, live ? ui.liveBadge() : null);
        wasLive = live;
      }
      var text = util.formatNumber(count) + ' ' + util.pluralize(count, 'run') +
        ' in the last ' + span.label +
        (timeline && timeline.truncated ? ' — the newest ' + util.formatNumber(runs.length) : '');
      if (hint.textContent !== text) { hint.textContent = text; }
    }

    return { el: card, update: update, destroy: chart.destroy };
  }

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

    // Slots rather than one container: the timeline and the status row sit
    // between two stretches of the page that are rebuilt on every update, and
    // they must not be detached and re-inserted with them — that alone drops
    // focus and restarts every transition in them. Plain divs, so `hidden`
    // really hides them, and a hidden slot leaves no gap in the stack.
    var liveTimeline = timelineRow(store);
    var projectStatus = statusRow();
    var head = el('div');
    var timelineSlot = el('div', {}, liveTimeline.el);
    var statusSlot = el('div', {}, projectStatus.el);
    var rest = el('div');
    // Until the first overview lands there is nothing true to say in any.
    timelineSlot.hidden = true;
    statusSlot.hidden = true;
    rest.hidden = true;
    var content = el('div', { className: 'stack' }, [head, timelineSlot, statusSlot, rest]);
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

    /** Everything but the head slot goes, for the states with no data in them. */
    function showOnly(node) {
      util.replace(head, node);
      timelineSlot.hidden = true;
      statusSlot.hidden = true;
      util.clear(rest);
      rest.hidden = true;
    }

    function render(state) {
      pending = [];
      var overview = state.overview;

      if (state.errors.overview) {
        showOnly(ui.card({
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
        showOnly(el('div', { className: 'stack' }, [
          el('div', { className: 'kpis' }, [0, 1, 2, 3, 4, 5].map(function () {
            return el('div', { className: 'kpi' }, ui.skeleton('card', 1));
          })),
          ui.card({ title: 'Pass rate trend', body: ui.loadingBlock('Loading the overview', 'chart', 1) })
        ]));
        return;
      }

      var counts = overview.counts;
      var lead = [];

      if (overview.derived) {
        lead.push(ui.banner({
          tone: null,
          title: 'Recomputed in your browser',
          text: overview.derived_note ||
            'These totals were recalculated in the browser from the runs that match the filters.'
        }));
      }
      lead.push(kpiRow(counts, overview));

      util.replace(head, el('div', { className: 'stack' }, lead));
      // Shown before it is drawn: the chart measures its own width, and a
      // hidden slot has none.
      timelineSlot.hidden = false;
      liveTimeline.update(state);
      projectStatus.update(overview);
      statusSlot.hidden = false;
      util.replace(rest, el('div', { className: 'stack' }, [
        headlineRow(overview),
        trendRow(overview),
        liveRow(overview),
        projectsCard(overview),
        testListsRow(overview)
      ]));
      rest.hidden = false;
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
      var win = windows.of(overview);
      var step = windows.bucketWords(overview.trend_bucket_seconds);
      var pills = [
        pill('clock', util.formatNumber(overview.total_runs) + ' ' +
          util.pluralize(overview.total_runs, 'run') + ' ' + windows.briefly(win)),
        pill('grid', util.formatNumber((overview.projects || []).length) + ' ' +
          util.pluralize((overview.projects || []).length, 'project')),
        // Not the pass rate: the donut is already saying that, two inches to
        // the right and in larger type.
        pill(failing ? 'alert' : 'check', util.formatNumber(failing) + ' failed or errored')
      ];
      if (live.length) {
        pills.push(el('span', {
          className: 'hero__pill hero__pill--live',
          text: util.formatNumber(live.length) + ' in flight'
        }));
      }

      return el('div', { className: 'grid grid--2' }, [
        ui.card({
          title: 'Health',
          hint: windows.title(win),
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
          title: 'Volume ' + step.by,
          hint: util.formatNumber(overview.total_runs) + ' runs',
          body: chart({
            kind: 'stackedBar',
            title: 'Tests executed ' + step.per,
            description: 'Failed and errored tests stacked against everything else, ' +
              step.per + '.',
            height: 196,
            buckets: (overview.trend || []).map(function (point) {
              return {
                label: windows.bucketLabel(point.bucket, overview.trend_bucket_seconds),
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
      var seconds = overview.trend_bucket_seconds;
      var step = windows.bucketWords(seconds);
      var per = step.per.charAt(0).toUpperCase() + step.per.slice(1);
      var trend = overview.trend || [];

      function counted(point) {
        return [
          { value: util.formatNumber(point.total || 0), label: util.pluralize(point.total || 0, 'test') },
          { value: util.formatNumber(point.failed || 0), label: 'failed or errored' },
          { value: util.formatNumber(point.runs || 0), label: util.pluralize(point.runs || 0, 'run') }
        ];
      }

      var rates = trend.map(function (point) {
        return {
          label: windows.bucketLabel(point.bucket, seconds),
          value: typeof point.pass_rate === 'number' ? point.pass_rate : null,
          rows: counted(point),
          cells: [
            util.formatNumber(point.total || 0),
            util.formatNumber(point.failed || 0),
            util.formatNumber(point.runs || 0)
          ]
        };
      });
      var durations = trend.map(function (point) {
        return {
          label: windows.bucketLabel(point.bucket, seconds),
          low: typeof point.duration_ms_p50 === 'number' ? point.duration_ms_p50 : null,
          high: typeof point.duration_ms_p95 === 'number' ? point.duration_ms_p95 : null,
          rows: [{
            value: util.formatNumber(point.runs || 0),
            label: util.pluralize(point.runs || 0, 'timed run')
          }]
        };
      });

      // The failure rate is not drawn beside the pass rate: over executed
      // tests the two always sum to 100, so a second series would only be the
      // first one upside down. The tooltip and the table carry the counts.
      return el('div', { className: 'grid grid--2' }, [
        ui.card({
          title: 'Pass rate trend',
          hint: per + ' \u00b7 dashed line is the whole window',
          body: chart({
            kind: 'rateColumns',
            title: 'Pass rate ' + step.by,
            description: 'Pass rate ' + step.per + ' across the selected window, against the ' +
              'window\'s overall pass rate of ' + util.formatPercent(overview.counts.pass_rate, 1) + '.',
            buckets: rates,
            reference: overview.counts.pass_rate,
            referenceLabel: 'overall',
            valueLabel: 'pass rate',
            extraHeaders: ['Tests', 'Failed or errored', 'Runs'],
            height: 220,
            emptyMessage: 'No completed runs in this window'
          })
        }),
        ui.card({
          title: 'Run duration',
          hint: 'Wall clock, ' + step.per,
          body: [
            chart({
              kind: 'rangeBars',
              title: 'Run duration ' + step.by,
              description: 'Median and 95th percentile wall-clock run duration ' + step.per +
                ': the dot is the median, the bar runs from the median to the 95th percentile.',
              buckets: durations,
              height: 196,
              emptyMessage: 'No timed runs in this window'
            }),
            el('div', { className: 'legend' }, [
              legendItem('median', 'Median (p50)'),
              legendItem('spread', 'p50 to p95')
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
        if (section === 'timeline') {
          // Its own section, drawn on its own: a live event moves the
          // timeline's bars and nothing else on the page should flicker for it.
          if (state.overview && !state.errors.overview) { liveTimeline.update(state); }
          return;
        }
        render(state);
      },
      destroy: function () {
        // The timeline's clock is an interval and two window listeners; none
        // of them leaves with the element.
        liveTimeline.destroy();
        charts.forget(content);
      }
    };
  }

  TestHub.views.overview = { create: create };
})(window);
