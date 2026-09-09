/* Test Hub — SnapshotDataProvider: the whole product, from one frozen object.
 *
 * The exported HTML file carries a `Snapshot` (see test_hub.domain.models) on
 * `window.TEST_HUB_SNAPSHOT`. There is no server, no network and no more data
 * coming, so every question is answered out of memory and `status` is
 * permanently 'static'.
 *
 * The interesting decision is what happens when the user filters. Two options
 * existed: grey the filters out in snapshot mode, or recompute. Greying them
 * out makes the offline file a second-class dashboard and silently changes the
 * product depending on how you opened it. So we recompute — with the formulas
 * transcribed from test_hub.domain.metrics into 10-provider.js.
 *
 * With no filters we hand back the server-computed overview verbatim instead of
 * our own recomputation. Not because we distrust the arithmetic, but because
 * the exporter had access to the full history the snapshot may have truncated
 * (flake rates over a longer window than the runs it shipped), and the honest
 * default is the number the exporter wrote down.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var provider = TestHub.provider;

  function notFound(kind, id) {
    var error = new Error(kind + ' ' + id + ' is not in this snapshot.');
    error.code = 'not_found';
    error.hint = 'The export includes a subset of runs. Open the live dashboard for the rest.';
    return error;
  }

  function copyOverview(overview) {
    var copy = {};
    var keys = Object.keys(overview || {});
    for (var i = 0; i < keys.length; i++) { copy[keys[i]] = overview[keys[i]]; }
    copy.counts = provider.normalizeCounts(overview ? overview.counts : null);
    copy.live_runs = (copy.live_runs || []).map(provider.normalizeSummary);
    copy.recent_runs = (copy.recent_runs || []).map(provider.normalizeSummary);
    copy.trend = copy.trend || [];
    copy.projects = copy.projects || [];
    copy.top_failing_tests = copy.top_failing_tests || [];
    copy.flakiest_tests = copy.flakiest_tests || [];
    copy.filters = copy.filters || {};
    return copy;
  }

  /** Build a TestDetail-shaped object for a test that history never indexed. */
  function detailFromCase(testCase, run) {
    return {
      schema_version: 1,
      test_id: testCase.id,
      project_key: run ? run.project_key : null,
      name: testCase.name,
      class_name: testCase.class_name,
      suite_path: testCase.suite_path,
      params: testCase.params,
      full_name: testCase.full_name,
      owner: testCase.owner,
      tags: testCase.tags || [],
      latest: testCase,
      history: run ? [{
        run_id: run.id,
        started_at: run.started_at,
        status: testCase.status,
        duration_ms: testCase.duration_ms,
        attempts: (testCase.attempts || []).length || 1,
        branch: (run.context || {}).branch,
        environment: (run.context || {}).environment
      }] : [],
      flake_rate: null,
      failure_rate: null,
      average_duration_ms: testCase.duration_ms,
      partial: true
    };
  }

  function create(snapshot) {
    var runsById = {};
    var testsById = {};
    var caseIndex = {};
    var summaries = [];
    var i;
    var j;

    var runs = (snapshot && snapshot.runs) || [];
    for (i = 0; i < runs.length; i++) {
      var detail = runs[i];
      if (!detail || !detail.run) { continue; }
      runsById[String(detail.run.id)] = detail;
      summaries.push(provider.summarize(detail));
      var cases = detail.tests || [];
      for (j = 0; j < cases.length; j++) {
        // First run wins as the "representative" case only if nothing newer
        // arrives; runs are re-sorted below, so index the newest afterwards.
        var key = String(cases[j].id);
        var existing = caseIndex[key];
        if (!existing || compareStart(detail.run.started_at, existing.run.started_at) > 0) {
          caseIndex[key] = { test: cases[j], run: detail.run };
        }
      }
    }

    var testDetails = (snapshot && snapshot.tests) || [];
    for (i = 0; i < testDetails.length; i++) {
      testsById[String(testDetails[i].test_id)] = testDetails[i];
    }

    summaries = util.sortBy(summaries, 'started_at', 'desc');

    var capabilities = {
      live: false,
      artifacts: runs.some(function (detail) {
        return (detail.artifacts || []).length > 0;
      }),
      paging: true
    };

    var self = provider.createBase('snapshot', capabilities, 'static');

    self.meta = {
      title: (snapshot && snapshot.title) || 'Test Hub report',
      generated_at: snapshot && snapshot.generated_at,
      generator: (snapshot && snapshot.generator) || 'test-hub',
      generator_version: snapshot && snapshot.generator_version,
      workspace: snapshot && snapshot.workspace,
      schema_version: snapshot && snapshot.schema_version,
      run_count: summaries.length,
      test_count: testDetails.length
    };

    self.getOverview = function (filters) {
      var clean = provider.cleanFilters(filters);
      var base = snapshot && snapshot.overview;
      var windowMatches = !clean.window_days || !base ||
        Number(clean.window_days) === Number(base.window_days);
      if (base && windowMatches && !provider.hasFacetFilter(clean)) {
        return Promise.resolve(copyOverview(base));
      }
      return Promise.resolve(recompute(clean, base));
    };

    function recompute(clean, base) {
      var scoped = summaries.filter(function (summary) {
        return provider.matchesFilters(summary, clean);
      });
      var counts = provider.emptyCounts();
      var live = [];
      var k;
      for (k = 0; k < scoped.length; k++) {
        counts = provider.mergeCounts(counts, scoped[k].counts);
        if (scoped[k].is_live) { live.push(scoped[k]); }
      }

      var projectKeys = {};
      for (k = 0; k < scoped.length; k++) { projectKeys[scoped[k].project_key] = true; }
      var scopedTests = testDetails.filter(function (test) {
        return projectKeys[test.project_key] === true;
      });

      var windowDays = Number(clean.window_days) || (base && base.window_days) || 30;
      return {
        schema_version: (base && base.schema_version) || 1,
        generated_at: (base && base.generated_at) || (snapshot && snapshot.generated_at),
        window_days: windowDays,
        counts: counts,
        total_runs: scoped.length,
        live_runs: live,
        recent_runs: scoped.slice(0, 8),
        projects: provider.buildProjectHealth(scoped, (snapshot && snapshot.projects) || [], scopedTests),
        trend: provider.buildTrend(scoped, windowDays),
        total_runs_all_time: summaries.length,
        top_failing_tests: rank(scopedTests, 'failure_rate'),
        flakiest_tests: rank(scopedTests, 'flake_rate'),
        filters: provider.facetsFromSummaries(summaries),
        derived: true,
        test_lists: 'derived',
        derived_note: 'These filters are not the ones this file was exported with, so the ' +
          'numbers were recalculated from the ' + util.formatNumber(scoped.length) + ' matching ' +
          util.pluralize(scoped.length, 'run') + ' it contains.'
      };
    }

    function rank(tests, field) {
      return tests.filter(function (test) {
        return typeof test[field] === 'number' && test[field] > 0;
      }).sort(function (a, b) {
        return b[field] - a[field];
      }).slice(0, 8);
    }

    self.listRuns = function (filters) {
      var clean = provider.cleanFilters(filters);
      var matched = summaries.filter(function (summary) {
        return provider.matchesFilters(summary, clean);
      });
      if (clean.sort) {
        matched = util.sortBy(matched, sortKey(clean.sort), clean.dir === 'asc' ? 'asc' : 'desc');
      }
      var offset = Math.max(0, Number(clean.offset) || 0);
      var limit = Math.max(1, Number(clean.limit) || 25);
      return Promise.resolve({
        runs: matched.slice(offset, offset + limit),
        total: matched.length,
        offset: offset,
        limit: limit,
        // Everything is in memory here, so a sort is a sort of the whole result
        // set — not of one page. See the same field in 12-live-provider.js.
        sorted: 'all',
        exact: true
      });
    };

    function sortKey(name) {
      if (name === 'pass_rate') {
        return function (summary) { return summary.counts.pass_rate; };
      }
      if (name === 'total') {
        return function (summary) { return summary.counts.total; };
      }
      if (name === 'failed') {
        return function (summary) { return summary.counts.failed + summary.counts.error; };
      }
      return name;
    }

    self.getRun = function (runId) {
      var detail = runsById[String(runId)];
      if (!detail) { return Promise.reject(notFound('Run', runId)); }
      return Promise.resolve(detail);
    };

    self.getTest = function (testId) {
      var key = String(testId);
      var detail = testsById[key];
      if (detail) { return Promise.resolve(detail); }
      var indexed = caseIndex[key];
      if (indexed) { return Promise.resolve(detailFromCase(indexed.test, indexed.run)); }
      return Promise.reject(notFound('Test', testId));
    };

    self.getFacets = function () {
      var base = snapshot && snapshot.overview;
      var derived = provider.facetsFromSummaries(summaries);
      if (base && base.filters) {
        var keys = Object.keys(base.filters);
        for (var k = 0; k < keys.length; k++) {
          var values = base.filters[keys[k]];
          if (values && values.length) { derived[keys[k]] = values.slice(); }
        }
      }
      derived.projects = derived.projects && derived.projects.length
        ? derived.projects
        : ((snapshot && snapshot.projects) || []).map(function (project) { return project.key; });
      return Promise.resolve(derived);
    };

    // Nothing will ever arrive. Returning a working unsubscribe keeps every
    // caller identical between the two modes.
    self.subscribe = function () {
      return function () { return undefined; };
    };

    self.refresh = function () {
      return Promise.resolve(false);
    };

    return self;
  }

  function compareStart(a, b) {
    var left = util.toDate(a);
    var right = util.toDate(b);
    if (!left && !right) { return 0; }
    if (!left) { return -1; }
    if (!right) { return 1; }
    return left.getTime() - right.getTime();
  }

  TestHub.SnapshotDataProvider = {
    create: create,
    detect: function () {
      return window.TEST_HUB_SNAPSHOT || null;
    }
  };
})(window);
