/* Test Hub — the live timeline: every run as a bar on a clock that keeps moving.
 *
 * The overview's other charts answer for a window of days. This one answers
 * for the last minutes or hours, and it never stops: the time axis glides left
 * once a second, a run that starts lands as a new bar at the right-hand edge,
 * and that bar grows — stacked by status — while the run's results arrive. Left
 * open on a screen it reads like a ticker of everything every project ran,
 * from CI or from anywhere else.
 *
 * Four decisions shape it:
 *
 *  - One bar per *slot*, not per run. A slot is a fixed slice of time (a
 *    minute, on the hour view) aligned to the reader's wall clock, and every
 *    run that started inside it stacks into its bar. A pipeline that fans out
 *    six jobs at once is one taller bar rather than six drawn on top of each
 *    other; the readout lists the runs inside.
 *  - A bar sits where its runs *started*, and stays there. A run still going
 *    is a bar that grows in place while it drifts left with everything else —
 *    which is what a live run moving with time looks like.
 *  - Motion is a transform with a one-second linear transition, written once a
 *    second. Nothing is redrawn to move it: the clock costs one style write per
 *    bar per tick, the compositor does the rest, and the clock stops entirely
 *    while the tab is hidden.
 *  - The DOM is keyed and patched, never rebuilt, for the reasons the status
 *    bars in 40-components.js are: a bar that changes grows into its new height
 *    instead of blinking, and the bar under a keyboard user's focus survives.
 *
 * The y axis counts tests: how much ran in each slot, and how it went.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

  //: How often the clock moves. One second is smooth at every span: at fifteen
  //: minutes a slot crosses the chart in fifteen minutes, about a pixel a second.
  var TICK_MS = 1000;
  //: Room above the tallest bar for the live marker.
  var HEADROOM_PX = 14;
  //: The least a slot with any result in it is drawn, so a 3-test run beside a
  //: 3,000-test one is still a mark you can see and hover.
  var MIN_BAR_PX = 3;
  var MAX_BAR_PX = 24;
  var MAX_RUNS_LISTED = 4;
  //: The least room a clock label gets. "12:30 PM" is about 56px; closer than
  //: this and every other label is skipped (the gridlines stay).
  var MIN_LABEL_GAP_PX = 72;

  /* Slots and labels are aligned to the reader's wall clock, not to UTC: a
   * half-hour slot should start at :00 or :30 where the reader is, and in a
   * zone half an hour off UTC those are not the same instants. */
  function localOffset(ms) {
    return new Date(ms).getTimezoneOffset() * 60000;
  }

  function floorLocal(ms, step) {
    var offset = localOffset(ms);
    return Math.floor((ms - offset) / step) * step + offset;
  }

  function ceilLocal(ms, step) {
    var offset = localOffset(ms);
    return Math.ceil((ms - offset) / step) * step + offset;
  }

  /** 1, 2 or 5 times a power of ten: the step a y axis can be read in. */
  function niceStep(value) {
    if (!(value > 0)) { return 1; }
    var power = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    var fraction = value / power;
    var nice = fraction <= 1 ? 1 : (fraction <= 2 ? 2 : (fraction <= 5 ? 5 : 10));
    return Math.max(1, nice * power);
  }

  function clockLabel(ms, seconds) {
    var options = { hour: '2-digit', minute: '2-digit' };
    if (seconds) { options.second = '2-digit'; }
    return new Date(ms).toLocaleTimeString([], options);
  }

  /** Every status count of `runs`, added up — the slot's own RunCounts. */
  function sumCounts(runs) {
    var counts = {};
    for (var s = 0; s < util.STATUS_ORDER.length; s++) { counts[util.STATUS_ORDER[s]] = 0; }
    for (var r = 0; r < runs.length; r++) {
      var source = runs[r].counts || {};
      for (s = 0; s < util.STATUS_ORDER.length; s++) {
        var value = source[util.STATUS_ORDER[s]];
        if (typeof value === 'number' && isFinite(value) && value > 0) {
          counts[util.STATUS_ORDER[s]] += value;
        }
      }
    }
    return counts;
  }

  /**
   *   spec     {label}
   *   update   ({runs: RunSummary[], span: {key, label, ms, bin, tick}, error})
   *
   * Returns `{el, update, destroy}`. `destroy` must be called when the element
   * is dropped: the clock is an interval and two listeners on the window, and
   * none of them goes away with the node.
   */
  function create(spec) {
    var opts = spec || {};
    var classes = ui.statusBarClasses;
    var span = null;
    var runs = [];
    var error = null;
    var bins = Object.create(null);
    var ticks = Object.create(null);
    var yMax = 0;
    var yStep = 1;
    var yHeight = 0;
    var laidOutAt = 0;
    var timer = null;
    var hovered = null;
    var focused = null;
    var pointerX = null;
    var i;

    var legendItems = {};
    var legend = el('div', { className: 'legend tl__legend' });
    for (i = 0; i < classes.length; i++) {
      var legendItem = el('span', { className: 'legend__item' }, [
        el('span', { className: 'legend__swatch', dataset: { status: classes[i].key } }),
        el('span', { text: classes[i].label })
      ]);
      legendItem.hidden = true;
      legendItems[classes[i].key] = legendItem;
      legend.appendChild(legendItem);
    }

    var yaxis = el('div', { className: 'tl__yaxis', attrs: { 'aria-hidden': 'true' } });
    var hgrid = el('div', { className: 'tl__hgrid', attrs: { 'aria-hidden': 'true' } });
    var vgrid = el('div', { className: 'tl__vgrid', attrs: { 'aria-hidden': 'true' } });
    var nowLine = el('div', { className: 'tl__now', attrs: { 'aria-hidden': 'true' } },
      el('span', { className: 'tl__now-label', text: 'now' }));
    var cols = el('div', { className: 'tl__cols' });
    var message = el('div', { className: 'tl__msg' });
    var plot = el('div', { className: 'tl__plot' }, [hgrid, vgrid, cols, nowLine, message]);
    // The status bars' readout, reused: one hover vocabulary across the page.
    // Visual only, like theirs — every number in it is also in the bar's
    // accessible name and in the table below.
    var tip = el('div', { className: 'sbars__tip tl__tip', attrs: { 'aria-hidden': 'true' } });
    tip.hidden = true;
    var frame = el('div', { className: 'tl__frame' }, [yaxis, plot, tip]);
    var xaxis = el('div', { className: 'tl__xaxis', attrs: { 'aria-hidden': 'true' } });
    var caption = el('caption');
    var tbody = el('tbody');
    // Hidden inside a 1px box rather than hidden itself. A table is at least as
    // wide as its caption, and `.sr-only` keeps text on one line — so a hidden
    // table with a sentence for a caption was 380px wide on its own and slid a
    // phone's page sideways. The box clips it; the table can be any width.
    var table = el('div', { className: 'sr-only' }, el('table', {}, [
      caption,
      el('thead', {}, el('tr', {}, ['Started', 'Runs', 'Results'].map(function (header) {
        return el('th', { text: header, attrs: { scope: 'col' } });
      }))),
      tbody
    ]));
    var root = el('div', {
      className: 'tl',
      attrs: { role: 'group', 'aria-label': opts.label || 'Test runs over time' }
    }, [legend, frame, xaxis, table]);

    /* ------------------------------------------------------------ geometry */

    // The right edge is one slot past now, so the slot the clock is in is
    // always whole on screen — its bar pops up at the edge, not half off it.
    function geometry(now) {
      var width = plot.clientWidth || 0;
      var slot = span ? width * span.bin / span.ms : 0;
      return {
        width: width,
        height: plot.clientHeight || 0,
        end: now + (span ? span.bin : 0),
        start: now + (span ? span.bin - span.ms : 0),
        slot: slot,
        bar: Math.max(2, Math.min(MAX_BAR_PX, Math.round(slot * 0.7), Math.floor(slot - 2))),
        scale: span ? width / span.ms : 0
      };
    }

    function xOf(ms, g) { return (ms - g.start) * g.scale; }

    /* ---------------------------------------------------------------- bins */

    function slotLabel(bin) {
      var seconds = span.bin < 60000;
      return clockLabel(bin.at, seconds) + '–' + clockLabel(bin.at + span.bin, seconds);
    }

    function binLabel(bin) {
      var count = bin.runs.length;
      return 'Started ' + slotLabel(bin) + ', ' + util.formatNumber(count) + ' ' +
        util.pluralize(count, 'run') + ': ' + ui.statusBarSentence(bin.tally) +
        '. Opens the newest run.';
    }

    function createBin(at) {
      var segments = {};
      var bar = el('div', { className: 'tl__bar' });
      for (var k = 0; k < classes.length; k++) {
        var segment = el('span', { className: 'tl__seg', dataset: { status: classes[k].key } });
        segment.hidden = true;
        segments[classes[k].key] = segment;
        bar.appendChild(segment);
      }
      var bin = {
        key: String(at),
        at: at,
        runs: [],
        tally: null,
        live: false,
        segments: segments,
        bar: bar,
        // The whole slot, top to bottom, is the target: a 3px bar is not a
        // thing anybody should have to hit dead-centre.
        col: el('div', {
          className: 'tl__col',
          attrs: { role: 'link', tabindex: '0' }
        }, [el('span', { className: 'tl__dot' }), bar])
      };
      bin.col.addEventListener('pointerenter', function (event) {
        hovered = bin.key;
        pointerX = event.clientX;
        showTip();
      });
      bin.col.addEventListener('pointermove', function (event) {
        if (hovered !== bin.key) { return; }
        pointerX = event.clientX;
        placeTip();
      });
      bin.col.addEventListener('pointerleave', function () {
        if (hovered === bin.key) { hovered = null; showTip(); }
      });
      bin.col.addEventListener('focus', function () { focused = bin.key; showTip(); });
      bin.col.addEventListener('blur', function () {
        if (focused === bin.key) { focused = null; showTip(); }
      });
      bin.col.addEventListener('click', function () { openNewest(bin); });
      bin.col.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openNewest(bin);
        }
      });
      bins[bin.key] = bin;
      return bin;
    }

    function patchBin(bin, binRuns) {
      bin.runs = binRuns;
      bin.tally = ui.statusBarTally(sumCounts(binRuns));
      bin.live = false;
      for (var r = 0; r < binRuns.length; r++) {
        if (binRuns[r].is_live) { bin.live = true; }
      }
      for (var k = 0; k < classes.length; k++) {
        var value = bin.tally.values[classes[k].key];
        var segment = bin.segments[classes[k].key];
        if (segment.hidden !== !value) { segment.hidden = !value; }
        segment.style.flexGrow = String(value);
      }
      bin.col.classList.toggle('tl__col--live', bin.live);
      var label = binLabel(bin);
      if (bin.col.getAttribute('aria-label') !== label) { bin.col.setAttribute('aria-label', label); }
    }

    function removeBin(key) {
      var bin = bins[key];
      if (bin.col.parentNode) { bin.col.parentNode.removeChild(bin.col); }
      delete bins[key];
      if (hovered === key) { hovered = null; }
      if (focused === key) { focused = null; }
    }

    function openNewest(bin) {
      if (bin.runs.length) { util.navigate(util.buildHash(['runs', bin.runs[0].id])); }
    }

    /** Fold the runs into slots, dropping what has already left the chart. */
    function rebuild(now) {
      var g = geometry(now);
      var next = Object.create(null);
      for (var r = 0; r < runs.length; r++) {
        var started = util.toDate(runs[r].started_at);
        if (!started) { continue; }
        var at = floorLocal(started.getTime(), span.bin);
        if (at + span.bin <= g.start) { continue; }
        var key = String(at);
        if (!next[key]) { next[key] = { at: at, runs: [] }; }
        next[key].runs.push(runs[r]);
      }
      var keys = Object.keys(bins);
      for (var k = 0; k < keys.length; k++) {
        if (!next[keys[k]]) { removeBin(keys[k]); }
      }
      keys = Object.keys(next);
      for (k = 0; k < keys.length; k++) {
        // Newest first, by instant rather than by string: two timestamps with
        // different fractional digits do not sort as text.
        var slotRuns = util.sortBy(next[keys[k]].runs, startedAt, 'desc');
        patchBin(bins[keys[k]] || createBin(next[keys[k]].at), slotRuns);
      }
    }

    function startedAt(run) {
      var date = util.toDate(run.started_at);
      return date ? date.getTime() : null;
    }

    /* --------------------------------------------------------------- layout */

    function layoutY(g) {
      util.clear(yaxis);
      util.clear(hgrid);
      var usable = g.height - HEADROOM_PX;
      for (var value = 0; value <= yMax + 1e-9; value += yStep) {
        var bottom = (value / yMax) * usable;
        var label = el('span', { text: util.formatNumber(value) });
        label.style.bottom = bottom.toFixed(1) + 'px';
        yaxis.appendChild(label);
        // The baseline is the plot's own border; a gridline on it is a
        // second line drawn on the first.
        if (value > 0) {
          var line = el('span');
          line.style.bottom = bottom.toFixed(1) + 'px';
          hgrid.appendChild(line);
        }
      }
    }

    function layoutTicks(g) {
      var wanted = Object.create(null);
      // One step of slack on the left, so a label leaves by sliding off the
      // edge rather than vanishing while half of it is still showing.
      for (var t = ceilLocal(g.start - span.tick, span.tick); t <= g.end; t += span.tick) {
        wanted[String(t)] = t;
      }
      var keys = Object.keys(ticks);
      for (var k = 0; k < keys.length; k++) {
        if (wanted[keys[k]] !== undefined) { continue; }
        xaxis.removeChild(ticks[keys[k]].label);
        vgrid.removeChild(ticks[keys[k]].line);
        delete ticks[keys[k]];
      }
      // On a narrow chart only every n-th label is shown, picked by its place
      // on the clock rather than on the screen, so the same labels stay up as
      // the axis slides instead of hopping from one tick to the next.
      var every = Math.max(1, Math.ceil(MIN_LABEL_GAP_PX / (span.tick * g.scale)));
      keys = Object.keys(wanted);
      for (k = 0; k < keys.length; k++) {
        var tick = ticks[keys[k]];
        if (!tick) {
          tick = ticks[keys[k]] = {
            label: el('span', { text: clockLabel(wanted[keys[k]], span.tick < 60000) }),
            line: el('span')
          };
          xaxis.appendChild(tick.label);
          vgrid.appendChild(tick.line);
        }
        var x = xOf(wanted[keys[k]], g);
        var shown = Math.round(wanted[keys[k]] / span.tick) % every === 0;
        if (tick.label.hidden !== !shown) { tick.label.hidden = !shown; }
        tick.line.style.transform = 'translateX(' + x.toFixed(2) + 'px)';
        tick.label.style.transform = 'translateX(' + x.toFixed(2) + 'px) translateX(-50%)';
      }
    }

    /**
     * Put every bar, label and line where the clock says. `animate` is false
     * for the first draw and after a gap — a hidden tab, a resize, a new span —
     * so the chart jumps to the right place instead of gliding across the
     * whole width to get there.
     */
    function layout(animate) {
      if (!span) { return; }
      var now = Date.now();
      var g = geometry(now);
      if (!g.width || !g.height) { return; }
      var still = !animate || now - laidOutAt > TICK_MS * 2.5;
      laidOutAt = now;
      if (still) { root.classList.add('tl--still'); }

      var visible = [];
      var keys = Object.keys(bins);
      var k;
      for (k = 0; k < keys.length; k++) {
        if (xOf(bins[keys[k]].at + span.bin, g) < 0) {
          removeBin(keys[k]);
        } else {
          visible.push(bins[keys[k]]);
        }
      }

      var most = 0;
      for (k = 0; k < visible.length; k++) { most = Math.max(most, visible[k].tally.total); }
      var step = niceStep(Math.max(most, 4) / 4);
      var top = Math.max(step, Math.ceil(Math.max(most, 1) / step) * step);
      if (top !== yMax || step !== yStep || g.height !== yHeight) {
        yMax = top;
        yStep = step;
        yHeight = g.height;
        layoutY(g);
      }

      var usable = g.height - HEADROOM_PX;
      var fresh = [];
      for (k = 0; k < visible.length; k++) {
        var bin = visible[k];
        // Never past the edge, even when this clock runs a little behind the
        // server's and a run seems to have started in the future.
        var x = Math.min(xOf(bin.at, g), g.width - g.slot);
        var height = bin.tally.total
          ? Math.max(MIN_BAR_PX, (bin.tally.total / yMax) * usable)
          : 0;
        bin.col.style.width = g.slot.toFixed(2) + 'px';
        bin.col.style.transform = 'translateX(' + x.toFixed(2) + 'px)';
        bin.bar.style.width = g.bar + 'px';
        if (!bin.col.parentNode) {
          bin.bar.style.height = '0px';
          cols.appendChild(bin.col);
          fresh.push({ bin: bin, height: height });
        } else {
          bin.bar.style.height = height.toFixed(1) + 'px';
        }
      }
      layoutTicks(g);
      nowLine.style.transform = 'translateX(' + xOf(now, g).toFixed(2) + 'px)';

      // Commit the positions before transitions come back on, then let the new
      // bars rise: a run that just started pops up from the baseline.
      void plot.offsetWidth;
      if (still) { root.classList.remove('tl--still'); }
      for (k = 0; k < fresh.length; k++) {
        fresh[k].bin.bar.style.height = fresh[k].height.toFixed(1) + 'px';
      }
      syncMessage(visible.length);
      placeTip();
    }

    /* ------------------------------------------------------------- readout */

    function currentBin() {
      return (hovered !== null && bins[hovered]) || (focused !== null && bins[focused]) || null;
    }

    function showTip() {
      var bin = currentBin();
      if (!bin) {
        tip.hidden = true;
        return;
      }
      var listed = bin.runs.slice(0, MAX_RUNS_LISTED).map(function (run) {
        var parts = [run.project_key];
        if (run.trigger && run.trigger !== 'unknown') { parts.push(util.titleCase(run.trigger)); }
        if (run.is_live) { parts.push('live'); }
        return el('div', { className: 'tl__tip-run', text: parts.join(' · ') });
      });
      if (bin.runs.length > MAX_RUNS_LISTED) {
        listed.push(el('div', {
          className: 'tl__tip-run',
          text: 'and ' + util.formatNumber(bin.runs.length - MAX_RUNS_LISTED) + ' more'
        }));
      }
      var cells = [];
      for (var k = 0; k < classes.length; k++) {
        var value = bin.tally.values[classes[k].key];
        if (!value) { continue; }
        cells.push(
          el('span', { className: 'sbars__key', dataset: { status: classes[k].key } }),
          el('span', { className: 'sbars__tip-value', text: util.formatNumber(value) }),
          el('span', { className: 'sbars__tip-label', text: classes[k].label }),
          el('span', {
            className: 'sbars__tip-share',
            text: util.formatPercent((value / bin.tally.total) * 100, 1)
          })
        );
      }
      var count = bin.runs.length;
      util.replace(tip, [
        el('div', { className: 'sbars__tip-title', text: slotLabel(bin) }),
        el('div', {
          className: 'sbars__tip-sub',
          text: util.formatNumber(count) + ' ' + util.pluralize(count, 'run') + ' started · ' +
            util.timeAgo(bin.at)
        }),
        el('div', { className: 'tl__tip-runs' }, listed),
        cells.length
          ? el('div', { className: 'sbars__tip-grid' }, cells)
          : el('div', { className: 'sbars__tip-sub', text: 'No results reported yet' })
      ]);
      tip.hidden = false;
      placeTip();
    }

    function placeTip() {
      var bin = currentBin();
      if (tip.hidden || !bin || !bin.col.parentNode) { return; }
      var frameBox = frame.getBoundingClientRect();
      var colBox = bin.col.getBoundingClientRect();
      var barBox = bin.bar.getBoundingClientRect();
      var width = tip.offsetWidth;
      var height = tip.offsetHeight;
      var anchor = hovered === bin.key && pointerX !== null
        ? pointerX
        : colBox.left + colBox.width / 2;
      var left = Math.max(0, Math.min(anchor - frameBox.left - width / 2, frameBox.width - width));
      var top = barBox.top - frameBox.top - height - 10;
      // Above the bar unless that would leave the viewport; then below the plot.
      if (frameBox.top + top < 0) { top = colBox.bottom - frameBox.top + 8; }
      tip.style.left = Math.round(left) + 'px';
      tip.style.top = Math.round(top) + 'px';
    }

    /* ---------------------------------------------------------- the rest */

    function syncMessage(visibleCount) {
      var text = '';
      if (error) {
        text = 'The timeline did not load. It will try again on the next update.';
      } else if (!visibleCount) {
        text = 'Nothing started in the last ' + span.label +
          '. A run appears here the moment it reports.';
      }
      if (message.textContent !== text) { message.textContent = text; }
      message.hidden = !text;
    }

    function syncLegend() {
      var present = {};
      var keys = Object.keys(bins);
      for (var k = 0; k < keys.length; k++) {
        for (var c = 0; c < classes.length; c++) {
          if (bins[keys[k]].tally.values[classes[c].key]) { present[classes[c].key] = true; }
        }
      }
      for (var j = 0; j < classes.length; j++) {
        var hide = !present[classes[j].key];
        if (legendItems[classes[j].key].hidden !== hide) { legendItems[classes[j].key].hidden = hide; }
      }
    }

    function syncTable() {
      caption.textContent = 'Test runs started in the last ' + span.label + ', by ' +
        (span.bin < 60000 ? util.formatNumber(span.bin / 1000) + ' seconds' :
          util.formatNumber(span.bin / 60000) + ' minutes');
      var ordered = util.sortBy(Object.keys(bins).map(function (key) { return bins[key]; }), 'at', 'desc');
      util.replace(tbody, ordered.map(function (bin) {
        return el('tr', {}, [
          el('th', { text: slotLabel(bin), attrs: { scope: 'row' } }),
          el('td', { text: bin.runs.map(function (run) { return run.project_key; }).join(', ') }),
          el('td', { text: ui.statusBarSentence(bin.tally) })
        ]);
      }));
    }

    function clear() {
      var keys = Object.keys(bins);
      for (var k = 0; k < keys.length; k++) { removeBin(keys[k]); }
      keys = Object.keys(ticks);
      for (k = 0; k < keys.length; k++) {
        xaxis.removeChild(ticks[keys[k]].label);
        vgrid.removeChild(ticks[keys[k]].line);
        delete ticks[keys[k]];
      }
      laidOutAt = 0;
    }

    function start() {
      if (timer || document.hidden) { return; }
      timer = window.setInterval(function () { layout(true); }, TICK_MS);
    }

    function stop() {
      if (timer) {
        window.clearInterval(timer);
        timer = null;
      }
    }

    function onVisibility() {
      if (document.hidden) {
        stop();
      } else {
        layout(false);
        start();
      }
    }

    var onResize = util.debounce(function () { layout(false); }, 150, 600);
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('resize', onResize);

    function update(next) {
      if (!next || !next.span) { return; }
      if (!span || span.key !== next.span.key) {
        clear();
        span = next.span;
      }
      runs = next.runs || [];
      error = next.error || null;
      rebuild(Date.now());
      syncLegend();
      syncTable();
      layout(true);
      showTip();
      start();
    }

    return {
      el: root,
      update: update,
      destroy: function () {
        stop();
        document.removeEventListener('visibilitychange', onVisibility);
        window.removeEventListener('resize', onResize);
      }
    };
  }

  ui.timeline = create;
})(window, document);
