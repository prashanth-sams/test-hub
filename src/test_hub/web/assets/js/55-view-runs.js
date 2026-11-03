/* Test Hub — the run list: the drill-down's front door.
 *
 * Everything here is in the URL: filters, sort, page. That is not tidiness,
 * it is the feature — "the failing integration runs on main for payments" has
 * to be a link somebody can paste into an incident channel, and it has to
 * survive a reload and the back button.
 *
 * Sorting is the provider's concern, not this file's: it puts `sort` and `dir`
 * in the hash, and the provider honours them on the server where it can and in
 * the browser where a filter the API cannot express forced it to fetch a page
 * and refine it. The table says which of the two happened.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

  TestHub.views = TestHub.views || {};

  var RUN_STATUSES = ['queued', 'running', 'passed', 'failed', 'error', 'cancelled', 'unknown'];

  var FIELDS = [
    { key: 'q', label: 'Search', type: 'search', placeholder: 'Run, branch, commit, tag…' },
    { key: 'project', label: 'Project' },
    { key: 'team', label: 'Team' },
    { key: 'component', label: 'Component' },
    { key: 'test_type', label: 'Test type', facet: 'test_types' },
    { key: 'environment', label: 'Environment' },
    { key: 'branch', label: 'Branch', facet: 'branches' },
    {
      key: 'status', label: 'Status',
      options: [{ value: '', label: 'Any status' }].concat(RUN_STATUSES.map(function (status) {
        return { value: status, label: util.statusLabel(status) };
      }))
    }
  ];

  // Deliberately no time-window control here, unlike the overview. `GET /runs`
  // has no window parameter — it pages, newest first — so a window selector
  // would sit there doing nothing. The Started column and the pager already
  // answer "how far back am I".


  function create(ctx) {
    var store = ctx.store;

    var panel = ui.filterPanel({
      label: 'Run filters',
      fields: FIELDS,
      values: store.state.filters,
      onChange: function (key, value) {
        var patch = { page: null };
        patch[key] = value;
        util.navigate(store.hashWith(patch, { segments: ['runs'] }));
      },
      onReset: function () {
        util.navigate(util.buildHash(['runs'], {}));
      }
    });

    var content = el('div', { className: 'stack' });
    var root = el('div', {}, [
      el('div', { className: 'page-head' }, [
        el('div', { className: 'page-head__titles' }, [
          el('h1', { text: 'Runs' }),
          el('p', { className: 'page-head__sub', text: 'Every execution, newest first. Filters and sort live in the URL.' })
        ])
      ]),
      panel.el,
      el('div', { className: 'section' }, content)
    ]);

    function goPage(page) {
      util.navigate(store.hashWith({ page: page > 1 ? page : null }, { segments: ['runs'] }));
    }

    function onSort(key, dir) {
      util.navigate(store.hashWith({ sort: key, dir: dir, page: null }, { segments: ['runs'] }));
    }

    function render(state) {
      if (state.errors.runs) {
        util.replace(content, ui.card({
          title: 'Runs',
          body: ui.errorState({
            title: 'The run list did not load',
            error: state.errors.runs,
            onRetry: function () { store.retry(); }
          })
        }));
        return;
      }
      if (!state.runs) {
        util.replace(content, ui.card({
          title: 'Runs', flush: true,
          body: ui.loadingBlock('Loading runs', 'row', 8)
        }));
        return;
      }

      var page = state.runs;
      if (!page.runs.length) {
        util.replace(content, ui.card({
          title: 'Runs',
          body: ui.emptyState({
            title: 'No runs match these filters',
            hint: 'Every filter is combined with AND. Clearing the narrowest one usually helps.',
            action: el('button', {
              className: 'btn', attrs: { type: 'button' }, text: 'Clear filters',
              on: { click: function () { util.navigate(util.buildHash(['runs'], {})); } }
            })
          })
        }));
        return;
      }

      var table = ui.table({
        caption: 'Test runs',
        ariaLabel: 'Test runs',
        sort: { key: page.sort, dir: page.dir },
        onSort: onSort,
        onRowClick: function (row) { util.navigate(util.buildHash(['runs', row.id])); },
        columns: [
          {
            key: 'status', label: 'Status',
            render: function (row) {
              return el('div', { className: 'row row--tight' }, [
                ui.statusPill(row.status),
                row.is_live ? ui.liveBadge() : null
              ]);
            }
          },
          {
            key: 'name', label: 'Run',
            render: function (row) {
              var subtitle = [row.project_key, row.team, row.component]
                .filter(Boolean).join(' · ');
              return el('div', {}, [
                el('div', { className: 'cell-main' },
                  ui.link(util.buildHash(['runs', row.id]), row.name || row.project_key)),
                el('div', { className: 'cell-sub truncate', text: subtitle })
              ]);
            }
          },
          {
            key: 'branch', label: 'Branch',
            render: function (row) {
              if (!row.branch && !row.commit_sha) { return el('span', { className: 'subtle', text: util.DASH }); }
              return el('div', {}, [
                el('div', { className: 'truncate', text: row.branch || util.DASH }),
                row.commit_sha
                  ? el('div', { className: 'cell-sub mono', text: util.shortId(row.commit_sha) })
                  : null
              ]);
            }
          },
          {
            key: 'total', label: 'Tests', align: 'right', sortable: true,
            render: function (row) {
              return el('div', {}, [
                el('div', { className: 'num', text: util.formatNumber(row.counts.total) }),
                ui.countBar(row.counts)
              ]);
            }
          },
          {
            key: 'pass_rate', label: 'Pass rate', align: 'right', sortable: true,
            render: function (row) {
              return el('span', { className: 'num', text: util.formatPercent(row.counts.pass_rate, 1) });
            }
          },
          {
            key: 'failed', label: 'Failed', align: 'right', sortable: true,
            render: function (row) {
              var failed = row.counts.failed + row.counts.error;
              return failed
                ? el('span', { className: 'statuspill', dataset: { status: 'failed' },
                  text: util.formatNumber(failed) })
                : el('span', { className: 'subtle', text: '0' });
            }
          },
          {
            key: 'duration_ms', label: 'Duration', align: 'right', sortable: true,
            render: function (row) {
              return el('span', { className: 'num', text: util.formatDuration(row.duration_ms) });
            }
          },
          {
            key: 'started_at', label: 'Started', align: 'right', sortable: true,
            render: function (row) {
              return el('span', {
                className: 'num nowrap',
                attrs: { title: util.formatDateTime(row.started_at) },
                text: util.timeAgo(row.started_at)
              });
            }
          }
        ],
        rows: page.runs
      });

      var card = ui.card({
        title: 'Runs',
        hint: page.exact
          ? util.formatNumber(page.total) + ' matching'
          : 'narrowed in the browser',
        flush: true,
        body: [
          // Said out loud rather than implied by a sort arrow: a provider that
          // can only order the page it was given must not look like one that
          // ordered the whole result set.
          page.sorted === 'page' && page.total > page.runs.length
            ? el('p', { className: 'table-note', text:
              'Sorted within this page of ' + util.formatNumber(page.runs.length) +
              '. The server returns runs newest first; open a wider page size or ' +
              'narrow the filters to sort across all ' + util.formatNumber(page.total) + '.' })
            : null,
          table,
          ui.pager({
            page: page.page, limit: page.limit, total: page.total,
            exact: page.exact, shown: page.runs.length, onPage: goPage
          })
        ]
      });

      util.replace(content, card);
    }

    return {
      el: root,
      update: function (state, section) {
        panel.sync(state.filters, state.facets || {});
        if (section === 'facets' || section === 'status') { return; }
        render(state);
      },
      destroy: function () { return undefined; }
    };
  }

  TestHub.views.runs = { create: create };
})(window);
