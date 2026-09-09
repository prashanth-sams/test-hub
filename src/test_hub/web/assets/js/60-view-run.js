/* Test Hub — one run, in full.
 *
 * The engineering altitude. Everything a person needs when a pipeline just went
 * red: what changed since the last comparable run, which suites are involved,
 * which tests failed, and — one click later — why.
 *
 * The comparison panel is the part that earns its place. "47 failures" means
 * nothing on its own; "3 newly failing, 44 still failing, 2 newly passing"
 * means somebody broke three things and the other 44 were already broken. The
 * server computes the comparison against the previous *comparable* run (same
 * project, team, component, type, environment and branch) — this view only has
 * to resolve ids to names and stay out of the way.
 *
 * The test table keeps its search, status filter and sort in view-local state
 * rather than the URL: they are a way of reading one page, not a place anyone
 * links to. The run itself, and every test on it, are linkable.
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
    var local = { search: '', status: '', sort: 'status', dir: 'desc', expanded: {}, runId: null };

    var content = el('div', { className: 'stack' });
    var root = el('div', {}, content);

    /* ---------------------------------------------------- tests section */

    var searchInput = el('input', {
      className: 'input',
      attrs: {
        type: 'search', id: 'run-test-search', placeholder: 'Filter tests by name, class or file…',
        'aria-label': 'Filter tests', autocomplete: 'off', spellcheck: 'false'
      }
    });
    var statusSelect = el('select', {
      className: 'select',
      attrs: { id: 'run-test-status', 'aria-label': 'Filter tests by status' }
    });
    var tableHolder = el('div', {});
    var testsCount = el('span', { className: 'card__hint' });

    var applySearch = util.debounce(function () {
      local.search = searchInput.value;
      renderTests();
    }, 200, 800);
    searchInput.addEventListener('input', applySearch);
    statusSelect.addEventListener('change', function () {
      local.status = statusSelect.value;
      renderTests();
    });

    var testsSection = ui.card({
      title: 'Tests',
      actions: [testsCount],
      flush: true,
      body: [
        el('div', { className: 'card__toolbar' }, [
          el('div', { className: 'filters__row' }, [
            el('div', { className: 'field field--grow' }, [
              el('label', { className: 'field__label', attrs: { for: 'run-test-search' }, text: 'Filter' }),
              searchInput
            ]),
            el('div', { className: 'field' }, [
              el('label', { className: 'field__label', attrs: { for: 'run-test-status' }, text: 'Status' }),
              statusSelect
            ])
          ])
        ]),
        tableHolder
      ]
    });

    /* ------------------------------------------------------------ render */

    function chart(spec) {
      var holder = el('div', { className: 'chart' });
      pending.push(function () { charts.draw(holder, spec); });
      return holder;
    }

    function render(state) {
      pending = [];
      var focused = document.activeElement === searchInput;
      var caret = focused ? searchInput.selectionStart : null;

      if (state.errors.run) {
        util.replace(content, [
          backLink(),
          ui.card({
            title: 'Run',
            body: ui.errorState({
              title: 'That run did not load',
              error: state.errors.run,
              onRetry: function () { store.retry(); }
            })
          })
        ]);
        return;
      }
      if (!state.run) {
        util.replace(content, [
          backLink(),
          ui.card({ title: 'Run', body: ui.loadingBlock('Loading run', 'row', 6) })
        ]);
        return;
      }

      var detail = state.run;
      var run = detail.run;
      if (local.runId !== String(run.id)) {
        local.runId = String(run.id);
        local.expanded = {};
        local.search = '';
        local.status = '';
        searchInput.value = '';
      }

      // Always rendered, comparison or not. "There is no comparable run yet" is
      // an answer; an absent panel is the reader wondering whether the feature
      // is broken, and the first run of any new branch would silently lose it.
      var nodes = [backLink(), header(run), summaryRow(detail), comparisonCard(detail)];
      if ((run.warnings || []).length) { nodes.push(warningsCard(run.warnings)); }
      nodes.push(suitesCard(detail));
      nodes.push(testsSection);
      if ((detail.artifacts || []).length) { nodes.push(artifactsCard(detail.artifacts)); }

      util.replace(content, nodes);
      syncStatusOptions(detail);
      renderTests();
      for (var i = 0; i < pending.length; i++) { pending[i](); }
      pending = [];

      if (focused) {
        searchInput.focus();
        if (caret !== null && searchInput.setSelectionRange) {
          try {
            searchInput.setSelectionRange(caret, caret);
          } catch (err) {
            util.reportError('could not restore the caret', err);
          }
        }
      }
    }

    function backLink() {
      return el('div', { className: 'crumbs' }, [
        ui.link(util.buildHash(['overview'], {}), 'Overview'),
        el('span', { className: 'crumbs__sep', text: '/' }),
        ui.link(util.buildHash(['runs'], {}), 'Runs')
      ]);
    }

    function header(run) {
      var context = run.context || {};
      return el('div', { className: 'page-head' }, [
        el('div', { className: 'page-head__titles' }, [
          el('h1', {}, [
            document.createTextNode(run.name || run.project_key || 'Run'),
            ui.statusPill(run.status),
            run.is_live ? ui.liveBadge() : null
          ]),
          el('div', { className: 'page-head__sub' }, [
            el('span', { text: run.project_key }),
            run.team ? el('span', { className: 'tag', text: run.team }) : null,
            run.component ? el('span', { className: 'tag', text: run.component }) : null,
            el('span', { className: 'tag', text: util.titleCase(run.test_type) }),
            el('span', { text: 'started ' + util.timeAgo(run.started_at) }),
            el('span', { className: 'subtle mono', text: util.shortId(run.id) })
          ])
        ]),
        el('div', { className: 'page-head__actions' }, [
          context.pipeline_url ? ui.externalLink(context.pipeline_url, 'Pipeline') : null,
          el('button', {
            className: 'btn btn--sm', attrs: { type: 'button' },
            on: { click: function () { store.retry(); } }
          }, [ui.icon('refresh', 14), document.createTextNode('Refresh')])
        ])
      ]);
    }

    function summaryRow(detail) {
      var run = detail.run;
      var counts = TestHub.provider.normalizeCounts(run.counts);
      var context = run.context || {};
      var segments = [];
      for (var i = 0; i < util.STATUS_ORDER.length; i++) {
        var status = util.STATUS_ORDER[i];
        if (!counts[status]) { continue; }
        segments.push({
          key: status, label: util.statusLabel(status),
          value: counts[status], color: '--status-' + status
        });
      }

      return el('div', { className: 'grid grid--sidebar' }, [
        ui.card({
          title: 'Run context',
          body: ui.metaGrid([
            ['Environment', text(context.environment)],
            ['Branch', text(context.branch)],
            ['Commit', context.commit_sha
              ? el('span', { className: 'mono', text: util.shortId(context.commit_sha) })
              : text(null)],
            ['Trigger', text(util.titleCase(run.trigger))],
            ['Framework', text([context.framework, context.framework_version].filter(Boolean).join(' ') || null)],
            ['Language', text([context.language, context.language_version].filter(Boolean).join(' ') || null)],
            ['CI', text(context.ci_provider)],
            ['Host', text(context.hostname)],
            ['Started', text(util.formatDateTime(run.started_at))],
            ['Finished', text(util.formatDateTime(run.finished_at))],
            ['Duration', text(util.formatDuration(run.duration_ms))],
            ['Adapter', text(run.source_adapter)]
          ])
        }),
        ui.card({
          title: 'Outcome',
          body: el('div', { className: 'stack--sm' }, [
            chart({
              kind: 'donut',
              title: 'Outcomes in this run',
              description: ui.countsSentence(counts),
              segments: segments,
              size: 150,
              centerValue: util.formatPercent(counts.pass_rate, 1),
              centerLabel: 'pass rate'
            }),
            ui.countBar(counts),
            el('div', { className: 'legend' }, segments.map(function (segment) {
              return el('span', { className: 'legend__item' }, [
                el('span', { className: 'legend__swatch', dataset: { status: segment.key } }),
                el('span', { text: segment.label + ' · ' + util.formatNumber(segment.value) })
              ]);
            })),
            counts.flaky || counts.retried
              ? el('div', { className: 'kpi__meta', text:
                util.formatNumber(counts.flaky) + ' flaky · ' +
                util.formatNumber(counts.retried) + ' retried' })
              : null
          ])
        })
      ]);
    }

    function text(value) {
      return value === null || value === undefined || value === ''
        ? el('span', { className: 'subtle', text: util.DASH })
        : el('span', { text: String(value) });
    }

    /* -------------------------------------------------------- comparison */

    function comparisonCard(detail) {
      var comparison = detail.comparison || {};
      var names = {};
      var tests = detail.tests || [];
      for (var i = 0; i < tests.length; i++) { names[String(tests[i].id)] = tests[i]; }

      if (!comparison.baseline_run_id) {
        return ui.card({
          title: 'Compared with the previous run',
          body: ui.emptyState({
            title: 'No comparable run yet',
            hint: 'A comparison needs an earlier run with the same project, team, component, ' +
              'test type, environment and branch.'
          })
        });
      }

      var lists = [
        ['Newly failing', comparison.newly_failing, 'failed'],
        ['Still failing', comparison.still_failing, 'error'],
        ['Newly passing', comparison.newly_passing, 'passed'],
        ['Added', comparison.added, 'queued'],
        ['Removed', comparison.removed, 'skipped']
      ];

      return ui.card({
        title: 'Compared with the previous run',
        hint: 'baseline started ' + util.timeAgo(comparison.baseline_started_at),
        actions: [ui.link(util.buildHash(['runs', comparison.baseline_run_id]), 'Open baseline', 'btn btn--sm')],
        body: el('div', { className: 'stack' }, [
          el('div', { className: 'compare' }, [
            compareItem('Pass rate', comparison.pass_rate_delta, 'pct', true),
            compareItem('Duration', comparison.duration_delta_ms, 'ms', false),
            compareItem('Tests', comparison.total_delta, 'int', true)
          ]),
          el('div', { className: 'stack--sm' }, lists.map(function (entry) {
            return diffList(entry[0], entry[1] || [], entry[2], names);
          }))
        ])
      });
    }

    function compareItem(label, value, unit, betterIsUp) {
      return el('div', { className: 'compare__item' }, [
        el('div', { className: 'compare__value' },
          ui.deltaText(value, { unit: unit, betterIsUp: betterIsUp })),
        el('div', { className: 'compare__label', text: label })
      ]);
    }

    function diffList(label, ids, status, names) {
      if (!ids.length) { return null; }
      var list = el('ul', { className: 'artifact-list', attrs: { hidden: 'hidden' } },
        ids.slice(0, 200).map(function (id) {
          var test = names[String(id)];
          return el('li', {}, [
            ui.statusPill(test ? test.status : status),
            test
              ? ui.link(util.buildHash(['tests', id]), test.name)
              : ui.link(util.buildHash(['tests', id]), util.shortId(id)),
            test ? el('span', { className: 'subtle truncate', text: test.suite_path || '' }) : null
          ]);
        }));
      list.hidden = true;
      var toggle = el('button', {
        className: 'tree__row',
        attrs: { type: 'button', 'aria-expanded': 'false' }
      }, [
        el('span', { className: 'tree__chevron' }, ui.icon('chevron', 14)),
        el('span', { className: 'tree__name', text: label }),
        el('span', { className: 'statuspill', dataset: { status: status },
          text: util.formatNumber(ids.length) })
      ]);
      toggle.addEventListener('click', function () {
        var open = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', String(!open));
        list.hidden = open;
      });
      return el('div', { className: 'tree__item' }, [toggle, list]);
    }

    /* ---------------------------------------------------------- warnings */

    function warningsCard(warnings) {
      return ui.card({
        title: 'Parser warnings',
        hint: util.formatNumber(warnings.length) + ' ' + util.pluralize(warnings.length, 'warning'),
        flush: true,
        body: ui.table({
          caption: 'Warnings raised while importing this run',
          cards: false,
          columns: [
            {
              key: 'severity', label: 'Severity',
              render: function (row) {
                return ui.statusPill(row.severity === 'info' ? 'queued' : 'skipped',
                  { label: util.titleCase(row.severity) });
              }
            },
            { key: 'code', label: 'Code', render: function (row) {
              return el('span', { className: 'mono', text: row.code });
            } },
            { key: 'message', label: 'Message' },
            { key: 'location', label: 'Where', render: function (row) {
              return el('span', { className: 'mono subtle', text: row.location || util.DASH });
            } }
          ],
          rows: warnings
        })
      });
    }

    /* ------------------------------------------------------------- tree */

    function buildTree(detail) {
      var suites = detail.suites || [];
      var tests = detail.tests || [];
      var bySuite = {};
      var i;
      for (i = 0; i < tests.length; i++) {
        var key = String(tests[i].suite_id);
        (bySuite[key] = bySuite[key] || []).push(tests[i]);
      }

      if (!suites.length) {
        // Some adapters report no suites at all; group by the path each test
        // carries so the tree is never empty when tests exist.
        var byPath = {};
        for (i = 0; i < tests.length; i++) {
          var path = tests[i].suite_path || '(no suite)';
          (byPath[path] = byPath[path] || []).push(tests[i]);
        }
        return Object.keys(byPath).sort().map(function (path) {
          return {
            suite: { id: 'path:' + path, name: path, path: path },
            tests: byPath[path],
            children: []
          };
        });
      }

      var nodes = {};
      for (i = 0; i < suites.length; i++) {
        nodes[String(suites[i].id)] = {
          suite: suites[i],
          tests: bySuite[String(suites[i].id)] || [],
          children: []
        };
      }
      var roots = [];
      for (i = 0; i < suites.length; i++) {
        var node = nodes[String(suites[i].id)];
        var parent = suites[i].parent_id ? nodes[String(suites[i].parent_id)] : null;
        if (parent) { parent.children.push(node); } else { roots.push(node); }
      }
      return roots;
    }

    function suiteCounts(node) {
      var counts = TestHub.provider.normalizeCounts(node.suite.counts);
      if (counts.total) { return counts; }
      var derived = TestHub.provider.emptyCounts();
      collect(node, derived);
      return TestHub.provider.finalizeCounts(derived);
    }

    function collect(node, counts) {
      for (var i = 0; i < node.tests.length; i++) {
        TestHub.provider.addStatus(counts, node.tests[i].status, 1);
      }
      for (var c = 0; c < node.children.length; c++) { collect(node.children[c], counts); }
    }

    function suitesCard(detail) {
      var roots = buildTree(detail);
      if (!roots.length) {
        return ui.card({
          title: 'Suites',
          body: ui.emptyState({
            title: 'No suites reported',
            hint: 'This run reported tests without any suite grouping, or reported nothing at all.'
          })
        });
      }
      var list = el('ul', { className: 'tree' });
      for (var i = 0; i < roots.length; i++) {
        list.appendChild(suiteNode(roots[i], 0));
      }
      return ui.card({
        title: 'Suites',
        hint: util.formatNumber(countSuites(roots)) + ' in this run',
        flush: true,
        body: list
      });
    }

    function countSuites(nodes) {
      var total = 0;
      for (var i = 0; i < nodes.length; i++) {
        total += 1 + countSuites(nodes[i].children);
      }
      return total;
    }

    function suiteNode(node, depth) {
      var key = String(node.suite.id);
      var counts = suiteCounts(node);
      var expanded = local.expanded[key] === true;

      var row = el('button', {
        className: 'tree__row',
        attrs: { type: 'button', 'aria-expanded': String(expanded) }
      }, [
        el('span', { className: 'tree__chevron' }, ui.icon('chevron', 14)),
        el('span', { className: 'tree__name' }, [
          el('span', { text: node.suite.name || node.suite.path }),
          node.suite.path && node.suite.path !== node.suite.name
            ? el('div', { className: 'tree__path truncate', text: node.suite.path })
            : null
        ]),
        el('span', { className: 'tree__meta' }, [
          ui.countBar(counts),
          el('span', { className: 'num subtle nowrap',
            text: util.formatNumber(counts.total) + ' ' + util.pluralize(counts.total, 'test') }),
          el('span', { className: 'num nowrap', text: util.formatPercent(counts.pass_rate, 0) }),
          el('span', { className: 'num subtle nowrap',
            text: util.formatDuration(node.suite.duration_ms) })
        ])
      ]);
      row.style.paddingLeft = (16 + depth * 18) + 'px';

      var children = el('ul', { className: 'tree__children' });
      var built = false;

      function build() {
        if (built) { return; }
        built = true;
        for (var i = 0; i < node.children.length; i++) {
          children.appendChild(suiteNode(node.children[i], depth + 1));
        }
        var tests = util.sortBy(node.tests, 'name', 'asc');
        for (var t = 0; t < tests.length; t++) {
          children.appendChild(el('li', { className: 'tree__leaf' }, [
            ui.statusPill(tests[t].status),
            ui.link(util.buildHash(['tests', tests[t].id]), tests[t].name),
            tests[t].params ? el('span', { className: 'mono subtle truncate', text: tests[t].params }) : null,
            el('span', { className: 'filters__spacer' }),
            (tests[t].attempts || []).length > 1
              ? el('span', { className: 'tag',
                text: (tests[t].attempts || []).length + ' attempts' })
              : null,
            tests[t].flaky ? el('span', { className: 'statuspill', dataset: { status: 'xpassed' }, text: 'flaky' }) : null,
            el('span', { className: 'num subtle nowrap', text: util.formatDuration(tests[t].duration_ms) })
          ]));
        }
      }

      if (expanded) { build(); }
      children.hidden = !expanded;

      row.addEventListener('click', function () {
        var open = row.getAttribute('aria-expanded') === 'true';
        local.expanded[key] = !open;
        row.setAttribute('aria-expanded', String(!open));
        if (!open) { build(); }
        children.hidden = open;
      });

      return el('li', { className: 'tree__item' }, [row, children]);
    }

    /* ------------------------------------------------------- test table */

    function syncStatusOptions(detail) {
      var present = {};
      var tests = detail.tests || [];
      for (var i = 0; i < tests.length; i++) { present[tests[i].status] = true; }
      util.clear(statusSelect);
      statusSelect.appendChild(el('option', { attrs: { value: '' }, text: 'Any status' }));
      for (var s = 0; s < util.STATUS_ORDER.length; s++) {
        var status = util.STATUS_ORDER[s];
        if (!present[status]) { continue; }
        var option = el('option', { attrs: { value: status }, text: util.statusLabel(status) });
        if (local.status === status) { option.selected = true; }
        statusSelect.appendChild(option);
      }
    }

    function renderTests() {
      var state = store.state;
      if (!state.run) { return; }
      var tests = (state.run.tests || []).slice();
      var needle = local.search.trim().toLowerCase();

      var filtered = tests.filter(function (test) {
        if (local.status && test.status !== local.status) { return false; }
        if (!needle) { return true; }
        var haystack = [test.name, test.class_name, test.suite_path, test.file, test.params,
          test.full_name, (test.tags || []).join(' ')].join(' ').toLowerCase();
        return haystack.indexOf(needle) >= 0;
      });

      var sorted = util.sortBy(filtered, sortKey(local.sort), local.dir);
      util.replace(testsCount, document.createTextNode(
        util.formatNumber(sorted.length) + ' of ' + util.formatNumber(tests.length) + ' shown'));

      if (!sorted.length) {
        util.replace(tableHolder, ui.emptyState({
          title: tests.length ? 'No test matches this filter' : 'This run reported no tests',
          hint: tests.length
            ? 'Clear the search box or pick a different status.'
            : 'A run can exist before any result arrives — that is what a queued run looks like.'
        }));
        return;
      }

      util.replace(tableHolder, ui.table({
        caption: 'Tests in this run',
        sort: { key: local.sort, dir: local.dir },
        onSort: function (key, dir) {
          local.sort = key;
          local.dir = dir;
          renderTests();
        },
        columns: [
          {
            key: 'status', label: 'Status', sortable: true,
            render: function (row) {
              return el('div', { className: 'row row--tight' }, [
                ui.statusPill(row.status),
                row.flaky ? el('span', { className: 'statuspill', dataset: { status: 'xpassed' }, text: 'flaky' }) : null
              ]);
            }
          },
          {
            key: 'name', label: 'Test', sortable: true,
            render: function (row) {
              return el('div', {}, [
                el('div', { className: 'cell-main' },
                  ui.link(util.buildHash(['tests', row.id]), row.name)),
                el('div', { className: 'cell-sub truncate mono',
                  text: [row.class_name, row.suite_path].filter(Boolean).join(' · ') })
              ]);
            }
          },
          {
            key: 'params', label: 'Parameters',
            render: function (row) {
              return row.params
                ? el('span', { className: 'mono truncate', text: row.params })
                : el('span', { className: 'subtle', text: util.DASH });
            }
          },
          {
            key: 'attempts', label: 'Attempts', align: 'right', sortable: true,
            render: function (row) { return String((row.attempts || []).length); }
          },
          {
            key: 'duration_ms', label: 'Duration', align: 'right', sortable: true,
            render: function (row) {
              return el('span', { className: 'num', text: util.formatDuration(row.duration_ms) });
            }
          }
        ],
        rows: sorted,
        onRowClick: function (row) { util.navigate(util.buildHash(['tests', row.id])); }
      }));
    }

    function sortKey(name) {
      if (name === 'attempts') {
        return function (row) { return (row.attempts || []).length; };
      }
      if (name === 'status') {
        return function (row) { return util.STATUS_ORDER.indexOf(row.status); };
      }
      return name;
    }

    /* --------------------------------------------------------- artifacts */

    function artifactsCard(artifacts) {
      return ui.card({
        title: 'Artifacts',
        hint: util.formatNumber(artifacts.length) + ' ' + util.pluralize(artifacts.length, 'file'),
        flush: true,
        body: el('ul', { className: 'artifact-list' }, artifacts.map(function (artifact) {
          return el('li', {}, [
            el('span', { className: 'tag', text: artifact.kind }),
            artifact.url
              ? el('a', { attrs: { href: artifact.url, rel: 'noreferrer noopener' }, text: artifact.name })
              : el('span', { className: 'cell-main', text: artifact.name }),
            el('span', { className: 'subtle mono truncate', text: artifact.content_type }),
            el('span', { className: 'filters__spacer' }),
            el('span', { className: 'num subtle nowrap', text: formatBytes(artifact.size_bytes) })
          ]);
        }))
      });
    }

    function formatBytes(bytes) {
      if (typeof bytes !== 'number') { return util.DASH; }
      if (bytes < 1024) { return bytes + ' B'; }
      if (bytes < 1048576) { return (bytes / 1024).toFixed(1) + ' KB'; }
      if (bytes < 1073741824) { return (bytes / 1048576).toFixed(1) + ' MB'; }
      return (bytes / 1073741824).toFixed(2) + ' GB';
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

  TestHub.views.run = { create: create };
})(window, document);
