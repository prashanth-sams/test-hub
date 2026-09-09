/* Test Hub — the data provider seam (ADR-0001 D1, narrowed by ADR-0003).
 *
 * The dashboard is written once and never learns where its data came from. It
 * asks a provider; a provider answers. There is one implementation today — the
 * live REST + SSE client in 12-* — and the seam stays because it is what keeps
 * `fetch` out of every view: a view that cannot reach the network cannot grow a
 * request in the middle of a render, and the arithmetic below has one home
 * rather than being redone per page.
 *
 * The interface, in full:
 *
 *   getOverview(filters)   -> Promise<OverviewSnapshot>
 *   listRuns(filters)      -> Promise<{runs: RunSummary[], total: number, ...}>
 *   getRun(runId)          -> Promise<RunDetail>
 *   getTest(testId)        -> Promise<TestDetail>
 *   getFacets()            -> Promise<{teams: [], components: [], ...}>
 *   subscribe(handler)     -> unsubscribe function
 *   capabilities           -> {live, artifacts, paging}
 *   status                 -> 'connecting'|'live'|'reconnecting'|'offline'
 *
 * `subscribe` delivers plain messages, never raw transport objects:
 *
 *   {type: 'status',  status: <status>, detail?: string}
 *   {type: 'change',  runIds: string[], reason: 'events'|'resync', at: ISO}
 *
 * This file also owns the arithmetic the provider needs. The server computes
 * every number it is asked for, but a filter the API cannot express is applied
 * in the browser, and the counts that come back then have to be recomputed —
 * and they have to agree, to the decimal, with test_hub.domain.metrics. Every
 * formula below is a transcription of that module and the docstrings say which
 * one, so a change there is a grep away from a change here.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;

  var STATUSES = ['connecting', 'live', 'reconnecting', 'offline'];

  //: Filter keys that map straight onto API query parameters.
  var FACET_KEYS = ['project', 'team', 'component', 'test_type', 'environment', 'branch'];
  //: Filter keys the dashboard applies itself when the server does not.
  var LOCAL_KEYS = ['status', 'q'];

  var COUNT_FIELDS = [
    'total', 'passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed',
    'cancelled', 'unknown', 'running', 'queued', 'flaky', 'retried'
  ];

  /* --------------------------------------------------------------- counts */

  function emptyCounts() {
    var counts = {};
    for (var i = 0; i < COUNT_FIELDS.length; i++) { counts[COUNT_FIELDS[i]] = 0; }
    return finalizeCounts(counts);
  }

  /** RunCounts.completed / executed / pass_rate / failure_rate, verbatim. */
  function finalizeCounts(counts) {
    var completed = counts.passed + counts.failed + counts.error + counts.skipped +
      counts.xfailed + counts.xpassed + counts.cancelled + counts.unknown;
    var executed = completed - counts.skipped - counts.cancelled;
    counts.completed = completed;
    counts.executed = executed;
    counts.pass_rate = executed > 0
      ? round2(100.0 * (counts.passed + counts.xfailed) / executed)
      : null;
    counts.failure_rate = executed > 0
      ? round2(100.0 * (counts.failed + counts.error + counts.xpassed) / executed)
      : null;
    return counts;
  }

  /**
   * Accept a counts block from either provider and guarantee the derived
   * fields exist. Pydantic emits them; a hand-written fixture might not, and a
   * missing `pass_rate` must not become 0% on the way through the UI.
   */
  function normalizeCounts(raw) {
    var counts = {};
    for (var i = 0; i < COUNT_FIELDS.length; i++) {
      var key = COUNT_FIELDS[i];
      var value = raw ? raw[key] : 0;
      counts[key] = typeof value === 'number' && isFinite(value) ? value : 0;
    }
    return finalizeCounts(counts);
  }

  function mergeCounts(a, b) {
    var counts = {};
    for (var i = 0; i < COUNT_FIELDS.length; i++) {
      var key = COUNT_FIELDS[i];
      counts[key] = (a[key] || 0) + (b[key] || 0);
    }
    return finalizeCounts(counts);
  }

  function addStatus(counts, status, amount) {
    var field = status;
    if (COUNT_FIELDS.indexOf(field) < 0) { field = 'unknown'; }
    counts[field] += (amount === undefined ? 1 : amount);
    counts.total += (amount === undefined ? 1 : amount);
    return counts;
  }

  function round2(value) { return Math.round(value * 100) / 100; }

  /** test_hub.domain.metrics.percentile — linear interpolation, no library. */
  function percentile(values, fraction) {
    if (!values || !values.length) { return null; }
    var ordered = values.slice().sort(function (a, b) { return a - b; });
    if (ordered.length === 1) { return ordered[0]; }
    var position = Math.max(0, Math.min(1, fraction)) * (ordered.length - 1);
    var lower = Math.floor(position);
    var upper = Math.min(lower + 1, ordered.length - 1);
    var weight = position - lower;
    return ordered[lower] * (1 - weight) + ordered[upper] * weight;
  }

  /* -------------------------------------------------------------- filters */

  function cleanFilters(filters) {
    var out = {};
    var source = filters || {};
    var keys = Object.keys(source);
    for (var i = 0; i < keys.length; i++) {
      var value = source[keys[i]];
      if (value === null || value === undefined || value === '') { continue; }
      out[keys[i]] = value;
    }
    return out;
  }

  function hasFacetFilter(filters) {
    var clean = cleanFilters(filters);
    for (var i = 0; i < FACET_KEYS.length; i++) {
      if (clean[FACET_KEYS[i]] !== undefined) { return true; }
    }
    for (var j = 0; j < LOCAL_KEYS.length; j++) {
      if (clean[LOCAL_KEYS[j]] !== undefined) { return true; }
    }
    return false;
  }

  function equalsInsensitive(a, b) {
    if (a === null || a === undefined) { return false; }
    return String(a).toLowerCase() === String(b).toLowerCase();
  }

  /** Does one RunSummary-shaped object satisfy the active filters? */
  function matchesFilters(summary, filters) {
    var clean = cleanFilters(filters);
    if (clean.project && !equalsInsensitive(summary.project_key, clean.project)) { return false; }
    if (clean.team && !equalsInsensitive(summary.team, clean.team)) { return false; }
    if (clean.component && !equalsInsensitive(summary.component, clean.component)) { return false; }
    if (clean.test_type && !equalsInsensitive(summary.test_type, clean.test_type)) { return false; }
    if (clean.environment && !equalsInsensitive(summary.environment, clean.environment)) { return false; }
    if (clean.branch && !equalsInsensitive(summary.branch, clean.branch)) { return false; }
    if (clean.status && !equalsInsensitive(summary.status, clean.status)) { return false; }
    if (clean.q) {
      var needle = String(clean.q).toLowerCase();
      var haystack = [
        summary.name, summary.project_key, summary.team, summary.component,
        summary.branch, summary.commit_sha, summary.environment, summary.framework,
        (summary.tags || []).join(' ')
      ].join(' ').toLowerCase();
      if (haystack.indexOf(needle) < 0) { return false; }
    }
    if (clean.window_days) {
      var started = util.toDate(summary.started_at);
      if (started) {
        var cutoff = Date.now() - Number(clean.window_days) * 86400000;
        if (started.getTime() < cutoff) { return false; }
      }
    }
    return true;
  }

  /* ------------------------------------------------------------- summaries */

  function normalizeSummary(raw) {
    if (!raw) { return null; }
    var summary = {};
    var keys = Object.keys(raw);
    for (var i = 0; i < keys.length; i++) { summary[keys[i]] = raw[keys[i]]; }
    summary.counts = normalizeCounts(raw.counts);
    summary.tags = raw.tags || [];
    if (summary.is_live === undefined) {
      summary.is_live = summary.status === 'queued' || summary.status === 'running';
    }
    return summary;
  }

  /* ----------------------------------------------------------- aggregation */

  function dayKey(value) {
    var date = util.toDate(value);
    if (!date) { return null; }
    return date.toISOString().slice(0, 10);
  }

  /**
   * Bucket runs by UTC day into TrendPoint-shaped rows.
   *
   * Pass rate per bucket is computed from the bucket's *summed* counts rather
   * than by averaging the runs' own pass rates: a 4000-test run and a 3-test
   * run are not equal votes on the health of a day.
   */
  function buildTrend(summaries, windowDays) {
    var byDay = {};
    var i;
    for (i = 0; i < summaries.length; i++) {
      var key = dayKey(summaries[i].started_at);
      if (!key) { continue; }
      if (!byDay[key]) {
        byDay[key] = { bucket: key, runs: 0, counts: emptyCounts(), durations: [] };
      }
      var bucket = byDay[key];
      bucket.runs += 1;
      bucket.counts = mergeCounts(bucket.counts, summaries[i].counts);
      if (typeof summaries[i].duration_ms === 'number') {
        bucket.durations.push(summaries[i].duration_ms);
      }
    }

    var days = Object.keys(byDay).sort();
    // Fill the gaps so a quiet weekend is a gap in the line, not a straight
    // segment pretending two Fridays were adjacent.
    var filled = [];
    if (days.length) {
      var cursor = new Date(days[0] + 'T00:00:00Z');
      var last = new Date(days[days.length - 1] + 'T00:00:00Z');
      var guard = 0;
      var limit = Math.max(Number(windowDays) || 30, 1) + 400;
      while (cursor.getTime() <= last.getTime() && guard < limit) {
        var key2 = cursor.toISOString().slice(0, 10);
        var found = byDay[key2];
        filled.push(found
          ? {
            bucket: key2,
            runs: found.runs,
            pass_rate: found.counts.pass_rate,
            failure_rate: found.counts.failure_rate,
            duration_ms_p50: roundOrNull(percentile(found.durations, 0.5)),
            duration_ms_p95: roundOrNull(percentile(found.durations, 0.95)),
            total: found.counts.total,
            failed: found.counts.failed + found.counts.error
          }
          : {
            bucket: key2, runs: 0, pass_rate: null, failure_rate: null,
            duration_ms_p50: null, duration_ms_p95: null, total: 0, failed: 0
          });
        cursor = new Date(cursor.getTime() + 86400000);
        guard += 1;
      }
    }
    return filled;
  }

  function roundOrNull(value) {
    return value === null || value === undefined ? null : Math.round(value);
  }

  /**
   * Project health rows.
   *
   * `pass_rate` aggregates every run in scope; `pass_rate_delta` is the change
   * from the previous run to the latest one — a concrete, explainable number,
   * unlike "vs the window", which has no second window to compare against.
   */
  function buildProjectHealth(summaries, projects, tests) {
    var byProject = {};
    var i;
    for (i = 0; i < summaries.length; i++) {
      var summary = summaries[i];
      var key = summary.project_key;
      if (!byProject[key]) {
        byProject[key] = { key: key, runs: [], counts: emptyCounts(), live: 0, runs24h: 0 };
      }
      var entry = byProject[key];
      entry.runs.push(summary);
      entry.counts = mergeCounts(entry.counts, summary.counts);
      if (summary.is_live) { entry.live += 1; }
      var started = util.toDate(summary.started_at);
      if (started && Date.now() - started.getTime() <= 86400000) { entry.runs24h += 1; }
    }

    var flakyByProject = {};
    for (i = 0; i < (tests || []).length; i++) {
      var test = tests[i];
      if (test.flake_rate) {
        flakyByProject[test.project_key] = (flakyByProject[test.project_key] || 0) + 1;
      }
    }

    var meta = {};
    for (i = 0; i < (projects || []).length; i++) { meta[projects[i].key] = projects[i]; }

    var rows = [];
    var keys = Object.keys(byProject);
    for (i = 0; i < keys.length; i++) {
      var project = byProject[keys[i]];
      var ordered = util.sortBy(project.runs, 'started_at', 'desc');
      var latest = ordered[0];
      var previous = ordered[1];
      var delta = null;
      if (latest && previous &&
        latest.counts.pass_rate !== null && previous.counts.pass_rate !== null) {
        delta = round2(latest.counts.pass_rate - previous.counts.pass_rate);
      }
      rows.push({
        project_key: project.key,
        name: (meta[project.key] && meta[project.key].name) || project.key,
        team: (latest && latest.team) || (meta[project.key] && meta[project.key].default_team) || null,
        runs_24h: project.runs24h,
        last_run_id: latest ? latest.id : null,
        last_run_at: latest ? latest.started_at : null,
        last_status: latest ? latest.status : 'unknown',
        pass_rate: project.counts.pass_rate,
        pass_rate_delta: delta,
        flaky_tests: flakyByProject[project.key] || 0,
        live_runs: project.live
      });
    }
    return util.sortBy(rows, function (row) { return row.last_run_at; }, 'desc');
  }

  /* ----------------------------------------------------------------- base */

  /**
   * Shared plumbing: the message bus, the status field and the one place a
   * status transition is announced. It stays separate from the implementation
   * so the connection pill's semantics live in one file rather than in the
   * transport that happens to be reporting them.
   */
  function createBase(kind, capabilities, status) {
    var bus = util.emitter();
    return {
      kind: kind,
      capabilities: capabilities,
      status: status,
      subscribe: function (handler) {
        return bus.on('message', handler);
      },
      _emit: function (message) {
        bus.emit('message', message);
      },
      _setStatus: function (next, detail) {
        if (this.status === next) { return; }
        this.status = next;
        this._emit({ type: 'status', status: next, detail: detail || null });
      }
    };
  }

  TestHub.provider = {
    STATUSES: STATUSES,
    FACET_KEYS: FACET_KEYS,
    LOCAL_KEYS: LOCAL_KEYS,
    COUNT_FIELDS: COUNT_FIELDS,
    createBase: createBase,
    emptyCounts: emptyCounts,
    finalizeCounts: finalizeCounts,
    normalizeCounts: normalizeCounts,
    mergeCounts: mergeCounts,
    addStatus: addStatus,
    percentile: percentile,
    round2: round2,
    cleanFilters: cleanFilters,
    hasFacetFilter: hasFacetFilter,
    matchesFilters: matchesFilters,
    normalizeSummary: normalizeSummary,
    buildTrend: buildTrend,
    buildProjectHealth: buildProjectHealth,
    dayKey: dayKey
  };
})(window);
