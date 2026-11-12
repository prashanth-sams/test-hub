/* Test Hub — the window every portfolio page is read through.
 *
 * The overview, analytics, suites, metrics and evidence pages all answer "over
 * what stretch of time?", and they all read the answer from one URL key,
 * `window`:
 *
 *   1h 6h 24h 7d 14d 30d 90d               a length that ends now
 *   2026-11-10T08:00Z..2026-11-11T18:30Z   a fixed range
 *   2026-11-10T08:00Z..                    a range that runs on to now
 *
 * One key, so a filter bar that changes one key at a time can change it, and a
 * link carries the whole window or none of it. Instants are UTC with a `Z`: a
 * link opened in another time zone means the same hours, and the API refuses a
 * timestamp without an offset rather than guess at it. The key used to be
 * `window_days=N`; such links still open, as `Nd`.
 *
 * Everything here is arithmetic and wording — no DOM, no network — so the
 * provider can turn a window into API parameters, the filter bar can draw it
 * and the pages can say it in words, all from one definition.
 */
(function (window) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};

  var MINUTE = 60000;
  var HOUR = 60 * MINUTE;
  var DAY = 24 * HOUR;

  //: GET /overview's own ceilings (api.routes.queries): a window the server
  //: would refuse is never sent, it is replaced by the default instead.
  var MAX_DAYS = 365;
  var MAX_HOURS = 366 * 24;
  var MAX_SPAN_MS = 366 * DAY;

  var SEPARATOR = '..';
  var DEFAULT_KEY = '30d';

  function lengthOf(amount, unit) {
    var hours = unit === 'h' ? amount : null;
    var days = unit === 'd' ? amount : null;
    return {
      key: amount + unit,
      hours: hours,
      days: days,
      ms: hours ? hours * HOUR : days * DAY,
      // "in the last hour", not "in the last 1 hours".
      words: hours === 1 ? 'hour' : (hours ? hours + ' hours' : (days === 1 ? 'day' : days + ' days')),
      count: hours ? amount + (amount === 1 ? ' hour' : ' hours') : amount + (amount === 1 ? ' day' : ' days')
    };
  }

  //: What the filter bar offers, shortest first.
  var PRESETS = [
    lengthOf(1, 'h'), lengthOf(6, 'h'), lengthOf(24, 'h'),
    lengthOf(7, 'd'), lengthOf(14, 'd'), lengthOf(30, 'd'), lengthOf(90, 'd')
  ];
  var PRESET_KEYS = Object.create(null);
  for (var p = 0; p < PRESETS.length; p++) { PRESET_KEYS[PRESETS[p].key] = PRESETS[p]; }

  /* ------------------------------------------------------------- parsing */

  var LENGTH = /^(\d{1,4})([hd])$/;
  var INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?(Z|[+-]\d{2}:\d{2})$/;

  function parseInstant(text) {
    if (!INSTANT.test(text)) { return null; }
    var date = new Date(text);
    return isNaN(date.getTime()) ? null : date;
  }

  /** `2026-11-10T08:00Z`: UTC, to the minute unless there are seconds. */
  function formatInstant(date) {
    var iso = date.toISOString();
    return iso.slice(16) === ':00.000Z' ? iso.slice(0, 16) + 'Z' : iso.slice(0, 19) + 'Z';
  }

  function lengthWindow(length) {
    return {
      kind: 'length', value: length.key, length: length,
      preset: !!PRESET_KEYS[length.key], from: null, to: null
    };
  }

  function rangeWindow(from, to) {
    return {
      kind: 'range',
      value: formatInstant(from) + SEPARATOR + (to ? formatInstant(to) : ''),
      length: null, preset: false, from: from, to: to || null
    };
  }

  /** Why `from`..`to` cannot be a window, in words for the picker; null if it can. */
  function rangeProblem(from, to, now) {
    var clock = typeof now === 'number' ? now : Date.now();
    if (!from) { return 'Pick when the range starts.'; }
    if (from.getTime() >= clock) { return 'The start cannot be in the future.'; }
    if (to && to.getTime() <= from.getTime()) { return 'The end has to be after the start.'; }
    if ((to ? to.getTime() : clock) - from.getTime() > MAX_SPAN_MS) {
      return 'A range can be at most 366 days long.';
    }
    return null;
  }

  /**
   * The window `value` names. Anything that is not one — a typo, a range the
   * server would refuse — is the default rather than an error: a hand-edited
   * URL should land on a working page, not a broken one.
   */
  function parse(value, now) {
    var text = value === undefined || value === null ? '' : String(value).trim();
    var match = LENGTH.exec(text);
    if (match) {
      var amount = Number(match[1]);
      var limit = match[2] === 'h' ? MAX_HOURS : MAX_DAYS;
      if (amount >= 1 && amount <= limit) {
        return lengthWindow(PRESET_KEYS[text] || lengthOf(amount, match[2]));
      }
    }
    var cut = text.indexOf(SEPARATOR);
    if (cut > 0) {
      var from = parseInstant(text.slice(0, cut));
      var tail = text.slice(cut + SEPARATOR.length);
      var to = tail ? parseInstant(tail) : null;
      if (from && (to || !tail) && rangeProblem(from, to, now) === null) {
        return rangeWindow(from, to);
      }
    }
    return lengthWindow(PRESET_KEYS[DEFAULT_KEY]);
  }

  /** The window a route's query names: `window`, or a link's `window_days`. */
  function fromQuery(query) {
    var source = query || {};
    if (source.window) { return parse(source.window); }
    if (/^\d+$/.test(String(source.window_days || ''))) { return parse(source.window_days + 'd'); }
    return parse('');
  }

  /** A fixed range, or one running on to now when `to` is null. */
  function range(from, to) { return rangeWindow(from, to); }

  /** What an overview's numbers are for: the window it was asked with. */
  function of(overview) {
    if (overview && overview.window && overview.window.kind) { return overview.window; }
    return parse(overview && overview.window_days ? overview.window_days + 'd' : '');
  }

  /* ---------------------------------------------------------- arithmetic */

  /** The window's first and last instant, in ms, for a clock reading `now`. */
  function bounds(win, now) {
    var clock = typeof now === 'number' ? now : Date.now();
    if (win.kind === 'range') {
      return { start: win.from.getTime(), end: win.to ? win.to.getTime() : clock, fixedEnd: !!win.to };
    }
    return { start: clock - win.length.ms, end: clock, fixedEnd: false };
  }

  /** The GET /overview parameters that ask for `win`. */
  function apiParams(win) {
    if (win.kind === 'range') {
      var params = { since: win.from.toISOString() };
      if (win.to) { params.until = win.to.toISOString(); }
      return params;
    }
    return win.length.hours ? { window_hours: win.length.hours } : { window_days: win.length.days };
  }

  /* SqlRunRepository.trend_step, transcribed: a filtered slice is bucketed in
   * the browser, and its chart must look like the server's. Two days or less
   * is bucketed by the clock — the narrowest step that keeps it to 24 points —
   * and anything longer by the UTC date, widening past 60 points. */
  var CLOCK_LIMIT_MS = 48 * HOUR;
  var CLOCK_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200];
  var CLOCK_BUCKETS = 24;
  var MAX_TREND_BUCKETS = 60;

  /** Seconds per trend bucket, for a window `spanMs` long. */
  function trendStep(spanMs) {
    if (spanMs <= CLOCK_LIMIT_MS) {
      var seconds = Math.max(1, spanMs / 1000);
      for (var i = 0; i < CLOCK_STEPS.length; i++) {
        if (seconds <= CLOCK_STEPS[i] * CLOCK_BUCKETS) { return CLOCK_STEPS[i]; }
      }
      return CLOCK_STEPS[CLOCK_STEPS.length - 1];
    }
    var days = Math.ceil(spanMs / DAY);
    return (days <= MAX_TREND_BUCKETS ? 1 : Math.ceil(days / MAX_TREND_BUCKETS)) * 86400;
  }

  /* ------------------------------------------------------------- wording */

  function pad2(value) { return value < 10 ? '0' + value : String(value); }

  function isMidnight(date) {
    return date.getHours() === 0 && date.getMinutes() === 0 &&
      date.getSeconds() === 0 && date.getMilliseconds() === 0;
  }

  function addDays(date, amount) {
    // By the calendar, not by 24 hours: across a DST change a day is 23 or 25.
    return new Date(date.getFullYear(), date.getMonth(), date.getDate() + amount);
  }

  function formatWith(date, options, fallback) {
    try {
      return date.toLocaleString(undefined, options);
    } catch (err) {
      return fallback;
    }
  }

  function dayOptions(date) {
    var options = { month: 'short', day: 'numeric' };
    if (date.getFullYear() !== new Date().getFullYear()) { options.year = 'numeric'; }
    return options;
  }

  function formatDayOf(date) {
    return formatWith(date, dayOptions(date), formatInstant(date).slice(0, 10));
  }

  // `numeric`, not `2-digit`: "6:00 PM" where the locale says it that way,
  // "18:00" where it does not — never "06:00 PM".
  function formatClock(date) {
    return formatWith(date, { hour: 'numeric', minute: '2-digit' },
      pad2(date.getHours()) + ':' + pad2(date.getMinutes()));
  }

  /** "Nov 10, 08:00" — or just "Nov 10" for a midnight, which is a day. */
  function formatMoment(date) {
    if (isMidnight(date)) { return formatDayOf(date); }
    var options = dayOptions(date);
    options.hour = 'numeric';
    options.minute = '2-digit';
    return formatWith(date, options, formatInstant(date));
  }

  /**
   * A fixed range, as briefly as it can be said without being vague:
   * "Nov 11" for one whole day, "Nov 2 – Nov 8" for whole days (the last one
   * included), "Nov 11, 08:00 – 18:30" inside one day, and both ends in full
   * otherwise.
   */
  function formatSpan(from, to) {
    if (isMidnight(from) && isMidnight(to)) {
      var last = addDays(to, -1);
      if (last.getTime() <= from.getTime()) { return formatDayOf(from); }
      return formatDayOf(from) + ' – ' + formatDayOf(last);
    }
    if (from.toDateString() === to.toDateString()) {
      return formatMoment(from) + ' – ' + formatClock(to);
    }
    return formatMoment(from) + ' – ' + formatMoment(to);
  }

  /** A card hint: "Last 6 hours", "Since Nov 10, 08:00", "Nov 2 – Nov 8". */
  function title(win) {
    if (win.kind === 'length') { return 'Last ' + win.length.words; }
    if (!win.to) { return 'Since ' + formatMoment(win.from); }
    return formatSpan(win.from, win.to);
  }

  /** The end of a sentence: "… in the last 6 hours", "… since Nov 10". */
  function during(win) {
    if (win.kind === 'length') { return 'in the last ' + win.length.words; }
    if (!win.to) { return 'since ' + formatMoment(win.from); }
    return 'in ' + formatSpan(win.from, win.to);
  }

  /** A short count's tail: "12 runs in 6 hours", "12 runs in this range". */
  function briefly(win) {
    if (win.kind === 'length') { return 'in ' + win.length.count; }
    return win.to ? 'in this range' : 'since ' + formatMoment(win.from);
  }

  /** A subtitle or hint: "Across the last 6 hours", "Across Nov 2 – Nov 8". */
  function across(win) {
    if (win.kind === 'length') { return 'Across the last ' + win.length.words; }
    if (!win.to) { return 'Since ' + formatMoment(win.from); }
    return 'Across ' + formatSpan(win.from, win.to);
  }

  /** The custom-range button, when a range (or an unoffered length) is set. */
  function label(win) {
    if (win.kind === 'length') { return 'Last ' + win.length.words; }
    if (!win.to) { return 'Since ' + formatMoment(win.from); }
    return formatSpan(win.from, win.to);
  }

  var NAMED_STEPS = { 60: 'minute', 3600: 'hour', 86400: 'day', 604800: 'week' };

  /**
   * How a chart says its bucket: "by hour" / "per 15 minutes" for a title,
   * "per hour" for a description, and the sentence about empty buckets.
   */
  function bucketWords(seconds) {
    var step = Number(seconds) || 86400;
    var named = NAMED_STEPS[step];
    if (named) {
      return {
        by: 'by ' + named,
        per: 'per ' + named,
        gaps: named.charAt(0).toUpperCase() + named.slice(1) + 's with no runs are gaps, not zeroes'
      };
    }
    var amount = step < 3600 ? step / 60 : (step < 86400 ? step / 3600 : step / 86400);
    var unit = step < 3600 ? 'minute' : (step < 86400 ? 'hour' : 'day');
    return {
      by: 'per ' + amount + ' ' + unit + 's',
      per: 'per ' + amount + ' ' + unit + 's',
      gaps: amount + '-' + unit + ' slots with no runs are gaps, not zeroes'
    };
  }

  /**
   * A trend bucket's axis label. Daily buckets are UTC dates and are printed
   * as dates (in UTC, or a viewer west of Greenwich sees every day one day
   * early); clock buckets are instants and are printed as local time, with
   * the date in place of "00:00" so a day's boundary is visible on the axis.
   */
  function bucketLabel(bucket, seconds) {
    var text = String(bucket || '');
    if (!seconds || seconds >= 86400 || /^\d{4}-\d{2}-\d{2}$/.test(text)) {
      var day = new Date(text.slice(0, 10) + 'T00:00:00Z');
      if (isNaN(day.getTime())) { return text; }
      return formatWith(day, { month: 'short', day: 'numeric', timeZone: 'UTC' }, text.slice(0, 10));
    }
    var date = new Date(text);
    if (isNaN(date.getTime())) { return text; }
    if (date.getHours() === 0 && date.getMinutes() === 0) { return formatDayOf(date); }
    return formatClock(date);
  }

  /* --------------------------------------------------------- the picker */

  /** Calendar ranges the picker offers in one click, in the viewer's zone. */
  function quickRanges(now) {
    var clock = new Date(typeof now === 'number' ? now : Date.now());
    var today = new Date(clock.getFullYear(), clock.getMonth(), clock.getDate());
    // Weeks start on Monday, as ISO 8601 has them.
    var monday = addDays(today, -((today.getDay() + 6) % 7));
    return [
      { key: 'today', label: 'Today', from: today, to: null },
      { key: 'yesterday', label: 'Yesterday', from: addDays(today, -1), to: today },
      { key: 'week', label: 'This week', from: monday, to: null },
      { key: 'last-week', label: 'Last week', from: addDays(monday, -7), to: monday }
    ];
  }

  /** `2026-11-10T08:00`, what an <input type="datetime-local"> holds: local time. */
  function toLocalInput(date) {
    return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) +
      'T' + pad2(date.getHours()) + ':' + pad2(date.getMinutes());
  }

  var LOCAL_INPUT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;

  /** The instant a datetime-local value means, in this browser's zone. */
  function fromLocalInput(text) {
    var match = LOCAL_INPUT.exec(String(text || ''));
    if (!match) { return null; }
    var date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]),
      Number(match[4]), Number(match[5]), Number(match[6] || 0));
    return isNaN(date.getTime()) ? null : date;
  }

  /** The viewer's time zone, by name where the browser knows it. */
  function zoneName() {
    try {
      var zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (zone) { return zone; }
    } catch (err) { /* fall through to the offset */ }
    var offset = -new Date().getTimezoneOffset();
    var sign = offset < 0 ? '-' : '+';
    offset = Math.abs(offset);
    return 'UTC' + sign + pad2(Math.floor(offset / 60)) + ':' + pad2(offset % 60);
  }

  TestHub.windows = {
    PRESETS: PRESETS,
    DEFAULT_KEY: DEFAULT_KEY,
    SEPARATOR: SEPARATOR,
    MAX_SPAN_MS: MAX_SPAN_MS,
    parse: parse,
    fromQuery: fromQuery,
    range: range,
    rangeProblem: rangeProblem,
    of: of,
    bounds: bounds,
    apiParams: apiParams,
    trendStep: trendStep,
    title: title,
    during: during,
    briefly: briefly,
    across: across,
    label: label,
    bucketWords: bucketWords,
    bucketLabel: bucketLabel,
    formatMoment: formatMoment,
    quickRanges: quickRanges,
    toLocalInput: toLocalInput,
    fromLocalInput: fromLocalInput,
    zoneName: zoneName
  };
})(window);
