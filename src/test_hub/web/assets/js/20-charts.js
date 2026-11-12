/* Test Hub — charts, hand-rolled in SVG (ADR-0001 D7).
 *
 * Five shapes cover the whole dashboard: a stacked bar, rate columns, range
 * bars, a donut and a sparkline. No library, no canvas, no build step —
 * a chart is a DOM tree like everything else, which means it inherits the
 * theme, scales with the container, and can be inspected in devtools.
 *
 * Three rules kept every chart honest here:
 *
 *  1. Colours are read from CSS custom properties with getComputedStyle, never
 *     hard-coded. A chart drawn before the theme attribute existed would bake
 *     in the wrong palette, which is why 00-util.js sets `data-theme` before
 *     first paint and why every chart re-renders on a theme change.
 *  2. Nothing is drawn from an interpolated string. Every node is created with
 *     createElementNS and every label is a text node, so a bucket named
 *     `</text><script>` is a label, not a bug.
 *  3. Every chart carries a <title>, an aria-label and a visually hidden table
 *     of the same numbers. A chart that only exists as pixels is a chart that
 *     does not exist for a screen reader, and "look at the graph" is not an
 *     answer anybody can act on.
 *
 * Gaps are gaps: a bucket with no runs is an empty slot, never a straight
 * segment joining the days on either side of a quiet weekend — which is why
 * the time series here are columns and bars, not lines.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var svg = util.svg;
  var el = util.el;

  var registry = [];
  var uid = 0;

  function nextId(prefix) {
    uid += 1;
    return 'th-' + prefix + '-' + uid;
  }

  function color(name, fallback) {
    return util.readCssVar(name, fallback);
  }

  function containerWidth(container, fallback) {
    var width = container && container.clientWidth ? container.clientWidth : 0;
    if (!width && container && container.parentNode && container.parentNode.clientWidth) {
      width = container.parentNode.clientWidth;
    }
    return Math.max(240, Math.round(width || fallback || 640));
  }

  function register(container, spec) {
    for (var i = 0; i < registry.length; i++) {
      if (registry[i].container === container) {
        registry[i].spec = spec;
        return;
      }
    }
    registry.push({ container: container, spec: spec });
  }

  function redrawAll() {
    var live = [];
    for (var i = 0; i < registry.length; i++) {
      var entry = registry[i];
      if (!entry.container || !entry.container.isConnected) { continue; }
      live.push(entry);
      draw(entry.container, entry.spec, true);
    }
    registry = live;
  }

  function emptyState(container, message) {
    util.replace(container, el('div', { className: 'chart__empty', text: message || 'No data yet' }));
  }

  /**
   * The text alternative. Same numbers, same order, in a real table — so the
   * chart is navigable by a screen reader and copyable by anyone else.
   */
  function dataTable(caption, headers, rows) {
    var thead = el('thead', {}, el('tr', {}, headers.map(function (header) {
      return el('th', { text: header, attrs: { scope: 'col' } });
    })));
    var tbody = el('tbody', {}, rows.map(function (row) {
      return el('tr', {}, row.map(function (cell, index) {
        return index === 0
          ? el('th', { text: cell, attrs: { scope: 'row' } })
          : el('td', { text: cell });
      }));
    }));
    return el('table', { className: 'sr-only' }, [el('caption', { text: caption }), thead, tbody]);
  }

  function frame(spec, width, height) {
    var titleId = nextId('t');
    var descId = nextId('d');
    var node = svg('svg', {
      width: width,
      height: height,
      viewBox: '0 0 ' + width + ' ' + height,
      role: 'img',
      'aria-labelledby': titleId + ' ' + descId,
      focusable: 'false'
    });
    node.appendChild(svg('title', { id: titleId }, spec.title || 'Chart'));
    node.appendChild(svg('desc', { id: descId }, spec.description || spec.title || ''));
    return node;
  }

  /* =========================================================== stacked bar */

  function drawStackedBar(container, spec) {
    var buckets = spec.buckets || [];
    var keys = spec.keys || [];
    if (!buckets.length || !keys.length) {
      emptyState(container, spec.emptyMessage);
      return;
    }

    var width = containerWidth(container, 720);
    var height = spec.height || 180;
    var padding = { top: 12, right: 14, bottom: 26, left: 46 };
    var plotWidth = Math.max(10, width - padding.left - padding.right);
    var plotHeight = Math.max(10, height - padding.top - padding.bottom);

    var totals = buckets.map(function (bucket) {
      var sum = 0;
      for (var i = 0; i < keys.length; i++) { sum += Math.max(0, bucket.values[keys[i].key] || 0); }
      return sum;
    });
    var max = Math.max.apply(null, totals.concat([1]));

    var node = frame(spec, width, height);
    var gridColor = color('--chart-grid', '#f0f2f6');
    var axisColor = color('--chart-axis', '#666666');
    var ticks = 3;
    var t;
    for (t = 0; t <= ticks; t++) {
      var value = (max * t) / ticks;
      var y = padding.top + plotHeight - (value / max) * plotHeight;
      node.appendChild(svg('line', {
        x1: padding.left, x2: width - padding.right, y1: y, y2: y,
        stroke: gridColor, 'stroke-width': 1, 'shape-rendering': 'crispEdges'
      }));
      node.appendChild(svg('text', {
        x: padding.left - 8, y: y + 3.5, 'text-anchor': 'end', fill: axisColor, 'font-size': 11.5
      }, util.formatNumber(Math.round(value))));
    }

    var slot = plotWidth / buckets.length;
    var barWidth = Math.max(3, Math.min(spec.maxBarWidth || 26, slot - 4));
    var maxLabels = Math.max(2, Math.floor(plotWidth / 78));
    var step = Math.max(1, Math.ceil(buckets.length / maxLabels));

    for (var b = 0; b < buckets.length; b++) {
      var bucket = buckets[b];
      var x = padding.left + slot * b + (slot - barWidth) / 2;
      var cursor = padding.top + plotHeight;
      var total = totals[b];
      for (var k = 0; k < keys.length; k++) {
        var amount = Math.max(0, bucket.values[keys[k].key] || 0);
        if (!amount) { continue; }
        var segmentHeight = (amount / max) * plotHeight;
        cursor -= segmentHeight;
        var rect = svg('rect', {
          x: round1(x), y: round1(cursor), width: round1(barWidth), height: round1(segmentHeight),
          fill: color(keys[k].color, '#cfd6e0'), rx: 1.5
        });
        rect.appendChild(svg('title', {}, bucket.label + ' · ' + keys[k].label + ': ' +
          util.formatNumber(amount)));
        node.appendChild(rect);
      }
      if (!total) {
        node.appendChild(svg('rect', {
          x: round1(x), y: round1(padding.top + plotHeight - 1), width: round1(barWidth), height: 1,
          fill: gridColor
        }));
      }
      if (b % step === 0 || b === buckets.length - 1) {
        node.appendChild(svg('text', {
          x: round1(x + barWidth / 2), y: height - 8, 'text-anchor': 'middle',
          fill: axisColor, 'font-size': 11.5
        }, bucket.label));
      }
    }

    var headers = [spec.xLabel || 'Bucket'];
    for (var h = 0; h < keys.length; h++) { headers.push(keys[h].label); }
    var rows = buckets.map(function (bucket) {
      var row = [bucket.label];
      for (var i = 0; i < keys.length; i++) {
        row.push(util.formatNumber(bucket.values[keys[i].key] || 0));
      }
      return row;
    });

    util.replace(container, [node, dataTable(spec.title || 'Volume', headers, rows)]);
  }

  /* =========================================================== hover layer */

  /**
   * The readout the bucketed charts share: one tooltip over the bucket under
   * the pointer, and the same readout from the keyboard. The whole chart is
   * one tab stop — the arrows move between buckets with data — rather than a
   * stop per bar, which on a 90-day window is 46 presses to get past it.
   *
   * `slots[i]` is `{x, width, title, rows: [{value, label, key?}]}` or null
   * for a bucket with nothing in it. The table under every chart still holds
   * all of it: the tooltip enhances, it never gates.
   */
  function hoverLayer(container, node, slots, plot) {
    var band = svg('rect', {
      x: 0, y: plot.top, width: 0, height: plot.height,
      fill: color('--chart-band', '#f1f3f7'), opacity: 0
    });
    node.insertBefore(band, node.firstChild.nextSibling.nextSibling);
    var tip = el('div', { className: 'chart__tip', attrs: { role: 'status', 'aria-live': 'polite' } });
    tip.hidden = true;
    var active = -1;

    function show(index) {
      var slot = slots[index];
      if (!slot) { hide(); return; }
      active = index;
      band.setAttribute('x', String(round1(slot.x)));
      band.setAttribute('width', String(round1(slot.width)));
      band.setAttribute('opacity', '1');
      util.clear(tip);
      tip.appendChild(el('div', { className: 'chart__tip-title', text: slot.title }));
      for (var r = 0; r < slot.rows.length; r++) {
        var row = slot.rows[r];
        tip.appendChild(el('div', { className: 'chart__tip-row' }, [
          el('span', { className: 'chart__tip-key', dataset: { key: row.key || 'none' } }),
          el('strong', { text: row.value }),
          el('span', { text: row.label })
        ]));
      }
      tip.hidden = false;
      var width = container.clientWidth || plot.width;
      var left = slot.x + slot.width / 2 - tip.offsetWidth / 2;
      tip.style.left = Math.max(0, Math.min(width - tip.offsetWidth, left)) + 'px';
      tip.style.top = Math.max(0, plot.top - tip.offsetHeight - 6) + 'px';
    }

    function hide() {
      active = -1;
      band.setAttribute('opacity', '0');
      tip.hidden = true;
    }

    function step(from, direction) {
      for (var i = from + direction; i >= 0 && i < slots.length; i += direction) {
        if (slots[i]) { return i; }
      }
      return from;
    }

    function lastWithData() { return step(slots.length, -1); }

    for (var i = 0; i < slots.length; i++) {
      (function (index) {
        var hit = svg('rect', {
          x: round1(plot.left + plot.slot * index), y: plot.top,
          width: round1(plot.slot), height: plot.height,
          fill: 'transparent'
        });
        hit.addEventListener('pointerenter', function () { show(index); });
        node.appendChild(hit);
      })(i);
    }

    node.setAttribute('tabindex', '0');
    node.addEventListener('pointerleave', function () {
      if (document.activeElement !== node) { hide(); }
    });
    node.addEventListener('focus', function () { show(active >= 0 ? active : lastWithData()); });
    node.addEventListener('blur', hide);
    node.addEventListener('keydown', function (event) {
      var start = active >= 0 ? active : lastWithData();
      var next = null;
      if (event.key === 'ArrowLeft') { next = step(start, -1); }
      if (event.key === 'ArrowRight') { next = step(start, 1); }
      if (event.key === 'Home') { next = step(-1, 1); }
      if (event.key === 'End') { next = lastWithData(); }
      if (event.key === 'Escape') { hide(); return; }
      if (next === null) { return; }
      event.preventDefault();
      show(next);
    });
    return tip;
  }

  function xLabels(node, buckets, plot, height, axisColor) {
    var maxLabels = Math.max(2, Math.floor(plot.width / 78));
    var every = Math.max(1, Math.ceil(buckets.length / maxLabels));
    for (var b = 0; b < buckets.length; b++) {
      if (b % every !== 0 && b !== buckets.length - 1) { continue; }
      node.appendChild(svg('text', {
        x: round1(plot.left + plot.slot * b + plot.slot / 2), y: height - 8,
        'text-anchor': 'middle', fill: axisColor, 'font-size': 11.5
      }, buckets[b].label));
    }
  }

  function gridline(node, plot, y, label, gridColor, axisColor) {
    node.appendChild(svg('line', {
      x1: plot.left, x2: plot.left + plot.width, y1: round1(y), y2: round1(y),
      stroke: gridColor, 'stroke-width': 1, 'shape-rendering': 'crispEdges'
    }));
    node.appendChild(svg('text', {
      x: plot.left - 8, y: round1(y + 3.5), 'text-anchor': 'end', fill: axisColor, 'font-size': 11.5
    }, label));
  }

  /** A column whose data end is rounded and whose baseline end is square. */
  function columnPath(x, top, width, baseline) {
    var radius = Math.min(4, width / 2, Math.max(0, baseline - top));
    return 'M' + round1(x) + ' ' + round1(baseline) +
      ' V' + round1(top + radius) +
      ' Q' + round1(x) + ' ' + round1(top) + ' ' + round1(x + radius) + ' ' + round1(top) +
      ' H' + round1(x + width - radius) +
      ' Q' + round1(x + width) + ' ' + round1(top) + ' ' + round1(x + width) + ' ' + round1(top + radius) +
      ' V' + round1(baseline) + ' Z';
  }

  /* ======================================================= rate columns */

  /**
   * A rate per bucket, as columns on a fixed 0–100% axis — the pass rate
   * trend. Columns rather than a line because the buckets are often sparse:
   * a line through a fortnight with four active days is four dots and three
   * dashes, and the eye joins what the data never did. An empty bucket is an
   * empty slot. One series, so no legend: the title says what it is. A thin
   * reference line carries the window's own rate, so a bad day reads as
   * "below the line" without a second colour, and the newest column carries
   * its value.
   *
   *   buckets: [{label, value: 0..100 | null, rows: [...tooltip rows]}]
   *   reference: number | null, referenceLabel: string
   */
  function drawRateColumns(container, spec) {
    var buckets = spec.buckets || [];
    var any = buckets.some(function (bucket) { return typeof bucket.value === 'number'; });
    if (!buckets.length || !any) {
      emptyState(container, spec.emptyMessage);
      return;
    }
    var width = containerWidth(container, 720);
    var height = spec.height || 220;
    var padding = { top: 18, right: 14, bottom: 26, left: 46 };
    var plot = {
      left: padding.left, top: padding.top,
      width: Math.max(10, width - padding.left - padding.right),
      height: Math.max(10, height - padding.top - padding.bottom)
    };
    plot.slot = plot.width / buckets.length;
    var baseline = plot.top + plot.height;
    var yFor = function (value) { return baseline - (Math.max(0, Math.min(100, value)) / 100) * plot.height; };
    var format = spec.formatValue || function (value) { return util.formatPercent(value, 1); };

    var node = frame(spec, width, height);
    var gridColor = color('--chart-grid', '#f0f2f6');
    var axisColor = color('--chart-axis', '#666666');
    var series = color('--chart-series', '#2f7fc4');
    var t;
    for (t = 0; t <= 4; t++) { gridline(node, plot, yFor(t * 25), (t * 25) + '%', gridColor, axisColor); }

    var barWidth = Math.max(2, Math.min(24, plot.slot - 2, plot.slot * 0.62));
    var slots = [];
    var newest = -1;
    for (var b = 0; b < buckets.length; b++) {
      var bucket = buckets[b];
      var x = plot.left + plot.slot * b + (plot.slot - barWidth) / 2;
      if (typeof bucket.value !== 'number') {
        node.appendChild(svg('rect', {
          x: round1(x), y: round1(baseline - 1), width: round1(barWidth), height: 1, fill: gridColor
        }));
        slots.push(null);
        continue;
      }
      newest = b;
      var top = yFor(bucket.value);
      node.appendChild(svg('path', { d: columnPath(x, top, barWidth, baseline), fill: series }));
      slots.push({
        x: plot.left + plot.slot * b, width: plot.slot, title: bucket.label,
        rows: [{ value: format(bucket.value), label: spec.valueLabel || 'pass rate', key: 'series' }]
          .concat(bucket.rows || [])
      });
    }

    var textColor = color('--text-muted', '#5b6573');
    if (typeof spec.reference === 'number' && isFinite(spec.reference)) {
      var refY = yFor(spec.reference);
      node.appendChild(svg('line', {
        x1: plot.left, x2: plot.left + plot.width, y1: round1(refY), y2: round1(refY),
        stroke: color('--text-subtle', '#7b8390'), 'stroke-width': 1, 'stroke-dasharray': '4 3'
      }));
      // At the left end: the newest column, and its label, are on the right.
      var below = refY - 6 < plot.top + 10;
      node.appendChild(svg('text', {
        x: plot.left + 6, y: round1(below ? refY + 14 : refY - 6), 'text-anchor': 'start',
        fill: textColor, 'font-size': 11.5, 'font-weight': 600
      }, (spec.referenceLabel || 'window') + ' ' + format(spec.reference)));
    }
    if (newest >= 0) {
      var capY = yFor(buckets[newest].value) - 6;
      if (capY > 10) {
        node.appendChild(svg('text', {
          x: round1(plot.left + plot.slot * newest + plot.slot / 2), y: round1(capY),
          'text-anchor': 'middle', fill: textColor, 'font-size': 11.5, 'font-weight': 600
        }, util.formatPercent(buckets[newest].value, 0)));
      }
    }
    xLabels(node, buckets, plot, height, axisColor);
    var tip = hoverLayer(container, node, slots, plot);

    var headers = [spec.xLabel || 'Bucket', spec.valueLabel ? util.titleCase(spec.valueLabel) : 'Pass rate']
      .concat(spec.extraHeaders || []);
    var rows = buckets.map(function (bucket) {
      return [bucket.label, typeof bucket.value === 'number' ? format(bucket.value) : util.DASH]
        .concat(bucket.cells || []);
    });
    util.replace(container, [node, tip, dataTable(spec.title || 'Rate', headers, rows)]);
  }

  /* ========================================================= range bars */

  //: Axis steps for durations: round to what a person would say.
  var DURATION_STEPS = [
    100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000,
    600000, 900000, 1800000, 3600000, 7200000
  ];

  function durationTick(ms) {
    if (ms === 0) { return '0'; }
    if (ms < 1000) { return Math.round(ms) + 'ms'; }
    if (ms < 60000) { return (ms % 1000 ? (ms / 1000).toFixed(1) : String(ms / 1000)) + 's'; }
    if (ms < 3600000) {
      var minutes = Math.floor(ms / 60000);
      var seconds = Math.round((ms - minutes * 60000) / 1000);
      return minutes + 'm' + (seconds ? ' ' + seconds + 's' : '');
    }
    var hours = Math.floor(ms / 3600000);
    var rest = Math.round((ms - hours * 3600000) / 60000);
    return hours + 'h' + (rest ? ' ' + rest + 'm' : '');
  }

  /**
   * Two quantiles per bucket as a floating bar — the run duration chart. The
   * bar spans p50 to p95, so its length is how unpredictable the bucket was;
   * the dot on it is the median, so its height is the typical run. Nothing is
   * joined across buckets, for the same reason as the rate columns.
   *
   *   buckets: [{label, low: ms | null, high: ms | null, rows: [...]}]
   */
  function drawRangeBars(container, spec) {
    var buckets = spec.buckets || [];
    var highest = 0;
    var any = false;
    buckets.forEach(function (bucket) {
      [bucket.low, bucket.high].forEach(function (value) {
        if (typeof value === 'number' && isFinite(value)) {
          any = true;
          highest = Math.max(highest, value);
        }
      });
    });
    if (!buckets.length || !any) {
      emptyState(container, spec.emptyMessage);
      return;
    }
    var width = containerWidth(container, 720);
    var height = spec.height || 220;
    var padding = { top: 18, right: 14, bottom: 26, left: 52 };
    var plot = {
      left: padding.left, top: padding.top,
      width: Math.max(10, width - padding.left - padding.right),
      height: Math.max(10, height - padding.top - padding.bottom)
    };
    plot.slot = plot.width / buckets.length;
    var raw = Math.max(1, highest) / 4;
    var tick = DURATION_STEPS[DURATION_STEPS.length - 1];
    for (var d = 0; d < DURATION_STEPS.length; d++) {
      if (DURATION_STEPS[d] >= raw) { tick = DURATION_STEPS[d]; break; }
    }
    var top = Math.max(tick, Math.ceil((highest * 1.08) / tick) * tick);
    var baseline = plot.top + plot.height;
    var yFor = function (value) { return baseline - (value / top) * plot.height; };

    var node = frame(spec, width, height);
    var gridColor = color('--chart-grid', '#f0f2f6');
    var axisColor = color('--chart-axis', '#666666');
    for (var v = 0; v <= top + 1; v += tick) { gridline(node, plot, yFor(v), durationTick(v), gridColor, axisColor); }

    var series = color('--chart-series', '#2f7fc4');
    var soft = color('--chart-series-soft', '#86b4e0');
    var ring = color('--surface', '#ffffff');
    var barWidth = Math.max(4, Math.min(12, plot.slot * 0.42));
    var slots = [];
    for (var b = 0; b < buckets.length; b++) {
      var bucket = buckets[b];
      var low = typeof bucket.low === 'number' ? bucket.low : null;
      var high = typeof bucket.high === 'number' ? bucket.high : null;
      var center = plot.left + plot.slot * b + plot.slot / 2;
      if (low === null && high === null) {
        node.appendChild(svg('rect', {
          x: round1(center - barWidth / 2), y: round1(baseline - 1), width: round1(barWidth), height: 1,
          fill: gridColor
        }));
        slots.push(null);
        continue;
      }
      if (low !== null && high !== null && high > low) {
        var barTop = yFor(high);
        var barHeight = Math.max(2, yFor(low) - barTop);
        node.appendChild(svg('rect', {
          x: round1(center - barWidth / 2), y: round1(barTop), width: round1(barWidth),
          height: round1(barHeight), rx: Math.min(4, barWidth / 2), fill: soft
        }));
      }
      var median = low !== null ? low : high;
      node.appendChild(svg('circle', {
        cx: round1(center), cy: round1(yFor(median)), r: 4.5,
        fill: series, stroke: ring, 'stroke-width': 2
      }));
      slots.push({
        x: plot.left + plot.slot * b, width: plot.slot, title: bucket.label,
        rows: [
          { value: low !== null ? util.formatDuration(low) : util.DASH, label: 'median (p50)', key: 'series' },
          { value: high !== null ? util.formatDuration(high) : util.DASH, label: 'p95', key: 'soft' }
        ].concat(bucket.rows || [])
      });
    }
    xLabels(node, buckets, plot, height, axisColor);
    var tip = hoverLayer(container, node, slots, plot);

    var rows = buckets.map(function (bucket) {
      return [
        bucket.label,
        typeof bucket.low === 'number' ? util.formatDuration(bucket.low) : util.DASH,
        typeof bucket.high === 'number' ? util.formatDuration(bucket.high) : util.DASH
      ];
    });
    util.replace(container, [
      node, tip,
      dataTable(spec.title || 'Duration', [spec.xLabel || 'Bucket', 'p50', 'p95'], rows)
    ]);
  }

  /* ================================================================ donut */

  function drawDonut(container, spec) {
    var segments = (spec.segments || []).filter(function (segment) { return segment.value > 0; });
    var size = spec.size || 160;
    var thickness = spec.thickness || 18;
    var radius = (size - thickness) / 2;
    var circumference = 2 * Math.PI * radius;
    var total = 0;
    var i;
    for (i = 0; i < segments.length; i++) { total += segments[i].value; }

    var node = frame(spec, size, size);
    var track = svg('circle', {
      cx: size / 2, cy: size / 2, r: radius, fill: 'none',
      stroke: color('--surface-3', '#eef3f9'), 'stroke-width': thickness
    });
    node.appendChild(track);

    if (total > 0) {
      var offset = 0;
      for (i = 0; i < segments.length; i++) {
        var fraction = segments[i].value / total;
        var length = fraction * circumference;
        var arc = svg('circle', {
          cx: size / 2, cy: size / 2, r: radius, fill: 'none',
          stroke: color(segments[i].color, '#cfd6e0'),
          'stroke-width': thickness,
          'stroke-dasharray': round1(length) + ' ' + round1(circumference - length),
          'stroke-dashoffset': round1(-offset),
          transform: 'rotate(-90 ' + size / 2 + ' ' + size / 2 + ')'
        });
        arc.appendChild(svg('title', {}, segments[i].label + ': ' + util.formatNumber(segments[i].value) +
          ' (' + (fraction * 100).toFixed(1) + '%)'));
        node.appendChild(arc);
        offset += length;
      }
    }

    if (spec.centerValue !== undefined && spec.centerValue !== null) {
      node.appendChild(svg('text', {
        x: size / 2, y: size / 2 + (spec.centerLabel ? 0 : 6), 'text-anchor': 'middle',
        fill: color('--text', '#2f3742'), 'font-size': spec.centerFontSize || 24,
        'font-weight': 640
      }, String(spec.centerValue)));
    }
    if (spec.centerLabel) {
      node.appendChild(svg('text', {
        x: size / 2, y: size / 2 + 18, 'text-anchor': 'middle',
        fill: color('--text-subtle', '#7b8390'), 'font-size': 12
      }, spec.centerLabel));
    }

    var rows = (spec.segments || []).map(function (segment) {
      return [
        segment.label,
        util.formatNumber(segment.value),
        total ? ((segment.value / total) * 100).toFixed(1) + '%' : util.DASH
      ];
    });
    util.replace(container, [
      node,
      dataTable(spec.title || 'Distribution', ['Status', 'Tests', 'Share'], rows)
    ]);
  }

  /* ============================================================ sparkline */

  function drawSparkline(container, spec) {
    var values = (spec.values || []).filter(function (value) {
      return typeof value === 'number' && isFinite(value);
    });
    if (values.length < 2) {
      util.replace(container, el('span', { className: 'subtle', text: util.DASH }));
      return;
    }
    var width = spec.width || 96;
    var height = spec.height || 24;
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    if (max === min) { max = min + 1; }
    var stroke = color(spec.color || '--chart-line', '#36a2eb');

    var node = svg('svg', {
      width: width, height: height, viewBox: '0 0 ' + width + ' ' + height,
      role: 'img', 'aria-label': spec.ariaLabel || 'Sparkline', focusable: 'false'
    });
    node.appendChild(svg('title', {}, spec.title || spec.ariaLabel || 'Trend'));

    var path = values.map(function (value, index) {
      var x = (index / (values.length - 1)) * (width - 2) + 1;
      var y = height - 2 - ((value - min) / (max - min)) * (height - 4);
      return (index === 0 ? 'M' : 'L') + round1(x) + ' ' + round1(y);
    }).join(' ');
    node.appendChild(svg('path', {
      d: path, fill: 'none', stroke: stroke, 'stroke-width': 1.6,
      'stroke-linecap': 'round', 'stroke-linejoin': 'round'
    }));
    node.appendChild(svg('circle', {
      cx: width - 1, cy: height - 2 - ((values[values.length - 1] - min) / (max - min)) * (height - 4),
      r: 2, fill: stroke
    }));
    util.replace(container, node);
  }

  function round1(value) { return Math.round(value * 10) / 10; }

  /* ------------------------------------------------------------ dispatch */

  var KINDS = {
    stackedBar: drawStackedBar,
    rateColumns: drawRateColumns,
    rangeBars: drawRangeBars,
    donut: drawDonut,
    sparkline: drawSparkline
  };

  function draw(container, spec, skipRegister) {
    if (!container || !spec) { return; }
    var fn = KINDS[spec.kind];
    if (!fn) {
      util.reportError('unknown chart kind: ' + spec.kind, null);
      return;
    }
    if (!skipRegister) { register(container, spec); }
    fn(container, spec);
  }

  util.theme.onChange(redrawAll);

  var onResize = util.debounce(function () { redrawAll(); }, 180, 900);
  window.addEventListener('resize', onResize);

  TestHub.charts = {
    draw: draw,
    stackedBar: function (container, spec) { spec.kind = 'stackedBar'; draw(container, spec); },
    donut: function (container, spec) { spec.kind = 'donut'; draw(container, spec); },
    sparkline: function (container, spec) { spec.kind = 'sparkline'; draw(container, spec); },
    redrawAll: redrawAll,
    forget: function (container) {
      registry = registry.filter(function (entry) { return entry.container !== container; });
    }
  };
})(window, document);
