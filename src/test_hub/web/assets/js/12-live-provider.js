/* Test Hub — LiveDataProvider: REST for state, SSE for "something changed".
 *
 * The stream is deliberately NOT the source of truth. Events tell us which runs
 * moved; REST tells us what they now look like. Folding the event payloads into
 * a client-side copy of the model would be a second implementation of
 * test_hub.service's folding logic, and the two would disagree the first time a
 * retry arrived out of order. So: a burst of events collapses into a debounced
 * "refetch these runs" and the server stays the only thing that computes counts.
 *
 * What this file has to survive, because all of it happens in practice:
 *
 *  - 500 events in two seconds when a large suite finishes. Debounced with a
 *    ceiling, so it becomes a handful of requests, not 500.
 *  - A proxy that buffers. Hence heartbeats: a `:` comment (which EventSource
 *    swallows silently, and which therefore only keeps the socket warm) and a
 *    named `heartbeat` event, which we can actually see. If nothing arrives for
 *    STALE_AFTER we assume a black hole and reconnect rather than displaying
 *    stale numbers under a green pill.
 *  - A laptop closing. The browser reconnects EventSource by itself and resends
 *    Last-Event-ID; we let it, because the bus replays from that cursor. Only
 *    when the browser gives up (readyState CLOSED) do we reconnect ourselves,
 *    with backoff, carrying `last_event_id` as a query parameter — a new
 *    EventSource object has no memory of the old one's cursor.
 *  - Missing more than the replay buffer holds. The server answers with a
 *    `resync` directive; we refetch everything rather than pretending.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var provider = TestHub.provider;

  var REQUEST_TIMEOUT_MS = 20000;
  var FLUSH_DEBOUNCE_MS = 350;
  var FLUSH_MAX_WAIT_MS = 2000;
  var STALE_AFTER_MS = 45000;
  var WATCHDOG_MS = 5000;
  var BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];
  var OFFLINE_AFTER_FAILURES = 3;

  function resolveApiBase() {
    if (window.TEST_HUB_API_BASE) { return String(window.TEST_HUB_API_BASE); }
    var meta = document.querySelector('meta[name="test-hub-api-base"]');
    if (meta && meta.getAttribute('content')) { return meta.getAttribute('content'); }
    try {
      // Relative to the page, so a dashboard mounted under a reverse-proxy
      // prefix (Settings.root_path) finds its own API without being told.
      return new window.URL('api/v1/', document.baseURI).toString();
    } catch (err) {
      return '/api/v1/';
    }
  }

  function joinUrl(base, path) {
    var left = String(base).replace(/\/+$/, '');
    var right = String(path).replace(/^\/+/, '');
    return left + '/' + right;
  }

  function apiError(message, code, status, detail) {
    var error = new Error(message);
    error.code = code || 'request_failed';
    error.status = status || 0;
    error.detail = detail || null;
    return error;
  }

  function create(options) {
    var config = options || {};
    var apiBase = config.apiBase || resolveApiBase();
    var streamUrl = config.streamUrl || joinUrl(apiBase, 'events/stream');

    var capabilities = { live: true, artifacts: true, paging: true };
    var self = provider.createBase('live', capabilities, 'connecting');

    var source = null;
    var attempt = 0;
    var failures = 0;
    var reconnectTimer = null;
    var watchdogTimer = null;
    var lastEventId = null;
    var lastMessageAt = 0;
    var pendingRuns = {};
    var pendingTypes = {};
    var pendingReason = 'events';
    var facetCache = null;
    var closed = false;

    self.meta = { apiBase: apiBase, streamUrl: streamUrl };

    /* ------------------------------------------------------------- fetch */

    function request(path, params) {
      var query = util.buildQuery(params || {});
      var url = joinUrl(apiBase, path) + (query ? '?' + query : '');
      var controller = null;
      var timer = null;
      var init = { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin' };
      if (window.AbortController) {
        controller = new window.AbortController();
        init.signal = controller.signal;
        timer = window.setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
      }
      return window.fetch(url, init).then(function (response) {
        if (timer) { window.clearTimeout(timer); }
        return response.text().then(function (body) {
          var payload = null;
          if (body) {
            try {
              payload = JSON.parse(body);
            } catch (err) {
              payload = null;
            }
          }
          if (!response.ok) {
            var message = (payload && (payload.message || payload.detail)) ||
              ('Request failed with HTTP ' + response.status);
            throw apiError(
              typeof message === 'string' ? message : 'Request failed',
              payload && payload.error,
              response.status,
              payload && payload.detail
            );
          }
          if (payload === null) {
            throw apiError('The API returned a response that is not JSON.', 'bad_response', response.status);
          }
          return payload;
        });
      }, function (err) {
        if (timer) { window.clearTimeout(timer); }
        if (err && err.name === 'AbortError') {
          throw apiError('The request timed out after ' + (REQUEST_TIMEOUT_MS / 1000) + ' s.', 'timeout', 0);
        }
        // A network-level failure here is the same condition the stream is in.
        markUnreachable();
        throw apiError('Could not reach the Test Hub API at ' + apiBase, 'unreachable', 0);
      });
    }

    function facetParams(filters) {
      var clean = provider.cleanFilters(filters);
      var params = {};
      for (var i = 0; i < provider.FACET_KEYS.length; i++) {
        var key = provider.FACET_KEYS[i];
        if (clean[key] !== undefined) { params[key] = clean[key]; }
      }
      // The query API spells the test-type facet `type` (it is an alias in
      // api.routes.queries.build_filter) while the URL, the snapshot and every
      // model field spell it `test_type`. Sending both is free — a server reads
      // the one it declared and ignores the other — and means the filter works
      // whichever spelling this build settled on.
      if (params.test_type !== undefined) { params.type = params.test_type; }
      if (clean.window_days) { params.window_days = clean.window_days; }
      return params;
    }

    /* ---------------------------------------------------------- read API */

    function shapeOverview(payload) {
      var overview = {};
      var keys = Object.keys(payload);
      for (var i = 0; i < keys.length; i++) { overview[keys[i]] = payload[keys[i]]; }
      overview.counts = provider.normalizeCounts(payload.counts);
      overview.live_runs = (payload.live_runs || []).map(provider.normalizeSummary);
      overview.recent_runs = (payload.recent_runs || []).map(provider.normalizeSummary);
      overview.trend = payload.trend || [];
      overview.projects = payload.projects || [];
      overview.top_failing_tests = payload.top_failing_tests || [];
      overview.flakiest_tests = payload.flakiest_tests || [];
      overview.filters = payload.filters || {};
      if (overview.filters && Object.keys(overview.filters).length) {
        facetCache = overview.filters;
      }
      return overview;
    }

    //: How many runs a browser-side narrowing is willing to fold. The API caps
    //: `limit` at 500, and past that the answer is honestly labelled truncated.
    var NARROW_LIMIT = 500;

    /**
     * Recompute the overview for a filtered slice.
     *
     * ``GET /overview`` answers for the whole workspace and takes only
     * ``window_days``; the facet parameters ride along in case a build honours
     * them, but this client cannot assume they were applied. Rather than show
     * portfolio-wide totals under an active filter — which would be a lie the
     * user cannot see — the matching runs are fetched and folded with the same
     * arithmetic the snapshot provider uses, and the result says so.
     */
    function narrowOverview(base, clean) {
      var params = facetParams(clean);
      params.limit = NARROW_LIMIT;
      params.offset = 0;
      if (clean.status) { params.status = clean.status; }
      if (clean.q) { params.q = clean.q; params.search = clean.q; }

      return request('runs', params).then(function (payload) {
        var summaries = (payload.runs || []).map(provider.normalizeSummary)
          .filter(function (summary) { return provider.matchesFilters(summary, clean); });
        var counts = provider.emptyCounts();
        var live = [];
        var i;
        for (i = 0; i < summaries.length; i++) {
          counts = provider.mergeCounts(counts, summaries[i].counts);
          if (summaries[i].is_live) { live.push(summaries[i]); }
        }
        var ordered = util.sortBy(summaries, 'started_at', 'desc');
        var windowDays = Number(clean.window_days) || base.window_days || 30;
        var truncated = typeof payload.total === 'number' && payload.total > summaries.length;

        return {
          schema_version: base.schema_version,
          generated_at: new Date().toISOString(),
          window_days: windowDays,
          counts: counts,
          total_runs: summaries.length,
          total_runs_all_time: base.total_runs_all_time || 0,
          live_runs: live,
          recent_runs: ordered.slice(0, 8),
          projects: provider.buildProjectHealth(ordered, [], []),
          trend: provider.buildTrend(ordered, windowDays),
          top_failing_tests: [],
          flakiest_tests: [],
          filters: base.filters || {},
          derived: true,
          // The per-test rankings need history this endpoint does not carry, so
          // they are declared missing instead of being shown stale or empty.
          test_lists: 'unavailable',
          derived_note: 'The server answers the overview for the whole workspace, so these ' +
            'filtered totals were recomputed in your browser from the ' +
            util.formatNumber(summaries.length) + ' matching ' +
            util.pluralize(summaries.length, 'run') +
            (truncated ? ' the API returned (the newest ' + NARROW_LIMIT + ').' : '.')
        };
      });
    }

    self.getOverview = function (filters) {
      var clean = provider.cleanFilters(filters);
      return request('overview', facetParams(clean)).then(function (payload) {
        var overview = shapeOverview(payload);
        if (!provider.hasFacetFilter(clean)) { return overview; }
        return narrowOverview(overview, clean);
      });
    };

    self.listRuns = function (filters) {
      var clean = provider.cleanFilters(filters);
      var params = facetParams(clean);
      params.limit = Math.max(1, Number(clean.limit) || 25);
      params.offset = Math.max(0, Number(clean.offset) || 0);
      // Sent optimistically: a server that understands them filters properly and
      // reports an honest total; one that ignores them costs nothing, because
      // the same predicate runs again below.
      if (clean.status) { params.status = clean.status; }
      // `search` is what api.routes.queries calls it; `q` is what the URL and
      // the snapshot provider call it. Both go, for the same reason as `type`.
      if (clean.q) { params.q = clean.q; params.search = clean.q; }

      return request('runs', params).then(function (payload) {
        var runs = (payload.runs || []).map(provider.normalizeSummary);
        var local = {};
        var refined = false;
        if (clean.status) { local.status = clean.status; }
        if (clean.q) { local.q = clean.q; }
        if (Object.keys(local).length) {
          var kept = runs.filter(function (summary) {
            return provider.matchesFilters(summary, local);
          });
          refined = kept.length !== runs.length;
          runs = kept;
        }
        // `GET /runs` orders by start time, newest first, and takes no sort
        // parameter — so any other column can only be ordered across the rows
        // this page actually contains. Doing it silently would put a sort arrow
        // on a column that is sorted for 25 of 4000 runs, so the scope travels
        // with the data and the view prints it. When the API grows real
        // sorting, send it and return 'all'.
        var sorted = 'all';
        if (clean.sort && clean.sort !== DEFAULT_SORT) {
          runs = util.sortBy(runs, sortKey(clean.sort), clean.dir === 'asc' ? 'asc' : 'desc');
          sorted = 'page';
        } else if (clean.dir === 'asc') {
          runs = util.sortBy(runs, DEFAULT_SORT, 'asc');
          sorted = 'page';
        }

        return {
          runs: runs,
          total: typeof payload.total === 'number' ? payload.total : runs.length,
          offset: params.offset,
          limit: params.limit,
          sorted: sorted,
          // False when the page was narrowed in the browser, so the view can
          // say "showing N of this page" instead of lying about the total.
          exact: !refined
        };
      });
    };

    //: The order `GET /runs` already returns. Asking for it costs no re-sort.
    var DEFAULT_SORT = 'started_at';

    /** Shared with the snapshot provider in spirit: counts live one level down. */
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
      return request('runs/' + encodeURIComponent(runId));
    };

    self.getTest = function (testId) {
      return request('tests/' + encodeURIComponent(testId));
    };

    /**
     * Facet values for the filter bar.
     *
     * `GET /facets` is the cheap, purpose-built answer. Falling back to the
     * overview's own `filters` block keeps the filters populated against a
     * server that predates that endpoint, and `/projects` fills the project
     * list on a workspace whose runs have all aged out of the window.
     */
    self.getFacets = function () {
      var projectsPromise = request('projects').then(function (payload) {
        return (payload.projects || []).map(function (project) { return project.key; });
      }, function () {
        return [];
      });
      var facetsPromise = request('facets').then(function (payload) {
        return payload && typeof payload === 'object' ? payload : {};
      }, function () {
        if (facetCache) { return facetCache; }
        return self.getOverview({}).then(function (overview) { return overview.filters || {}; },
          function () { return {}; });
      });

      return Promise.all([projectsPromise, facetsPromise]).then(function (results) {
        var facets = {
          projects: [], teams: [], components: [], test_types: [],
          environments: [], branches: [], statuses: [], triggers: []
        };
        var fromServer = results[1] || {};
        var keys = Object.keys(fromServer);
        for (var i = 0; i < keys.length; i++) {
          if (fromServer[keys[i]] && fromServer[keys[i]].length) {
            facets[keys[i]] = fromServer[keys[i]].slice();
          }
        }
        if (results[0].length) {
          facets.projects = util.unique(facets.projects.concat(results[0]));
        }
        return facets;
      });
    };

    /* -------------------------------------------------------- change fan */

    var flush = util.debounce(function () {
      var runIds = Object.keys(pendingRuns);
      var types = Object.keys(pendingTypes);
      var reason = pendingReason;
      pendingRuns = {};
      pendingTypes = {};
      pendingReason = 'events';
      self._emit({
        type: 'change',
        runIds: runIds,
        types: types,
        reason: reason,
        at: new Date().toISOString()
      });
    }, FLUSH_DEBOUNCE_MS, FLUSH_MAX_WAIT_MS);

    function markChanged(runId, eventType, reason) {
      if (runId) { pendingRuns[String(runId)] = true; }
      if (eventType) { pendingTypes[String(eventType)] = true; }
      if (reason === 'resync') { pendingReason = 'resync'; }
      flush();
    }

    /* ------------------------------------------------------------ stream */

    function markUnreachable() {
      failures += 1;
      self._setStatus(failures >= OFFLINE_AFTER_FAILURES ? 'offline' : 'reconnecting');
    }

    function touch() {
      lastMessageAt = Date.now();
    }

    function onEnvelope(event) {
      touch();
      if (event.lastEventId) { lastEventId = event.lastEventId; }
      failures = 0;
      self._setStatus('live');
      var payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch (err) {
        // A malformed frame is not a reason to tear the stream down, but it is
        // a reason to distrust our view of the world.
        markChanged(null, null, 'resync');
        return;
      }
      var envelope = (payload && payload.event) || payload || {};
      var runId = envelope.runid || envelope.run_id ||
        (envelope.data && (envelope.data.run_id || envelope.data.runid));
      markChanged(runId, envelope.type, 'events');
    }

    function connect() {
      if (closed || !window.EventSource) {
        if (!window.EventSource) { self._setStatus('offline', 'This browser has no EventSource.'); }
        return;
      }
      disconnect();
      self._setStatus(failures ? 'reconnecting' : 'connecting');
      var url = streamUrl;
      if (lastEventId) {
        url += (url.indexOf('?') >= 0 ? '&' : '?') + 'last_event_id=' + encodeURIComponent(lastEventId);
      }
      try {
        source = new window.EventSource(url, { withCredentials: false });
      } catch (err) {
        scheduleReconnect();
        return;
      }
      touch();

      source.addEventListener('open', function () {
        touch();
        var wasDown = failures > 0;
        failures = 0;
        attempt = 0;
        self._setStatus('live');
        // Anything could have happened while we were away, and the bus only
        // replays what its ring still holds.
        if (wasDown) { markChanged(null, null, 'resync'); }
      });

      source.addEventListener('hello', function (event) {
        touch();
        if (event.lastEventId) { lastEventId = event.lastEventId; }
        self._setStatus('live');
      });

      source.addEventListener('heartbeat', function (event) {
        touch();
        // Deliberately does NOT update lastEventId: the server gives heartbeats
        // the latest cursor, and adopting it would skip past events we have not
        // actually seen if we reconnect on the next tick.
        if (event) { self._setStatus('live'); }
      });

      source.addEventListener('resync', function (event) {
        touch();
        var detail = null;
        try {
          detail = JSON.parse(event.data);
        } catch (err) {
          detail = null;
        }
        self._emit({
          type: 'status', status: self.status,
          detail: 'Resyncing: ' + ((detail && detail.reason) || 'the server asked us to')
        });
        markChanged(null, null, 'resync');
      });

      source.addEventListener('test-hub', onEnvelope);
      source.addEventListener('message', onEnvelope);

      source.addEventListener('error', function () {
        if (closed) { return; }
        if (source && source.readyState === 2 /* CLOSED */) {
          failures += 1;
          self._setStatus(failures >= OFFLINE_AFTER_FAILURES ? 'offline' : 'reconnecting');
          scheduleReconnect();
          return;
        }
        // readyState CONNECTING: the browser is retrying on its own and will
        // resend Last-Event-ID for us. Say so, and stay out of its way.
        self._setStatus('reconnecting');
      });
    }

    function disconnect() {
      if (source) {
        try {
          source.close();
        } catch (err) {
          util.reportError('closing the event stream failed', err);
        }
        source = null;
      }
    }

    function scheduleReconnect() {
      if (closed || reconnectTimer) { return; }
      var delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      // Jitter, so twenty dashboards left open overnight do not all wake up in
      // the same millisecond when the server comes back.
      var jittered = delay + Math.floor(Math.random() * 400);
      reconnectTimer = window.setTimeout(function () {
        reconnectTimer = null;
        connect();
      }, jittered);
    }

    function startWatchdog() {
      if (watchdogTimer) { return; }
      watchdogTimer = window.setInterval(function () {
        if (closed || !source) { return; }
        if (Date.now() - lastMessageAt < STALE_AFTER_MS) { return; }
        // Silent for longer than any heartbeat interval: something between us
        // and the server is holding the connection open and empty.
        self._setStatus('reconnecting', 'No heartbeat — reconnecting.');
        failures += 1;
        disconnect();
        scheduleReconnect();
      }, WATCHDOG_MS);
    }

    function wakeUp() {
      if (closed) { return; }
      if (self.status === 'live') { return; }
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      attempt = 0;
      connect();
    }

    window.addEventListener('online', wakeUp);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { wakeUp(); }
    });
    window.addEventListener('offline', function () {
      self._setStatus('offline', 'The browser reports no network.');
    });

    self.start = function () {
      closed = false;
      connect();
      startWatchdog();
      return self;
    };

    self.close = function () {
      closed = true;
      disconnect();
      if (reconnectTimer) { window.clearTimeout(reconnectTimer); reconnectTimer = null; }
      if (watchdogTimer) { window.clearInterval(watchdogTimer); watchdogTimer = null; }
    };

    self.reconnect = function () {
      failures = 0;
      attempt = 0;
      wakeUp();
    };

    self.refresh = function () {
      markChanged(null, null, 'resync');
      return Promise.resolve(true);
    };

    return self;
  }

  TestHub.LiveDataProvider = { create: create, resolveApiBase: resolveApiBase };
})(window, document);
