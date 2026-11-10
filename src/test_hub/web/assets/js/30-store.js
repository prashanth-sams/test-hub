/* Test Hub — the store: one state object, one place that talks to the provider.
 *
 * Views never call `fetch`, never call the provider and never keep their own
 * copy of anything (ADR-0001 D1). They read `store.state` and re-render when
 * the store says a section changed — so a view is a pure function of state, and
 * the transport underneath it is somebody else's problem.
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

  /* The overview's live timeline looks back over one of these. Each is about
   * sixty slots wide — `bin` is one slot, the time a bar stands for — and
   * `tick` is how far apart the clock labels sit. The span rides in the URL as
   * `span`, like a filter, so a timeline left on a wall screen survives a
   * reload; unlike a filter it narrows only the timeline. */
  var TIMELINE_SPANS = {
    '15m': { key: '15m', label: '15 minutes', ms: 15 * 60000, bin: 15000, tick: 3 * 60000 },
    '1h': { key: '1h', label: 'hour', ms: 3600000, bin: 60000, tick: 10 * 60000 },
    '6h': { key: '6h', label: '6 hours', ms: 6 * 3600000, bin: 5 * 60000, tick: 3600000 },
    '24h': { key: '24h', label: '24 hours', ms: 24 * 3600000, bin: 30 * 60000, tick: 3 * 3600000 }
  };
  var DEFAULT_TIMELINE_SPAN = '1h';
  //: The most runs one timeline load asks for — the API's own page ceiling.
  var TIMELINE_LIMIT = 500;

  /* The routes that read the same thing: the portfolio overview plus the most
   * recent finished runs, in full. Analytics asks that dataset why the suite
   * fails; suites, metrics, screenshots, logs and coverage ask it what ran,
   * what it cost and what it left behind. One loader, one cache, one loading
   * state — seven pages that would otherwise each fetch the same five runs.
   *
   * `steps` is in the list because it is honest about being empty: it says how
   * many runs it looked through before finding no step data, and that sentence
   * needs the same runs as everything else. */
  var DETAIL_ROUTES = {
    analytics: true,
    suites: true,
    metrics: true,
    steps: true,
    screenshots: true,
    logs: true,
    coverage: true
  };

  function create(provider) {
    var bus = util.emitter();
    var tokens = { overview: 0, runs: 0, run: 0, test: 0, analytics: 0, facets: 0, timeline: 0 };

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
      analytics: null,
      timeline: null,
      lastUpdated: null,
      loading: {
        overview: false, runs: false, run: false, test: false, analytics: false, timeline: false
      },
      errors: { overview: null, runs: null, run: null, test: null, analytics: null, timeline: null }
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
      if (DETAIL_ROUTES[head]) {
        return { name: head, params: {}, query: parsed.query };
      }
      if (head === 'settings') {
        return { name: 'settings', params: {}, query: parsed.query };
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
        loadTimeline();
      } else if (DETAIL_ROUTES[route.name]) {
        loadRunDetails();
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
      } else if (DETAIL_ROUTES[route.name]) {
        segments = [route.name];
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

    function timelineSpan() {
      var key = state.route.query && state.route.query.span;
      return TIMELINE_SPANS[key] || TIMELINE_SPANS[DEFAULT_TIMELINE_SPAN];
    }

    /**
     * The runs the overview's live timeline draws: every run that started
     * inside its span, under the same facet filters as the rest of the page.
     *
     * Its own section rather than part of the overview, for two reasons. The
     * overview has to stay cheap enough to poll, and a day of runs is not.
     * And the timeline is redrawn without the rest of the page: a live event
     * moves its bars, and nothing else on the page should flicker for it.
     */
    function loadTimeline() {
      var token = begin('timeline');
      var span = timelineSpan();
      var filters = {};
      var keys = Object.keys(state.filters);
      for (var i = 0; i < keys.length; i++) { filters[keys[i]] = state.filters[keys[i]]; }
      // The span is the timeline's window; the page's `window_days` is not.
      delete filters.window_days;
      // One slot of slack at the far end, so the bar sliding off the left edge
      // is still drawn while it goes.
      filters.since = new Date(Date.now() - span.ms - span.bin).toISOString();
      filters.limit = TIMELINE_LIMIT;
      return provider.listRuns(filters).then(function (result) {
        if (token !== tokens.timeline) { return; }
        var runs = result.runs || [];
        state.timeline = {
          span: span.key,
          runs: runs,
          truncated: typeof result.total === 'number' && result.total > runs.length
        };
        settle('timeline', token, null);
      }, function (error) {
        settle('timeline', token, error);
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
      // parameter, so passing one through would be silently ignored and the URL
      // would claim a narrowing that never happened. The overview owns
      // `window_days`; this view does not.
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

    //: How many finished runs the detail pages read in full. Five is the
    //: number at which "what keeps breaking" stops being one run's luck, and
    //: it is also the point past which a live server is being asked for real
    //: work to draw one page. The API answers them in parallel.
    var ANALYTICS_RUNS = 5;

    /**
     * The detail pages read the portfolio overview and the newest finished
     * runs that match the filters, in full.
     *
     * In full is the point: failure taxonomy, suite hotspots, ownership and the
     * slowest tests all need every attempt of every test, and no summary
     * carries those. Five runs rather than the window's worth of them, because
     * the questions this page answers are about what the suite is doing *now*
     * and the cost of asking is one payload per run.
     *
     * Live runs are skipped deliberately: half a run's failures classified as
     * if they were all of them is a wrong answer, not a partial one.
     */
    function analyticsRunsFrom(overview) {
      var candidates = (overview && overview.recent_runs) || [];
      var chosen = [];
      for (var i = 0; i < candidates.length && chosen.length < ANALYTICS_RUNS; i++) {
        if (candidates[i].is_live || !candidates[i].finished_at) { continue; }
        chosen.push(candidates[i]);
      }
      return chosen;
    }

    /**
     * Finished runs do not change. Their details are immutable once the run has
     * ended, which is what makes them safe to keep — and what turns a live
     * update on this page from five requests into none. A resync empties it,
     * because after a resync we know nothing, including whether those runs
     * still exist.
     */
    var analyticsCache = {};
    var ANALYTICS_CACHE_MAX = 20;

    function cacheRun(detail) {
      var keys = Object.keys(analyticsCache);
      if (keys.length >= ANALYTICS_CACHE_MAX) { delete analyticsCache[keys[0]]; }
      analyticsCache[String(detail.run.id)] = detail;
      return detail;
    }

    function loadRunDetails() {
      // The analytics section owns the loading state for the whole page: the
      // overview it fetches is a dependency of this view, not a second view
      // loading beside it, and two spinners for one page is one too many.
      var token = begin('analytics');
      return provider.getOverview(state.filters).then(function (overview) {
        if (token !== tokens.analytics) { return null; }
        state.overview = overview;
        emit('overview');

        var summaries = analyticsRunsFrom(overview);
        if (!summaries.length) {
          state.analytics = { summaries: [], runs: [], partial: false };
          settle('analytics', token, null);
          return null;
        }
        // One run that will not load must not cost the reader the other four,
        // so every fetch resolves — to the detail, or to null — and the page
        // says how many it is drawing.
        return Promise.all(summaries.map(function (summary) {
          var cached = analyticsCache[String(summary.id)];
          if (cached) { return Promise.resolve(cached); }
          return provider.getRun(summary.id).then(cacheRun, function (error) {
            util.reportError('run ' + summary.id + ' did not load for analytics', error);
            return null;
          });
        })).then(function (details) {
          if (token !== tokens.analytics) { return null; }
          var loaded = details.filter(Boolean);
          state.analytics = {
            summaries: summaries,
            runs: loaded,
            partial: loaded.length !== summaries.length
          };
          settle('analytics', token, loaded.length ? null : {
            message: 'None of the recent runs could be loaded.',
            code: 'analytics_unavailable'
          });
          return null;
        });
      }, function (error) {
        settle('analytics', token, error);
        return null;
      });
    }

    function reloadCurrent() {
      var route = state.route;
      if (route.name === 'overview') { return Promise.all([loadOverview(), loadTimeline()]); }
      if (DETAIL_ROUTES[route.name]) { return loadRunDetails(); }
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
        // The timeline is the part of this page a live event is *for*: a run
        // that just started is a new bar, a result that just landed a taller one.
        loadTimeline();
      } else if (DETAIL_ROUTES[route.name]) {
        // Cheap to repeat: the overview is one request and the finished runs it
        // names are already cached, so a live update costs a run payload only
        // when a run this page has never drawn appears.
        if (resync) { analyticsCache = {}; }
        loadRunDetails();
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
      TIMELINE_SPANS: TIMELINE_SPANS,
      DEFAULT_TIMELINE_SPAN: DEFAULT_TIMELINE_SPAN,
      timelineSpan: timelineSpan,
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

  TestHub.store = {
    create: create,
    FILTER_KEYS: FILTER_KEYS,
    DETAIL_ROUTES: DETAIL_ROUTES,
    TIMELINE_SPANS: TIMELINE_SPANS
  };
})(window);
