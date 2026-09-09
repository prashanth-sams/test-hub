/* Test Hub — the store: one state object, one place that talks to the provider.
 *
 * Views never call `fetch`, never call the provider and never keep their own
 * copy of anything (ADR-0001 D1). They read `store.state` and re-render when
 * the store says a section changed. That is what lets the same view code sit on
 * top of a frozen snapshot and a live stream without a single conditional.
 *
 * The URL is the source of truth for filters. `#/runs?team=payments&page=2` is
 * not a side effect of the filter bar — it *is* the filter bar's state, which
 * is why a filtered view can be linked to, bookmarked and reloaded, and why the
 * back button does the obvious thing.
 *
 * Two details worth keeping:
 *
 *  - Every load carries a token. A slow overview request that lands after the
 *    user has already navigated to a run must not repaint the run page with
 *    stale data, and "the last response wins" is wrong when responses can
 *    overtake each other.
 *  - A live change notification is scoped. Events for run X do not reload the
 *    run page for Y; a `resync` reloads whatever is on screen, because after a
 *    resync we know nothing.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;

  //: Filter keys that survive navigation between views.
  var FILTER_KEYS = [
    'project', 'team', 'component', 'test_type', 'environment', 'branch',
    'status', 'q', 'window_days'
  ];
  var DEFAULT_WINDOW_DAYS = 30;
  var DEFAULT_PAGE_SIZE = 25;

  function create(provider) {
    var bus = util.emitter();
    var tokens = { overview: 0, runs: 0, run: 0, test: 0, facets: 0 };

    var state = {
      providerKind: provider.kind,
      capabilities: provider.capabilities,
      status: provider.status,
      statusDetail: null,
      meta: provider.meta || null,
      route: { name: 'overview', params: {}, query: {} },
      filters: {},
      facets: null,
      overview: null,
      runs: null,
      run: null,
      test: null,
      lastUpdated: null,
      loading: { overview: false, runs: false, run: false, test: false },
      errors: { overview: null, runs: null, run: null, test: null }
    };

    function emit(section) {
      bus.emit('update', { section: section, state: state });
    }

    function touch() {
      state.lastUpdated = new Date().toISOString();
    }

    /* ------------------------------------------------------------ routing */

    function filtersFromQuery(query) {
      var filters = {};
      for (var i = 0; i < FILTER_KEYS.length; i++) {
        var key = FILTER_KEYS[i];
        if (query[key] !== undefined && query[key] !== '') { filters[key] = query[key]; }
      }
      if (!filters.window_days) { filters.window_days = DEFAULT_WINDOW_DAYS; }
      return filters;
    }

    function parseRoute(hash) {
      var parsed = util.parseHash(hash);
      var segments = parsed.segments;
      var head = segments[0] || 'overview';
      if (head === 'runs' && segments[1]) {
        return { name: 'run', params: { runId: segments[1] }, query: parsed.query };
      }
      if (head === 'runs') {
        return { name: 'runs', params: {}, query: parsed.query };
      }
      if (head === 'tests' && segments[1]) {
        return { name: 'test', params: { testId: segments[1] }, query: parsed.query };
      }
      if (head === 'tests') {
        return { name: 'runs', params: {}, query: parsed.query };
      }
      return { name: 'overview', params: {}, query: parsed.query };
    }

    function applyRoute(hash) {
      var route = parseRoute(hash);
      var previous = state.route;
      state.route = route;
      state.filters = filtersFromQuery(route.query);
      emit('route');

      var sameView = previous.name === route.name &&
        previous.params.runId === route.params.runId &&
        previous.params.testId === route.params.testId;

      if (route.name === 'overview') {
        loadOverview();
      } else if (route.name === 'runs') {
        loadRuns();
      } else if (route.name === 'run') {
        loadRun(route.params.runId, { keepPrevious: sameView });
      } else if (route.name === 'test') {
        loadTest(route.params.testId, { keepPrevious: sameView });
      }
      return route;
    }

    /**
     * Build a hash for the current view with `patch` applied to its query.
     * A value of null removes the key — which is how "All teams" clears a
     * filter rather than adding `team=`.
     */
    function hashWith(patch, options) {
      var route = (options && options.route) || state.route;
      var query = {};
      var keys = Object.keys(route.query || {});
      var i;
      for (i = 0; i < keys.length; i++) { query[keys[i]] = route.query[keys[i]]; }
      var patchKeys = Object.keys(patch || {});
      for (i = 0; i < patchKeys.length; i++) {
        var value = patch[patchKeys[i]];
        if (value === null || value === undefined || value === '') {
          delete query[patchKeys[i]];
        } else {
          query[patchKeys[i]] = value;
        }
      }
      var segments;
      if (options && options.segments) {
        segments = options.segments;
      } else if (route.name === 'run') {
        segments = ['runs', route.params.runId];
      } else if (route.name === 'test') {
        segments = ['tests', route.params.testId];
      } else if (route.name === 'runs') {
        segments = ['runs'];
      } else {
        segments = ['overview'];
      }
      return util.buildHash(segments, query);
    }

    /* -------------------------------------------------------------- loads */

    function begin(section) {
      tokens[section] += 1;
      state.loading[section] = true;
      state.errors[section] = null;
      emit(section);
      return tokens[section];
    }

    function settle(section, token, error) {
      if (token !== tokens[section]) { return false; }
      state.loading[section] = false;
      state.errors[section] = error || null;
      if (!error) { touch(); }
      emit(section);
      return true;
    }

    function loadFacets() {
      var token = ++tokens.facets;
      return provider.getFacets().then(function (facets) {
        if (token !== tokens.facets) { return; }
        state.facets = facets;
        emit('facets');
      }, function (error) {
        util.reportError('facets unavailable', error);
      });
    }

    function loadOverview() {
      var token = begin('overview');
      return provider.getOverview(state.filters).then(function (overview) {
        if (token !== tokens.overview) { return; }
        state.overview = overview;
        settle('overview', token, null);
      }, function (error) {
        settle('overview', token, error);
      });
    }

    function loadRuns() {
      var token = begin('runs');
      var query = state.route.query || {};
      var limit = Math.max(1, Number(query.limit) || DEFAULT_PAGE_SIZE);
      var page = Math.max(1, Number(query.page) || 1);
      var filters = {};
      var keys = Object.keys(state.filters);
      for (var i = 0; i < keys.length; i++) { filters[keys[i]] = state.filters[keys[i]]; }
      // The run list is paged, not windowed: `GET /runs` has no window
      // parameter, so passing one through would silently narrow the snapshot
      // and be ignored by the API, and the two modes would disagree about what
      // the same URL means. The overview owns `window_days`; this view does not.
      delete filters.window_days;
      filters.limit = limit;
      filters.offset = (page - 1) * limit;
      filters.sort = query.sort || 'started_at';
      filters.dir = query.dir || 'desc';

      return provider.listRuns(filters).then(function (result) {
        if (token !== tokens.runs) { return; }
        state.runs = {
          runs: result.runs || [],
          total: result.total || 0,
          page: page,
          limit: limit,
          offset: filters.offset,
          exact: result.exact !== false,
          // 'all' when the provider ordered the whole result set, 'page' when it
          // could only order the rows it was handed. The view says which.
          sorted: result.sorted || 'all',
          sort: filters.sort,
          dir: filters.dir
        };
        settle('runs', token, null);
      }, function (error) {
        settle('runs', token, error);
      });
    }

    function loadRun(runId, options) {
      if (!runId) { return Promise.resolve(); }
      var token = begin('run');
      if (!options || !options.keepPrevious) { state.run = null; }
      return provider.getRun(runId).then(function (detail) {
        if (token !== tokens.run) { return; }
        state.run = detail;
        settle('run', token, null);
      }, function (error) {
        if (token === tokens.run) { state.run = null; }
        settle('run', token, error);
      });
    }

    function loadTest(testId, options) {
      if (!testId) { return Promise.resolve(); }
      var token = begin('test');
      if (!options || !options.keepPrevious) { state.test = null; }
      return provider.getTest(testId).then(function (detail) {
        if (token !== tokens.test) { return; }
        state.test = detail;
        settle('test', token, null);
      }, function (error) {
        if (token === tokens.test) { state.test = null; }
        settle('test', token, error);
      });
    }

    function reloadCurrent() {
      var route = state.route;
      if (route.name === 'overview') { return loadOverview(); }
      if (route.name === 'runs') { return loadRuns(); }
      if (route.name === 'run') { return loadRun(route.params.runId, { keepPrevious: true }); }
      if (route.name === 'test') { return loadTest(route.params.testId, { keepPrevious: true }); }
      return Promise.resolve();
    }

    /* ------------------------------------------------------- live updates */

    function onProviderMessage(message) {
      if (message.type === 'status') {
        state.status = message.status || provider.status;
        state.statusDetail = message.detail || null;
        emit('status');
        return;
      }
      if (message.type !== 'change') { return; }
      var route = state.route;
      var resync = message.reason === 'resync';
      if (route.name === 'overview') {
        loadOverview();
      } else if (route.name === 'runs') {
        loadRuns();
      } else if (route.name === 'run') {
        var touched = (message.runIds || []).indexOf(String(route.params.runId)) >= 0;
        if (touched || resync) { loadRun(route.params.runId, { keepPrevious: true }); }
      } else if (route.name === 'test') {
        loadTest(route.params.testId, { keepPrevious: true });
      }
      if (resync) { loadFacets(); }
    }

    var store = {
      state: state,
      FILTER_KEYS: FILTER_KEYS,
      DEFAULT_WINDOW_DAYS: DEFAULT_WINDOW_DAYS,
      DEFAULT_PAGE_SIZE: DEFAULT_PAGE_SIZE,
      on: function (fn) { return bus.on('update', fn); },
      applyRoute: applyRoute,
      parseRoute: parseRoute,
      hashWith: hashWith,
      loadFacets: loadFacets,
      reload: reloadCurrent,
      retry: reloadCurrent,
      provider: provider,
      start: function () {
        provider.subscribe(onProviderMessage);
        if (provider.start) { provider.start(); }
        state.status = provider.status;
        loadFacets();
        applyRoute();
      }
    };

    return store;
  }

  TestHub.store = { create: create, FILTER_KEYS: FILTER_KEYS };
})(window);
