/* Test Hub — charts, hand-rolled in SVG (ADR-0001 D7).
 *
 * Four shapes cover the whole dashboard: a trend line with an optional area, a
 * stacked bar, a donut and a sparkline. No library, no canvas, no build step —
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
 * Gaps are gaps: a bucket with no runs breaks the line instead of drawing a
 * straight segment between the days on either side of a quiet weekend.
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

  /* ================================================================ trend */

  function drawTrend(container, spec) {
    var points = spec.points || [];
    var series = spec.series || [];
    if (!points.length || !series.length) {
      emptyState(container, spec.emptyMessage);
      return;
    }

    var width = containerWidth(container, 720);
    var height = spec.height || 220;
    var padding = { top: 14, right: 14, bottom: 26, left: 46 };
    var plotWidth = Math.max(10, width - padding.left - padding.right);
    var plotHeight = Math.max(10, height - padding.top - padding.bottom);

    var values = [];
    var s;
    var p;
    for (s = 0; s < series.length; s++) {
      for (p = 0; p < points.length; p++) {
        var raw = points[p].values[series[s].key];
        if (typeof raw === 'number' && isFinite(raw)) { values.push(raw); }
      }
    }
    if (!values.length) {
      emptyState(container, spec.emptyMessage);
      return;
    }

    var min = spec.yMin !== undefined ? spec.yMin : Math.min.apply(null, values);
    var max = spec.yMax !== undefined ? spec.yMax : Math.max.apply(null, values);
    if (max === min) { max = min + (Math.abs(min) > 1 ? Math.abs(min) * 0.1 : 1); }
    // Breathing room, so a line does not graze the top gridline — but only on
    // the ends the caller left open. A pass-rate axis pinned to 0..100 stays.
    var headroom = (max - min) * 0.12 || 1;
    if (spec.yMin === undefined) { min = min - headroom; }
    if (spec.yMax === undefined) { max = max + headroom; }
    if (spec.yFloor !== undefined) { min = Math.max(spec.yFloor, min); }
    var span = max - min || 1;

    var xFor = function (index) {
      return points.length === 1
        ? padding.left + plotWidth / 2
        : padding.left + (index / (points.length - 1)) * plotWidth;
    };
    var yFor = function (value) {
      return padding.top + plotHeight - ((value - min) / span) * plotHeight;
    };

    var node = frame(spec, width, height);
    var gridColor = color('--chart-grid', '#e6ebf2');
    var axisColor = color('--chart-axis', '#646f7e');
    var markerColor = color('--chart-marker', '#ffffff');
    var formatY = spec.formatY || function (value) { return String(Math.round(value)); };

    var ticks = spec.ticks || 4;
    var t;
    for (t = 0; t <= ticks; t++) {
      var value = min + (span * t) / ticks;
      var y = yFor(value);
      node.appendChild(svg('line', {
        x1: padding.left, x2: width - padding.right, y1: y, y2: y,
        stroke: gridColor, 'stroke-width': 1, 'shape-rendering': 'crispEdges'
      }));
      node.appendChild(svg('text', {
        x: padding.left - 8, y: y + 3.5, 'text-anchor': 'end',
        fill: axisColor, 'font-size': 10.5
      }, formatY(value)));
    }

    var maxLabels = Math.max(2, Math.floor(plotWidth / 78));
    var step = Math.max(1, Math.ceil(points.length / maxLabels));
    for (p = 0; p < points.length; p++) {
      if (p % step !== 0 && p !== points.length - 1) { continue; }
      node.appendChild(svg('text', {
        x: xFor(p), y: height - 8,
        'text-anchor': p === 0 ? 'start' : (p === points.length - 1 ? 'end' : 'middle'),
        fill: axisColor, 'font-size': 10.5
      }, points[p].label));
    }

    for (s = 0; s < series.length; s++) {
      var line = series[s];
      var stroke = color(line.color, '#2f4fd8');
      var segments = [];
      var current = [];
      for (p = 0; p < points.length; p++) {
        var pointValue = points[p].values[line.key];
        if (typeof pointValue === 'number' && isFinite(pointValue)) {
          current.push({ x: xFor(p), y: yFor(pointValue), index: p, value: pointValue });
        } else if (current.length) {
          segments.push(current);
          current = [];
        }
      }
      if (current.length) { segments.push(current); }

      var gradientId = null;
      if (line.area) {
        gradientId = nextId('g');
        var gradient = svg('linearGradient', {
          id: gradientId, x1: 0, y1: 0, x2: 0, y2: 1
        }, [
          svg('stop', { offset: '0%', 'stop-color': color('--chart-fill-top', '#2f4fd842') }),
          svg('stop', { offset: '100%', 'stop-color': color('--chart-fill-bottom', '#2f4fd805') })
        ]);
        node.appendChild(svg('defs', {}, gradient));
      }

      for (var g = 0; g < segments.length; g++) {
        var segment = segments[g];
        var path = segment.map(function (point, index) {
          return (index === 0 ? 'M' : 'L') + round1(point.x) + ' ' + round1(point.y);
        }).join(' ');

        if (line.area && segment.length > 1) {
          var baseline = padding.top + plotHeight;
          node.appendChild(svg('path', {
            d: path + ' L' + round1(segment[segment.length - 1].x) + ' ' + round1(baseline) +
              ' L' + round1(segment[0].x) + ' ' + round1(baseline) + ' Z',
            fill: 'url(#' + gradientId + ')', stroke: 'none'
          }));
        }
        node.appendChild(svg('path', {
          d: path, fill: 'none', stroke: stroke, 'stroke-width': line.width || 2,
          'stroke-linecap': 'round', 'stroke-linejoin': 'round',
          'stroke-dasharray': line.dashed ? '5 4' : null
        }));

        // One marker per point when the series is sparse enough to read; a
        // 90-day trend with 90 dots is noise, not information.
        if (points.length <= 45) {
          for (var m = 0; m < segment.length; m++) {
            var marker = svg('circle', {
              cx: round1(segment[m].x), cy: round1(segment[m].y), r: 2.8,
              fill: markerColor, stroke: stroke, 'stroke-width': 1.6
            });
            marker.appendChild(svg('title', {}, points[segment[m].index].label + ' · ' +
              line.label + ' ' + (line.format ? line.format(segment[m].value) : formatY(segment[m].value))));
            node.appendChild(marker);
          }
        }
      }
    }

    var rows = points.map(function (point) {
      var row = [point.label];
      for (var i = 0; i < series.length; i++) {
        var value = point.values[series[i].key];
        row.push(typeof value === 'number'
          ? (series[i].format ? series[i].format(value) : formatY(value))
          : util.DASH);
      }
      return row;
    });
    var headers = [spec.xLabel || 'Bucket'];
    for (s = 0; s < series.length; s++) { headers.push(series[s].label); }

    util.replace(container, [node, dataTable(spec.title || 'Trend', headers, rows)]);
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
    var gridColor = color('--chart-grid', '#e6ebf2');
    var axisColor = color('--chart-axis', '#646f7e');
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
        x: padding.left - 8, y: y + 3.5, 'text-anchor': 'end', fill: axisColor, 'font-size': 10.5
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
          fill: color(keys[k].color, '#545e6c'), rx: 1.5
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
          fill: axisColor, 'font-size': 10.5
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
      stroke: color('--surface-3', '#eef1f6'), 'stroke-width': thickness
    });
    node.appendChild(track);

    if (total > 0) {
      var offset = 0;
      for (i = 0; i < segments.length; i++) {
        var fraction = segments[i].value / total;
        var length = fraction * circumference;
        var arc = svg('circle', {
          cx: size / 2, cy: size / 2, r: radius, fill: 'none',
          stroke: color(segments[i].color, '#545e6c'),
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
        fill: color('--text', '#111621'), 'font-size': spec.centerFontSize || 22,
        'font-weight': 640
      }, String(spec.centerValue)));
    }
    if (spec.centerLabel) {
      node.appendChild(svg('text', {
        x: size / 2, y: size / 2 + 18, 'text-anchor': 'middle',
        fill: color('--text-subtle', '#66707f'), 'font-size': 11
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
    var stroke = color(spec.color || '--chart-line', '#2f4fd8');

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
    trend: drawTrend,
    stackedBar: drawStackedBar,
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
    trend: function (container, spec) { spec.kind = 'trend'; draw(container, spec); },
    stackedBar: function (container, spec) { spec.kind = 'stackedBar'; draw(container, spec); },
    donut: function (container, spec) { spec.kind = 'donut'; draw(container, spec); },
    sparkline: function (container, spec) { spec.kind = 'sparkline'; draw(container, spec); },
    redrawAll: redrawAll,
    forget: function (container) {
      registry = registry.filter(function (entry) { return entry.container !== container; });
    }
  };
})(window, document);
