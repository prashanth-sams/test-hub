/* Test Hub — utilities, and the theme bootstrap that has to run first.
 *
 * This file is deliberately the first entry in assets/manifest.json, and the
 * script tags live in <head> without `defer`, because the block at the top of
 * this IIFE decides the theme BEFORE the browser paints anything. Any later
 * position and the page flashes white on the way to dark.
 *
 * Two things here are load-bearing and should not be "simplified":
 *
 *  - `data-theme` is always written explicitly onto <html>. Never left implicit
 *    for `prefers-color-scheme` to resolve, because 20-charts.js reads its
 *    palette back with getComputedStyle and an SVG cannot inherit a media
 *    query. Explicit attribute in, explicit colours out.
 *  - Every localStorage access is wrapped. Over file:// — which is exactly how
 *    the exported snapshot is opened — reading localStorage throws outright in
 *    some browsers, and a thrown exception here would leave the page unthemed
 *    and the rest of the app unloaded.
 *
 * Everything that turns data into DOM goes through `el()` or `textContent`.
 * Test names, log lines and stack traces are attacker-controlled; there is one
 * audited `escapeHtml` in this file for the rare case a string must become
 * markup, and nothing in the app calls it, which is the point.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};

  /* ---------------------------------------------------------------- theme */

  var THEME_KEY = 'testHubTheme';
  var DARK_QUERY = '(prefers-color-scheme: dark)';
  var themeListeners = [];

  function storageGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  function storageSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
      return true;
    } catch (err) {
      return false;
    }
  }

  function storageRemove(key) {
    try {
      window.localStorage.removeItem(key);
      return true;
    } catch (err) {
      return false;
    }
  }

  function systemTheme() {
    try {
      return window.matchMedia && window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light';
    } catch (err) {
      return 'light';
    }
  }

  function storedTheme() {
    var value = storageGet(THEME_KEY);
    return value === 'dark' || value === 'light' ? value : null;
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme === 'dark' ? 'dark' : 'light');
  }

  // Runs during head parsing, before first paint. Do not move it.
  applyTheme(storedTheme() || systemTheme());

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }

  function notifyTheme(theme) {
    for (var i = 0; i < themeListeners.length; i++) {
      try {
        themeListeners[i](theme);
      } catch (err) {
        reportError('theme listener failed', err);
      }
    }
  }

  function setTheme(theme, options) {
    var next = theme === 'dark' ? 'dark' : 'light';
    var persist = !options || options.persist !== false;
    applyTheme(next);
    if (persist) {
      storageSet(THEME_KEY, next);
    }
    notifyTheme(next);
    return next;
  }

  // The OS follows us only until the user has an opinion; after that their
  // choice wins in both directions until they change it again.
  (function watchSystem() {
    if (!window.matchMedia) { return; }
    var query;
    try {
      query = window.matchMedia(DARK_QUERY);
    } catch (err) {
      return;
    }
    var handler = function () {
      if (storedTheme()) { return; }
      applyTheme(systemTheme());
      notifyTheme(currentTheme());
    };
    if (query.addEventListener) {
      query.addEventListener('change', handler);
    } else if (query.addListener) {
      query.addListener(handler);
    }
  })();

  function followSystem() {
    storageRemove(THEME_KEY);
    applyTheme(systemTheme());
    notifyTheme(currentTheme());
  }

  // The *preference* — three states — as opposed to `current()`, which is the
  // two-state colour actually painted. 'system' is the absence of a stored
  // choice, so it is a real state rather than a third value in storage: there
  // is nothing to migrate if the OS switches, and clearing site data returns a
  // reader to following their OS rather than to whatever we last wrote.
  function themeMode() {
    return storedTheme() || 'system';
  }

  function setThemeMode(mode) {
    if (mode === 'system') {
      followSystem();
      return 'system';
    }
    setTheme(mode);
    return currentTheme();
  }

  var theme = {
    KEY: THEME_KEY,
    MODES: ['system', 'light', 'dark'],
    current: currentTheme,
    system: systemTheme,
    stored: storedTheme,
    mode: themeMode,
    setMode: setThemeMode,
    set: setTheme,
    toggle: function () {
      return setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
    },
    followSystem: followSystem,
    onChange: function (fn) {
      themeListeners.push(fn);
      return function () {
        var index = themeListeners.indexOf(fn);
        if (index >= 0) { themeListeners.splice(index, 1); }
      };
    }
  };

  /* --------------------------------------------------------------- errors */

  function reportError(message, err) {
    if (window.console && window.console.error) {
      window.console.error('[test-hub] ' + message, err);
    }
  }

  /* --------------------------------------------------------------- escape */

  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  /**
   * The single audited HTML escape. Present for completeness — the app builds
   * DOM instead — so that if anyone ever does need a string in markup there is
   * one correct implementation to reach for rather than a fresh regex.
   */
  function escapeHtml(value) {
    if (value === null || value === undefined) { return ''; }
    return String(value).replace(/[&<>"']/g, function (ch) { return ESCAPES[ch]; });
  }

  /* ------------------------------------------------------------------ DOM */

  var SVG_NS = 'http://www.w3.org/2000/svg';

  function appendChild(node, child) {
    if (child === null || child === undefined || child === false) { return; }
    if (Array.isArray(child)) {
      for (var i = 0; i < child.length; i++) { appendChild(node, child[i]); }
      return;
    }
    if (child.nodeType) {
      node.appendChild(child);
      return;
    }
    node.appendChild(document.createTextNode(String(child)));
  }

  /**
   * Build an element. `text` always goes through a text node, attributes always
   * through setAttribute — there is no option that treats a value as markup,
   * which is what makes a hostile test name harmless.
   */
  function el(tag, options, children) {
    var node = document.createElement(tag);
    var opts = options || {};
    var key;
    if (opts.className) { node.className = opts.className; }
    if (opts.text !== undefined && opts.text !== null) { node.textContent = String(opts.text); }
    if (opts.attrs) {
      for (key in opts.attrs) {
        if (Object.prototype.hasOwnProperty.call(opts.attrs, key)) {
          var value = opts.attrs[key];
          if (value === null || value === undefined || value === false) { continue; }
          node.setAttribute(key, value === true ? '' : String(value));
        }
      }
    }
    if (opts.dataset) {
      for (key in opts.dataset) {
        if (Object.prototype.hasOwnProperty.call(opts.dataset, key)) {
          var data = opts.dataset[key];
          if (data === null || data === undefined) { continue; }
          node.setAttribute('data-' + key, String(data));
        }
      }
    }
    if (opts.on) {
      for (key in opts.on) {
        if (Object.prototype.hasOwnProperty.call(opts.on, key)) {
          node.addEventListener(key, opts.on[key]);
        }
      }
    }
    if (children !== undefined) { appendChild(node, children); }
    return node;
  }

  function svg(tag, attrs, children) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (var key in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, key)) {
          var value = attrs[key];
          if (value === null || value === undefined || value === false) { continue; }
          node.setAttribute(key, String(value));
        }
      }
    }
    if (children !== undefined) { appendChild(node, children); }
    return node;
  }

  function frag(children) {
    var node = document.createDocumentFragment();
    appendChild(node, children);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) { node.removeChild(node.firstChild); }
    return node;
  }

  function replace(node, children) {
    clear(node);
    appendChild(node, children);
    return node;
  }

  function readCssVar(name, fallback) {
    try {
      var value = window.getComputedStyle(document.documentElement).getPropertyValue(name);
      value = (value || '').trim();
      return value || fallback || '';
    } catch (err) {
      return fallback || '';
    }
  }

  function prefersReducedMotion() {
    try {
      return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    } catch (err) {
      return false;
    }
  }

  /* ------------------------------------------------------------ formatting */

  var DASH = '—';

  function isNil(value) { return value === null || value === undefined; }

  function formatNumber(value) {
    if (isNil(value) || isNaN(value)) { return DASH; }
    try {
      return Number(value).toLocaleString();
    } catch (err) {
      return String(value);
    }
  }

  /**
   * `null` pass rate means "nothing executed" and must read as an em dash. A
   * run that only skipped is not a 0% run, and showing it in red is a lie the
   * dashboard would tell every single time somebody filters to a skipped suite.
   */
  function formatPercent(value, digits) {
    if (isNil(value) || isNaN(value)) { return DASH; }
    var places = digits === undefined ? 1 : digits;
    return Number(value).toFixed(places) + '%';
  }

  function formatDelta(value, unit) {
    if (isNil(value) || isNaN(value)) { return DASH; }
    var rounded = Math.abs(value) < 0.05 ? 0 : value;
    var sign = rounded > 0 ? '+' : (rounded < 0 ? '−' : '±');
    var body = unit === 'ms'
      ? formatDuration(Math.abs(rounded))
      : Math.abs(rounded).toFixed(unit === 'int' ? 0 : 1) + (unit === 'pct' ? '%' : '');
    return sign + body;
  }

  function deltaDirection(value, options) {
    if (isNil(value) || isNaN(value) || Math.abs(value) < 0.05) { return 'flat'; }
    var betterIsUp = !options || options.betterIsUp !== false;
    var positive = value > 0;
    return (positive === betterIsUp) ? 'up' : 'down';
  }

  function formatDuration(ms) {
    if (isNil(ms) || isNaN(ms)) { return DASH; }
    var value = Math.max(0, Number(ms));
    if (value < 1000) { return Math.round(value) + ' ms'; }
    var seconds = value / 1000;
    if (seconds < 60) { return (seconds < 10 ? seconds.toFixed(2) : seconds.toFixed(1)) + ' s'; }
    var minutes = Math.floor(seconds / 60);
    var restSeconds = Math.round(seconds - minutes * 60);
    if (restSeconds === 60) { minutes += 1; restSeconds = 0; }
    if (minutes < 60) { return minutes + 'm ' + pad2(restSeconds) + 's'; }
    var hours = Math.floor(minutes / 60);
    return hours + 'h ' + pad2(minutes - hours * 60) + 'm';
  }

  function pad2(value) { return value < 10 ? '0' + value : String(value); }

  function toDate(value) {
    if (isNil(value)) { return null; }
    if (value instanceof Date) { return isNaN(value.getTime()) ? null : value; }
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? null : parsed;
  }

  function formatDateTime(value) {
    var date = toDate(value);
    if (!date) { return DASH; }
    try {
      return date.toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: '2-digit',
        hour: '2-digit', minute: '2-digit'
      });
    } catch (err) {
      return date.toISOString();
    }
  }

  function formatTime(value) {
    var date = toDate(value);
    if (!date) { return DASH; }
    try {
      return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (err) {
      return date.toISOString();
    }
  }

  function formatDay(value) {
    var date = toDate(value);
    if (!date) { return DASH; }
    try {
      return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    } catch (err) {
      return date.toISOString().slice(0, 10);
    }
  }

  function timeAgo(value, now) {
    var date = toDate(value);
    if (!date) { return DASH; }
    var deltaSeconds = ((now ? toDate(now) : new Date()).getTime() - date.getTime()) / 1000;
    var future = deltaSeconds < 0;
    var seconds = Math.abs(deltaSeconds);
    var text;
    if (seconds < 45) { text = 'just now'; return future ? 'in a moment' : text; }
    if (seconds < 90) { text = 'a minute'; }
    else if (seconds < 3600) { text = Math.round(seconds / 60) + ' min'; }
    else if (seconds < 86400) { text = Math.round(seconds / 3600) + ' h'; }
    else if (seconds < 2592000) { text = Math.round(seconds / 86400) + ' d'; }
    else { return formatDay(date); }
    return future ? ('in ' + text) : (text + ' ago');
  }

  function shortId(value) {
    if (isNil(value)) { return DASH; }
    var text = String(value);
    return text.length > 8 ? text.slice(0, 8) : text;
  }

  function titleCase(value) {
    if (isNil(value) || value === '') { return ''; }
    var text = String(value).replace(/[_-]+/g, ' ');
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function pluralize(count, singular, plural) {
    return count === 1 ? singular : (plural || singular + 's');
  }

  /**
   * Pluralize a UI label with no count in hand — "All branches", not "All
   * branchs". Deliberately covers only the sibilant rule, because every label
   * in this dashboard is a facet name we choose ourselves; anything irregular
   * gets an explicit label at the call site rather than a bigger table here.
   */
  function pluralLabel(word) {
    var text = String(word === undefined || word === null ? '' : word);
    if (!text) { return text; }
    if (/(s|x|z|ch|sh)$/i.test(text)) { return text + 'es'; }
    if (/[^aeiou]y$/i.test(text)) { return text.slice(0, -1) + 'ies'; }
    return text + 's';
  }

  /* ------------------------------------------------------------- statuses */

  // Mirrors test_hub.domain.status. Kept in the same order the counts are
  // stacked in, so charts, legends and count bars all agree.
  var STATUS_ORDER = [
    'passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed',
    'running', 'queued', 'cancelled', 'unknown'
  ];
  var SUCCESS_STATUSES = { passed: true, xfailed: true };
  var FAILURE_STATUSES = { failed: true, error: true, xpassed: true };
  var STATUS_LABELS = {
    passed: 'Passed', failed: 'Failed', error: 'Error', skipped: 'Skipped',
    xfailed: 'xfailed', xpassed: 'xpassed', running: 'Running', queued: 'Queued',
    cancelled: 'Cancelled', unknown: 'Unknown'
  };

  function statusLabel(status) {
    return STATUS_LABELS[status] || titleCase(status || 'unknown');
  }

  function isSuccessful(status) { return SUCCESS_STATUSES[status] === true; }
  function isFailureish(status) { return FAILURE_STATUSES[status] === true; }

  /* -------------------------------------------------------------- emitter */

  function emitter() {
    var handlers = {};
    return {
      on: function (type, fn) {
        (handlers[type] = handlers[type] || []).push(fn);
        var self = this;
        return function () { self.off(type, fn); };
      },
      off: function (type, fn) {
        var list = handlers[type];
        if (!list) { return; }
        var index = list.indexOf(fn);
        if (index >= 0) { list.splice(index, 1); }
      },
      emit: function (type, payload) {
        var list = (handlers[type] || []).slice();
        for (var i = 0; i < list.length; i++) {
          try {
            list[i](payload);
          } catch (err) {
            reportError('handler for "' + type + '" failed', err);
          }
        }
      }
    };
  }

  /* --------------------------------------------------------------- timing */

  /**
   * Trailing debounce with a hard ceiling. A burst of 500 SSE events must
   * collapse into a handful of refetches, but a stream that never goes quiet
   * must still update, which is what `maxWait` is for.
   */
  function debounce(fn, wait, maxWait) {
    var timer = null;
    var firstCall = 0;
    return function () {
      var self = this;
      var args = arguments;
      var now = Date.now();
      if (!firstCall) { firstCall = now; }
      var run = function () {
        timer = null;
        firstCall = 0;
        fn.apply(self, args);
      };
      if (timer) { window.clearTimeout(timer); }
      if (maxWait && now - firstCall >= maxWait) {
        run();
        return;
      }
      var delay = wait;
      if (maxWait) { delay = Math.min(wait, Math.max(0, maxWait - (now - firstCall))); }
      timer = window.setTimeout(run, delay);
    };
  }

  /* ------------------------------------------------------------ comparing */

  function compare(a, b) {
    if (a === b) { return 0; }
    if (isNil(a)) { return 1; }
    if (isNil(b)) { return -1; }
    if (typeof a === 'number' && typeof b === 'number') { return a - b; }
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
  }

  function sortBy(items, key, direction) {
    var sign = direction === 'desc' ? -1 : 1;
    var read = typeof key === 'function' ? key : function (item) { return item[key]; };
    return items.slice().sort(function (a, b) {
      var result = compare(read(a), read(b));
      // Nulls sink to the bottom in both directions: a run with no start time
      // is missing data, not the oldest run in the list.
      if (result === 1 && isNil(read(a))) { return 1; }
      if (result === -1 && isNil(read(b))) { return -1; }
      return result * sign;
    });
  }

  function unique(values) {
    var seen = {};
    var out = [];
    for (var i = 0; i < values.length; i++) {
      var value = values[i];
      if (isNil(value) || value === '') { continue; }
      var key = String(value);
      if (seen[key]) { continue; }
      seen[key] = true;
      out.push(value);
    }
    return out;
  }

  function deepFreeze(value) {
    if (!value || typeof value !== 'object' || Object.isFrozen(value)) { return value; }
    Object.freeze(value);
    var keys = Object.keys(value);
    for (var i = 0; i < keys.length; i++) { deepFreeze(value[keys[i]]); }
    return value;
  }

  /* --------------------------------------------------------------- routes */

  /**
   * Hash routing so that every view is linkable and reloadable, including from
   * a file:// snapshot where there is no server to answer a real path.
   *
   *   #/overview?team=payments   #/runs?status=failed&page=2
   *   #/runs/<uuid>              #/tests/<uuid>
   */
  function parseHash(hash) {
    var raw = String(hash === undefined ? window.location.hash : hash).replace(/^#/, '');
    if (raw.charAt(0) === '/') { raw = raw.slice(1); }
    var queryIndex = raw.indexOf('?');
    var path = queryIndex >= 0 ? raw.slice(0, queryIndex) : raw;
    var search = queryIndex >= 0 ? raw.slice(queryIndex + 1) : '';
    var segments = path.split('/').filter(function (part) { return part !== ''; }).map(decodeURIComponent);
    return { segments: segments, query: parseQuery(search), path: path };
  }

  function parseQuery(search) {
    var out = {};
    var parts = String(search || '').split('&');
    for (var i = 0; i < parts.length; i++) {
      if (!parts[i]) { continue; }
      var pair = parts[i].split('=');
      var key = safeDecode(pair[0]);
      if (!key) { continue; }
      out[key] = safeDecode(pair.slice(1).join('='));
    }
    return out;
  }

  function safeDecode(value) {
    try {
      return decodeURIComponent(String(value === undefined ? '' : value).replace(/\+/g, ' '));
    } catch (err) {
      return String(value === undefined ? '' : value);
    }
  }

  function buildQuery(params) {
    var parts = [];
    var keys = Object.keys(params || {}).sort();
    for (var i = 0; i < keys.length; i++) {
      var value = params[keys[i]];
      if (isNil(value) || value === '' || value === false) { continue; }
      parts.push(encodeURIComponent(keys[i]) + '=' + encodeURIComponent(String(value)));
    }
    return parts.join('&');
  }

  function buildHash(segments, params) {
    var path = (segments || []).map(function (part) { return encodeURIComponent(String(part)); }).join('/');
    var query = buildQuery(params);
    return '#/' + path + (query ? '?' + query : '');
  }

  function navigate(hash, options) {
    if (options && options.replace && window.history && window.history.replaceState) {
      window.history.replaceState(null, '', hash);
      window.dispatchEvent(new window.HashChangeEvent('hashchange'));
      return;
    }
    window.location.hash = hash;
  }

  TestHub.util = {
    DASH: DASH,
    STATUS_ORDER: STATUS_ORDER,
    theme: theme,
    storage: { get: storageGet, set: storageSet, remove: storageRemove },
    escapeHtml: escapeHtml,
    el: el,
    svg: svg,
    frag: frag,
    clear: clear,
    replace: replace,
    appendChild: appendChild,
    readCssVar: readCssVar,
    prefersReducedMotion: prefersReducedMotion,
    isNil: isNil,
    formatNumber: formatNumber,
    formatPercent: formatPercent,
    formatDelta: formatDelta,
    deltaDirection: deltaDirection,
    formatDuration: formatDuration,
    formatDateTime: formatDateTime,
    formatTime: formatTime,
    formatDay: formatDay,
    timeAgo: timeAgo,
    toDate: toDate,
    shortId: shortId,
    titleCase: titleCase,
    pluralize: pluralize,
    pluralLabel: pluralLabel,
    statusLabel: statusLabel,
    isSuccessful: isSuccessful,
    isFailureish: isFailureish,
    emitter: emitter,
    debounce: debounce,
    compare: compare,
    sortBy: sortBy,
    unique: unique,
    deepFreeze: deepFreeze,
    parseHash: parseHash,
    parseQuery: parseQuery,
    buildQuery: buildQuery,
    buildHash: buildHash,
    navigate: navigate,
    reportError: reportError
  };
})(window, document);
