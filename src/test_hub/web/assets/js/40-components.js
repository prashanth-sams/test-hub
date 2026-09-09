/* Test Hub — the component kit.
 *
 * Small functions that return DOM nodes. No templating, no innerHTML, no
 * string concatenation that ends up in the document: every piece of data
 * reaches the page as a text node or as an attribute set through setAttribute.
 * The fixture in this repo whose test name is literally
 * `<script>alert(1)</script>` renders as that text, in a table cell, in every
 * one of these components, because there is no code path here that could do
 * anything else.
 *
 * The states nobody designs — loading, empty, error, disconnected — are
 * components in their own right and get the same care as the happy path. A
 * dashboard's honesty is mostly visible in those four.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var el = util.el;
  var svgEl = util.svg;

  /* ---------------------------------------------------------------- icons */

  var ICON_PATHS = {
    sun: ['M12 17.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11Z', 'M12 1.8v2.4M12 19.8v2.4M4.2 12H1.8M22.2 12h-2.4M6.5 6.5 4.8 4.8M19.2 19.2l-1.7-1.7M17.5 6.5l1.7-1.7M4.8 19.2l1.7-1.7'],
    moon: ['M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1Z'],
    chevron: ['m9 6 6 6-6 6'],
    alert: ['M12 9v4.5M12 17h.01', 'M10.3 3.9 2.6 17.4A2 2 0 0 0 4.3 20.4h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z'],
    inbox: ['M3.5 13.5h4l1.5 3h6l1.5-3h4', 'M5.6 4.8h12.8l2.1 8.7v4a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2v-4Z'],
    refresh: ['M20.5 12a8.5 8.5 0 1 1-2.6-6.1', 'M20.8 4.2v4.6h-4.6'],
    external: ['M14 4h6v6', 'M20 4 11 13', 'M18 14.5V19a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 19V8a1.5 1.5 0 0 1 1.5-1.5H10'],
    search: ['M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z', 'm20 20-4.1-4.1'],
    back: ['M20 12H4', 'm10 6-6 6 6 6'],
    filter: ['M3.5 5.5h17l-6.5 7.6V19l-4 2v-7.9Z'],
    clock: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z', 'M12 7.2V12l3 1.8'],
    gear: ['M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z', 'M19.4 14.4a1.5 1.5 0 0 0 .3 1.7l.1.1a1.9 1.9 0 1 1-2.6 2.6l-.1-.1a1.5 1.5 0 0 0-2.6 1v.2a1.9 1.9 0 1 1-3.8 0v-.1a1.5 1.5 0 0 0-2.6-1l-.1.1a1.9 1.9 0 1 1-2.6-2.6l.1-.1a1.5 1.5 0 0 0-1-2.6h-.2a1.9 1.9 0 1 1 0-3.8h.1a1.5 1.5 0 0 0 1-2.6l-.1-.1a1.9 1.9 0 1 1 2.6-2.6l.1.1a1.5 1.5 0 0 0 1.7.3h.1a1.5 1.5 0 0 0 .9-1.4v-.2a1.9 1.9 0 1 1 3.8 0v.1a1.5 1.5 0 0 0 2.6 1l.1-.1a1.9 1.9 0 1 1 2.6 2.6l-.1.1a1.5 1.5 0 0 0-.3 1.7v.1a1.5 1.5 0 0 0 1.4.9h.2a1.9 1.9 0 1 1 0 3.8h-.1a1.5 1.5 0 0 0-1.4.9Z'],
    check: ['m5 12.8 4.4 4.4L19 7.6'],
    monitor: ['M4.5 4.8h15a1.5 1.5 0 0 1 1.5 1.5v8.4a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 14.7V6.3a1.5 1.5 0 0 1 1.5-1.5Z', 'M9 19.2h6M12 16.2v3'],
    grid: ['M4.2 4.2h6.2v6.2H4.2zM13.6 4.2h6.2v6.2h-6.2zM4.2 13.6h6.2v6.2H4.2zM13.6 13.6h6.2v6.2h-6.2z'],
    list: ['M9 6.5h11M9 12h11M9 17.5h11', 'M4.6 6.5h.01M4.6 12h.01M4.6 17.5h.01'],
    chart: ['M4 4.5v15h16', 'm7.5 15.2 3.6-4.6 3 2.4L20 6.6'],
    bug: ['M9 7.4a3 3 0 0 1 6 0', 'M7.5 10.4h9v4.1a4.5 4.5 0 0 1-9 0Z', 'M4.6 12.2h2.9M16.5 12.2h2.9M5.6 7.2l2 1.6M18.4 7.2l-2 1.6M5.6 17.6l2-1.4M18.4 17.6l-2-1.4'],
    users: ['M9.4 11.6a3.4 3.4 0 1 0 0-6.8 3.4 3.4 0 0 0 0 6.8Z', 'M3.6 19.6a5.8 5.8 0 0 1 11.6 0', 'M16.2 5.3a3.4 3.4 0 0 1 0 6.6', 'M17.6 14.5a5.6 5.6 0 0 1 2.8 5.1'],
    bolt: ['M13.2 3.2 5.6 13.6h5.3l-.8 7.2 8.3-10.4h-5.3Z'],
    layers: ['m12 3.6 8 4.2-8 4.2-8-4.2 8-4.2Z', 'm4 12.2 8 4.2 8-4.2', 'm4 16.4 8 4.2 8-4.2'],
    briefcase: ['M4.5 8h15a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-9A1.5 1.5 0 0 1 4.5 8Z', 'M9 8V6.2A1.5 1.5 0 0 1 10.5 4.7h3A1.5 1.5 0 0 1 15 6.2V8', 'M3 12.6h18'],
    rows: ['M3.6 5.6h16.8M3.6 10h16.8M3.6 14h16.8M3.6 18.4h16.8'],
    steps: ['M9.5 6.5h10.5M9.5 12h10.5M9.5 17.5h10.5', 'M3.8 5h1.3v3.4', 'M3.6 11.2a1 1 0 0 1 1.7.7c0 .9-1.7 1.4-1.7 2.3h1.9', 'M3.7 16.3h1.6l-1 1.2a1.1 1.1 0 1 1-.6 1.7'],
    archive: ['M3.6 5.4h16.8v3.2H3.6z', 'M5.2 8.6v9.2a1.5 1.5 0 0 0 1.5 1.5h10.6a1.5 1.5 0 0 0 1.5-1.5V8.6', 'M10 12.2h4'],
    camera: ['M4.5 8h2.9l1.4-2.3h6.4L16.6 8h2.9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-8A1.5 1.5 0 0 1 4.5 8Z', 'M12 16.3a3.3 3.3 0 1 0 0-6.6 3.3 3.3 0 0 0 0 6.6Z'],
    swap: ['M4 8.6h13', 'm13.6 5.1 3.5 3.5-3.5 3.5', 'M20 15.4H7', 'm10.4 11.9-3.5 3.5 3.5 3.5'],
    shield: ['M12 3.4 4.8 6v5.5c0 4.2 2.9 7.5 7.2 9.1 4.3-1.6 7.2-4.9 7.2-9.1V6L12 3.4Z', 'm9 12.2 2.2 2.2 4-4.3']
  };

  function icon(name, size) {
    var paths = ICON_PATHS[name] || ICON_PATHS.inbox;
    var node = svgEl('svg', {
      width: size || 16, height: size || 16, viewBox: '0 0 24 24',
      fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7,
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      'aria-hidden': 'true', focusable: 'false'
    });
    for (var i = 0; i < paths.length; i++) {
      node.appendChild(svgEl('path', { d: paths[i] }));
    }
    return node;
  }

  /* --------------------------------------------------------------- atoms */

  function statusPill(status, options) {
    var opts = options || {};
    var value = status || 'unknown';
    return el('span', {
      className: 'statuspill' + (opts.className ? ' ' + opts.className : ''),
      dataset: { status: value },
      text: opts.label || util.statusLabel(value),
      attrs: opts.title ? { title: opts.title } : null
    });
  }

  function liveBadge(text) {
    return el('span', { className: 'livebadge', text: text || 'Live' });
  }

  function tag(text) {
    return el('span', { className: 'tag', text: text });
  }

  function tagList(tags, limit) {
    var items = (tags || []).slice(0, limit || 6);
    var nodes = items.map(tag);
    if ((tags || []).length > items.length) {
      nodes.push(el('span', { className: 'tag', text: '+' + ((tags || []).length - items.length) }));
    }
    return el('span', { className: 'row row--tight' }, nodes);
  }

  function link(hash, text, className) {
    return el('a', { className: className || null, attrs: { href: hash }, text: text });
  }

  function externalLink(href, text) {
    // Rendered only when the model supplied a URL; `noopener` because the
    // target is a CI system somebody else operates.
    return el('a', {
      attrs: { href: href, rel: 'noreferrer noopener', target: '_blank' },
      className: 'row row--tight'
    }, [document.createTextNode(text), icon('external', 14)]);
  }

  function deltaText(value, options) {
    var opts = options || {};
    return el('span', {
      className: 'delta',
      dataset: { dir: util.deltaDirection(value, opts) },
      text: util.formatDelta(value, opts.unit || 'pct')
    });
  }

  /** The composition of a run in one strip, with a text alternative. */
  function countBar(counts, options) {
    var opts = options || {};
    var total = 0;
    var i;
    for (i = 0; i < util.STATUS_ORDER.length; i++) {
      total += counts[util.STATUS_ORDER[i]] || 0;
    }
    var bar = el('span', {
      className: 'countbar',
      attrs: { role: 'img', 'aria-label': countsSentence(counts) }
    });
    if (!total) {
      return el('span', { className: 'countbar' });
    }
    for (i = 0; i < util.STATUS_ORDER.length; i++) {
      var status = util.STATUS_ORDER[i];
      var value = counts[status] || 0;
      if (!value) { continue; }
      var segment = el('span', {
        dataset: { status: status },
        attrs: { title: util.statusLabel(status) + ': ' + util.formatNumber(value) }
      });
      segment.style.width = ((value / total) * 100).toFixed(3) + '%';
      bar.appendChild(segment);
    }
    if (opts.width) { bar.style.width = opts.width; }
    return bar;
  }

  function countsSentence(counts) {
    var parts = [];
    for (var i = 0; i < util.STATUS_ORDER.length; i++) {
      var status = util.STATUS_ORDER[i];
      if (counts[status]) {
        parts.push(util.formatNumber(counts[status]) + ' ' + util.statusLabel(status).toLowerCase());
      }
    }
    return parts.length ? parts.join(', ') : 'no tests';
  }

  /* -------------------------------------------------------------- layout */

  function card(spec) {
    var children = [];
    if (spec.title || spec.actions || spec.hint) {
      var head = el('div', { className: 'card__head' }, [
        el('div', { className: 'row row--tight' }, [
          // The icon is decorative: it repeats what the title already says, so
          // it is aria-hidden and the heading keeps the whole accessible name.
          spec.icon ? el('span', { className: 'card__icon' }, icon(spec.icon, 18)) : null,
          el('h2', { className: 'card__title', text: spec.title || '' }),
          spec.badge || null
        ]),
        spec.actions ? el('div', { className: 'row row--tight' }, spec.actions)
          : (spec.hint ? el('span', { className: 'card__hint', text: spec.hint }) : null)
      ]);
      children.push(head);
    }
    children.push(el('div', {
      className: 'card__body' + (spec.flush ? ' card__body--flush' : '')
    }, spec.body));
    return el('section', {
      className: 'card' + (spec.className ? ' ' + spec.className : ''),
      attrs: spec.ariaLabel ? { 'aria-label': spec.ariaLabel } : null
    }, children);
  }

  function section(spec) {
    return el('section', { className: 'section' }, [
      el('div', { className: 'section__head' }, [
        el('h2', { className: 'section__title', text: spec.title }),
        spec.aside ? el('div', { className: 'section__aside' }, spec.aside) : null
      ]),
      spec.body
    ]);
  }

  function kpi(spec) {
    return el('div', {
      className: 'kpi',
      dataset: spec.status ? { status: spec.status } : null
    }, [
      el('div', { className: 'kpi__label', text: spec.label }),
      el('div', { className: 'kpi__value', text: spec.value }),
      spec.meta ? el('div', { className: 'kpi__meta' }, spec.meta) : null
    ]);
  }

  /**
   * The headline tile of the analytics page: one number, what it measures, and
   * the sentence that stops it being misread. `tone` colours the top rule with
   * the status language, so a score that is bad looks bad at a glance and a
   * count that is merely a count does not.
   */
  function statTile(spec) {
    return el('div', {
      className: 'stat',
      dataset: spec.tone ? { tone: spec.tone } : null
    }, [
      el('div', { className: 'stat__value' }, [
        el('span', { text: spec.value }),
        spec.delta === null || spec.delta === undefined
          ? null
          : deltaText(spec.delta, { unit: spec.deltaUnit || 'pct' })
      ]),
      el('div', { className: 'stat__label', text: spec.label }),
      spec.hint ? el('div', { className: 'stat__hint', text: spec.hint }) : null
    ]);
  }

  /**
   * A proportion drawn as a bar. Always paired with the number it draws — the
   * bar is the comparison between rows, never the value itself, which is why
   * `label` is rendered as text next to it rather than inside it.
   */
  function meter(spec) {
    var max = spec.max || 0;
    var value = spec.value || 0;
    var share = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
    var fill = el('span', { className: 'meter__fill', dataset: { status: spec.status || 'total' } });
    fill.style.width = (share * 100).toFixed(2) + '%';
    return el('span', {
      className: 'meter',
      attrs: { role: 'img', 'aria-label': spec.ariaLabel || (spec.label || String(value)) }
    }, fill);
  }

  /** A short piece of evidence — usually a test name under a failure type. */
  function chip(text, hash) {
    if (hash) {
      return el('a', { className: 'chip chip--link', text: text, attrs: { href: hash } });
    }
    return el('span', { className: 'chip', text: text });
  }

  /**
   * What history says about one test. The four words are deliberate and
   * exclusive: `stable` never failed, `flaky` disagreed with itself, `broken`
   * has not passed, `quiet` did not run often enough for any of the three to
   * mean anything.
   */
  function verdictBadge(verdict, title) {
    return el('span', {
      className: 'verdict',
      dataset: { verdict: verdict },
      text: verdict,
      attrs: title ? { title: title } : null
    });
  }

  /**
   * The outcome counters, sized to be read across a room and clickable when the
   * caller can say where each one leads. A status with no tests is still drawn:
   * "0 failed" is the reassurance people come to the page for, and a strip that
   * changes shape between runs cannot be compared with the one before it.
   */
  //: The outcomes a finished test can carry. `running`, `queued`, `cancelled`
  //: and `unknown` are states, not outcomes, and a strip that lists them spends
  //: a third of its width on zeroes.
  var STRIP_STATUSES = ['passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed'];

  function statStrip(counts, options) {
    var opts = options || {};
    var statuses = opts.statuses || STRIP_STATUSES;
    var nodes = [];
    var build = function (status, value) {
      var label = util.statusLabel(status);
      var children = [
        el('span', { className: 'statstrip__value', text: util.formatNumber(value) }),
        el('span', { className: 'statstrip__label', text: label })
      ];
      // A counter at zero has nothing behind it to show, so it stays a figure
      // rather than becoming a button that does nothing when pressed.
      if (!opts.onSelect || !value) {
        return el('div', { className: 'statstrip__item', dataset: { status: status } }, children);
      }
      return el('button', {
        className: 'statstrip__item',
        dataset: { status: status },
        attrs: { type: 'button', title: 'Show only the ' + label.toLowerCase() + ' tests' },
        on: { click: function () { opts.onSelect(status); } }
      }, children);
    };
    for (var i = 0; i < statuses.length; i++) {
      var value = counts[statuses[i]] || 0;
      if (!value && opts.hideEmpty) { continue; }
      nodes.push(build(statuses[i], value));
    }
    // Flakiness and retries are properties of a test's attempts rather than
    // outcomes it can end on, so they are counted separately and never
    // clickable: there is no "the flaky ones" filter to jump to.
    var extras = opts.extras || [];
    for (var e = 0; e < extras.length; e++) {
      nodes.push(el('div', {
        className: 'statstrip__item',
        dataset: { status: extras[e].status || 'unknown' },
        attrs: extras[e].title ? { title: extras[e].title } : null
      }, [
        el('span', { className: 'statstrip__value', text: util.formatNumber(extras[e].value || 0) }),
        el('span', { className: 'statstrip__label', text: extras[e].label })
      ]));
    }
    return el('div', { className: 'statstrip' }, nodes);
  }

  /**
   * One cell per run, oldest on the left: the shape of a test's recent history
   * at the size of a word. Deliberately not a sparkline — the value being drawn
   * is categorical (it passed, it did not), and a line implies a magnitude
   * between two outcomes that has no meaning.
   */
  function streak(points, options) {
    var opts = options || {};
    var limit = opts.limit || 12;
    var items = points.slice(-limit);
    var cells = items.map(function (point) {
      return el('span', {
        className: 'streak__cell',
        dataset: { status: point.status || 'unknown' },
        attrs: { title: point.title || util.statusLabel(point.status) }
      });
    });
    return el('span', {
      className: 'streak',
      attrs: { role: 'img', 'aria-label': opts.ariaLabel || (items.length + ' most recent runs') }
    }, cells);
  }

  function kv(pairs) {
    var nodes = [];
    for (var i = 0; i < pairs.length; i++) {
      if (!pairs[i]) { continue; }
      nodes.push(el('dt', { text: pairs[i][0] }));
      nodes.push(el('dd', {}, pairs[i][1]));
    }
    return el('dl', { className: 'kv' }, nodes);
  }

  function metaGrid(items) {
    return el('div', { className: 'metagrid' }, items.filter(Boolean).map(function (item) {
      return el('div', { className: 'metagrid__item' }, [
        el('div', { className: 'metagrid__label', text: item[0] }),
        el('div', { className: 'metagrid__value' }, item[1])
      ]);
    }));
  }

  function banner(spec) {
    return el('div', {
      className: 'banner' + (spec.tone ? ' banner--' + spec.tone : ''),
      attrs: { role: spec.tone === 'error' ? 'alert' : 'status' }
    }, [
      icon(spec.icon || (spec.tone === 'error' ? 'alert' : 'clock'), 18),
      el('div', {}, [
        spec.title ? el('div', { className: 'banner__title', text: spec.title }) : null,
        spec.text ? el('div', { text: spec.text }) : null
      ]),
      spec.actions ? el('div', { className: 'banner__actions' }, spec.actions) : null
    ]);
  }

  /* -------------------------------------------------------------- states */

  function skeleton(kind, count) {
    var nodes = [];
    for (var i = 0; i < (count || 1); i++) {
      nodes.push(el('div', { className: 'skeleton skeleton--' + (kind || 'text') }));
    }
    return el('div', { attrs: { 'aria-hidden': 'true' } }, nodes);
  }

  function loadingBlock(label, kind, count) {
    return el('div', { attrs: { role: 'status', 'aria-live': 'polite' } }, [
      el('span', { className: 'sr-only', text: label || 'Loading' }),
      skeleton(kind || 'row', count || 5)
    ]);
  }

  function emptyState(spec) {
    return el('div', { className: 'empty' }, [
      el('div', { className: 'empty__icon' }, icon(spec.icon || 'inbox', 22)),
      el('p', { className: 'empty__title', text: spec.title || 'Nothing here yet' }),
      spec.hint ? el('p', { className: 'empty__hint', text: spec.hint }) : null,
      spec.action || null
    ]);
  }

  function errorState(spec) {
    var error = spec.error || {};
    var actions = [];
    if (spec.onRetry) {
      actions.push(el('button', {
        className: 'btn', attrs: { type: 'button' }, on: { click: spec.onRetry }
      }, [icon('refresh', 15), document.createTextNode('Try again')]));
    }
    return el('div', { className: 'errorbox', attrs: { role: 'alert' } }, [
      el('div', { className: 'errorbox__icon' }, icon('alert', 22)),
      el('p', { className: 'errorbox__title', text: spec.title || 'That did not load' }),
      el('p', { className: 'errorbox__hint', text: error.message || String(error) || 'Unknown error' }),
      error.hint ? el('p', { className: 'errorbox__detail', text: error.hint }) : null,
      error.code ? el('p', { className: 'errorbox__detail', text: 'code: ' + error.code }) : null,
      actions.length ? el('div', { className: 'row' }, actions) : null
    ]);
  }

  /* -------------------------------------------------------------- tables */

  /**
   * Columns are `{key, label, render(row) -> node|string, align, sortable,
   * width, mobileLabel}`. `data-label` goes on every cell so the mobile card
   * layout can label the values in CSS instead of duplicating the markup.
   */
  function table(spec) {
    var columns = spec.columns || [];
    var rows = spec.rows || [];
    var i;

    var headCells = [];
    for (i = 0; i < columns.length; i++) {
      var column = columns[i];
      var attrs = { scope: 'col' };
      if (spec.sort && column.sortable && spec.sort.key === column.key) {
        attrs['aria-sort'] = spec.sort.dir === 'asc' ? 'ascending' : 'descending';
      }
      var content;
      if (column.sortable && spec.onSort) {
        content = el('button', {
          className: 'table__sort',
          attrs: { type: 'button' },
          text: column.label,
          on: { click: makeSortHandler(spec, column) }
        });
      } else {
        content = document.createTextNode(column.label);
      }
      headCells.push(el('th', {
        className: column.align === 'right' ? 'num' : null,
        attrs: attrs
      }, content));
    }

    var bodyRows = [];
    for (i = 0; i < rows.length; i++) {
      bodyRows.push(buildRow(spec, columns, rows[i], i));
    }

    var tableNode = el('table', {
      className: 'table' + (spec.cards === false ? '' : ' table--cards') +
        (spec.onRowClick ? ' table--rows' : ''),
      attrs: spec.caption ? null : { 'aria-label': spec.ariaLabel || 'Results' }
    }, [
      spec.caption ? el('caption', { className: 'sr-only', text: spec.caption }) : null,
      el('thead', {}, el('tr', {}, headCells)),
      el('tbody', {}, bodyRows)
    ]);

    return el('div', { className: 'table-wrap' }, tableNode);
  }

  function makeSortHandler(spec, column) {
    return function () {
      var dir = 'desc';
      if (spec.sort && spec.sort.key === column.key) {
        dir = spec.sort.dir === 'desc' ? 'asc' : 'desc';
      } else if (column.defaultDir) {
        dir = column.defaultDir;
      }
      spec.onSort(column.key, dir);
    };
  }

  function buildRow(spec, columns, row, index) {
    var cells = [];
    for (var i = 0; i < columns.length; i++) {
      var column = columns[i];
      var value = column.render ? column.render(row, index) : row[column.key];
      var cell = el('td', {
        className: column.align === 'right' ? 'num' : null,
        dataset: { label: column.mobileLabel !== undefined ? column.mobileLabel : column.label }
      });
      util.appendChild(cell, value === null || value === undefined ? util.DASH : value);
      cells.push(cell);
    }
    var tr = el('tr', {}, cells);
    if (spec.onRowClick) {
      // The row is a convenience; the first cell always holds a real link, so
      // the keyboard path never depends on this handler.
      tr.addEventListener('click', function (event) {
        var target = event.target;
        while (target && target !== tr) {
          var name = target.tagName;
          if (name === 'A' || name === 'BUTTON' || name === 'INPUT' || name === 'SELECT') { return; }
          target = target.parentNode;
        }
        spec.onRowClick(row, event);
      });
    }
    return tr;
  }

  function pager(spec) {
    var page = spec.page || 1;
    var limit = spec.limit || 25;
    var total = spec.total || 0;
    var pages = Math.max(1, Math.ceil(total / limit));
    var first = total ? (page - 1) * limit + 1 : 0;
    var last = Math.min(total, page * limit);
    var summary = spec.exact === false
      ? ('Showing ' + util.formatNumber(spec.shown || 0) + ' of this page — the rest were filtered here')
      : ('Showing ' + util.formatNumber(first) + '–' + util.formatNumber(last) +
        ' of ' + util.formatNumber(total));

    return el('div', { className: 'pager' }, [
      el('span', { className: 'num', text: summary }),
      el('div', { className: 'pager__buttons' }, [
        el('button', {
          className: 'btn btn--sm',
          attrs: { type: 'button', disabled: page <= 1 ? 'disabled' : null },
          text: 'Previous',
          on: { click: function () { spec.onPage(page - 1); } }
        }),
        el('span', { className: 'num', text: 'Page ' + page + ' of ' + pages }),
        el('button', {
          className: 'btn btn--sm',
          attrs: { type: 'button', disabled: page >= pages ? 'disabled' : null },
          text: 'Next',
          on: { click: function () { spec.onPage(page + 1); } }
        })
      ])
    ]);
  }

  /* ------------------------------------------------------------- filters */

  function selectField(spec) {
    var select = el('select', {
      className: 'select',
      attrs: { id: spec.id, 'aria-label': spec.label },
      on: {
        change: function (event) { spec.onChange(spec.key, event.target.value); }
      }
    });
    var options = spec.options || [];
    for (var i = 0; i < options.length; i++) {
      var option = options[i];
      var node = el('option', {
        attrs: { value: option.value },
        text: option.label
      });
      if (String(option.value) === String(spec.value === undefined || spec.value === null ? '' : spec.value)) {
        node.selected = true;
      }
      select.appendChild(node);
    }
    return el('div', { className: 'field' }, [
      el('label', { className: 'field__label', attrs: { for: spec.id }, text: spec.label }),
      select
    ]);
  }

  function searchField(spec) {
    var input = el('input', {
      className: 'input',
      attrs: {
        id: spec.id, type: 'search', placeholder: spec.placeholder || 'Search',
        'aria-label': spec.label, value: spec.value || '', autocomplete: 'off',
        spellcheck: 'false'
      }
    });
    var commit = util.debounce(function () { spec.onChange(spec.key, input.value); }, 350, 1200);
    input.addEventListener('input', commit);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { spec.onChange(spec.key, input.value); }
      if (event.key === 'Escape') { input.value = ''; spec.onChange(spec.key, ''); }
    });
    return el('div', { className: 'field field--grow' }, [
      el('label', { className: 'field__label', attrs: { for: spec.id }, text: spec.label }),
      input
    ]);
  }

  /**
   * A radio-like group of buttons.
   *
   * Returns `{el, sync}` rather than a bare node: the pressed option changes on
   * every navigation, and rebuilding the group each time would throw away the
   * button the user is standing on. `sync` only moves `aria-pressed`.
   */
  function segmented(spec) {
    var buttons = [];
    var group = el('div', {
      className: 'segmented',
      attrs: { role: 'group', 'aria-label': spec.label }
    });
    for (var i = 0; i < spec.options.length; i++) {
      (function (option) {
        var button = el('button', {
          attrs: { type: 'button', 'aria-pressed': 'false' },
          dataset: { value: option.value },
          text: option.label,
          on: { click: function () { spec.onChange(option.value); } }
        });
        buttons.push(button);
        group.appendChild(button);
      })(spec.options[i]);
    }

    function sync(value) {
      var target = String(value === undefined || value === null ? '' : value);
      for (var k = 0; k < buttons.length; k++) {
        buttons[k].setAttribute(
          'aria-pressed',
          String(String(spec.options[k].value) === target)
        );
      }
    }
    sync(spec.value);

    return {
      el: el('div', { className: 'field' }, [
        el('span', { className: 'field__label', text: spec.label }),
        group
      ]),
      sync: sync
    };
  }

  /**
   * A filter bar that owns its own DOM across re-renders.
   *
   * Rebuilt-from-scratch filters are how a dashboard eats the character you
   * just typed: the store updates, the view re-renders, the input is a new
   * node and the caret is gone. So this returns a stable element plus a
   * `sync(values, facets)` that edits the controls in place, and only replaces
   * a select's options when the facet list itself has changed.
   */
  /**
   * The portfolio filter bar's fields, in one place: the overview and the
   * analytics page slice the same data by the same seven dimensions, and two
   * copies of this list is two lists that drift.
   */
  function portfolioFields() {
    return [
      {
        key: 'window_days', label: 'Window', type: 'segmented', defaultValue: '30',
        options: [
          { value: '7', label: '7d' }, { value: '14', label: '14d' },
          { value: '30', label: '30d' }, { value: '90', label: '90d' }
        ]
      },
      { key: 'project', label: 'Project' },
      { key: 'team', label: 'Team' },
      { key: 'component', label: 'Component' },
      { key: 'test_type', label: 'Test type', facet: 'test_types' },
      { key: 'environment', label: 'Environment' },
      { key: 'branch', label: 'Branch', facet: 'branches' }
    ];
  }

  function filterPanel(spec) {
    var controls = {};
    var row = el('div', { className: 'filters__row' });
    var chips = el('div', { className: 'chips' });
    var i;

    for (i = 0; i < spec.fields.length; i++) {
      (function (field) {
        var id = 'filter-' + field.key;
        if (field.type === 'search') {
          var searchNode = searchField({
            id: id, key: field.key, label: field.label,
            placeholder: field.placeholder, value: spec.values[field.key] || '',
            onChange: spec.onChange
          });
          controls[field.key] = {
            type: 'search', field: field, node: searchNode,
            input: searchNode.querySelector('input')
          };
          row.appendChild(searchNode);
          return;
        }
        if (field.type === 'segmented') {
          var holder = el('div', { className: 'field-holder' });
          // Built once, here, where `field` is captured by this IIFE. Building
          // it inside `sync` instead would close over the loop variable and
          // every button would end up writing the last field's filter key.
          var control = segmented({
            label: field.label,
            value: spec.values[field.key] || field.defaultValue,
            options: field.options,
            onChange: function (next) { spec.onChange(field.key, next); }
          });
          holder.appendChild(control.el);
          controls[field.key] = {
            type: 'segmented', holder: holder, field: field, control: control
          };
          row.appendChild(holder);
          return;
        }
        var wrapper = el('div', { className: 'field-holder' });
        controls[field.key] = { type: 'select', holder: wrapper, field: field, signature: null };
        row.appendChild(wrapper);
      })(spec.fields[i]);
    }

    var reset = el('button', {
      className: 'btn btn--ghost btn--sm', attrs: { type: 'button' },
      text: 'Reset', on: { click: spec.onReset }
    });
    row.appendChild(el('div', { className: 'filters__spacer' }));
    row.appendChild(el('div', { className: 'filters__actions' }, [reset]));

    var root = el('section', {
      className: 'filters',
      attrs: { 'aria-label': spec.label || 'Filters' }
    }, [row, chips]);

    function optionsFor(field, facets) {
      var values = (facets && facets[field.facet || (field.key + 's')]) || [];
      var options = [{
        value: '',
        label: field.allLabel || ('All ' + util.pluralLabel(field.label).toLowerCase())
      }];
      for (var k = 0; k < values.length; k++) {
        options.push({ value: values[k], label: util.titleCase(values[k]) });
      }
      return options;
    }

    function sync(values, facets) {
      var keys = Object.keys(controls);
      var active = [];
      for (var k = 0; k < keys.length; k++) {
        var key = keys[k];
        var control = controls[key];
        var value = values[key] === undefined || values[key] === null ? '' : String(values[key]);
        if (control.type === 'search') {
          if (document.activeElement !== control.input && control.input.value !== value) {
            control.input.value = value;
          }
        } else if (control.type === 'segmented') {
          control.control.sync(value || control.field.defaultValue);
        } else {
          var options = control.field.options || optionsFor(control.field, facets);
          var signature = options.map(function (option) { return option.value; }).join('\u001f');
          if (control.signature !== signature) {
            control.signature = signature;
            util.replace(control.holder, selectField({
              id: 'filter-' + key, key: key, label: control.field.label,
              options: options, value: value, onChange: spec.onChange
            }));
          } else {
            var select = control.holder.querySelector('select');
            if (select && select.value !== value) { select.value = value; }
          }
        }
        // The window selector is always set and is shown as a control, not as
        // a chip; chips are for narrowings the user can be surprised by.
        if (value && control.type !== 'segmented') {
          active.push([key, value, (control.field && control.field.label) ||
            (control.type === 'search' ? 'Search' : key)]);
        }
      }

      util.clear(chips);
      for (var a = 0; a < active.length; a++) {
        (function (entry) {
          chips.appendChild(el('span', { className: 'chip' }, [
            document.createTextNode(entry[2] + ': ' + entry[1]),
            el('button', {
              className: 'chip__remove',
              attrs: { type: 'button', 'aria-label': 'Remove filter ' + entry[2] },
              text: '\u00d7',
              on: { click: function () { spec.onChange(entry[0], ''); } }
            })
          ]));
        })(active[a]);
      }
      chips.hidden = active.length === 0;
    }

    return { el: root, sync: sync };
  }

  /* ---------------------------------------------------------------- menu */

  /* An anchored popover with a trigger. The dashboard's first, so the keyboard
   * contract is written here once rather than per caller:
   *
   *   Enter/Space/click  toggle, and on open move focus to the first item
   *   ArrowDown/Up       roving focus within the panel, wrapping
   *   Home/End           first / last item
   *   Escape             close and return focus to the trigger
   *   Tab                close, and let focus leave naturally
   *   click outside      close, without stealing focus
   *
   * Returning focus to the trigger on Escape is the part that is easy to skip
   * and impossible to work around: without it a keyboard user who opens the
   * menu and changes their mind is dropped at the top of the document.
   *
   * `items` are built by the caller and are expected to carry a menu role
   * (`menuitem`, `menuitemradio`); anything focusable inside the panel is
   * treated as an item for arrow-key purposes.
   */
  function menu(spec) {
    var id = spec.id || ('th-menu-' + Math.random().toString(36).slice(2, 8));
    var open = false;

    var panel = el('div', {
      className: 'menu__panel',
      attrs: {
        id: id,
        role: 'menu',
        'aria-label': spec.label || 'Menu',
        hidden: true
      }
    });

    var trigger = el('button', {
      className: 'iconbtn menu__trigger',
      attrs: {
        type: 'button',
        'aria-haspopup': 'true',
        'aria-expanded': 'false',
        'aria-controls': id,
        'aria-label': spec.label || 'Menu',
        title: spec.title || spec.label || 'Menu'
      }
    }, icon(spec.icon || 'gear', 18));

    var root = el('div', { className: 'menu' }, [trigger, panel]);

    function items() {
      return panel.querySelectorAll('[role^="menuitem"], button, [href], input, select');
    }

    function focusItem(index) {
      var list = items();
      if (!list.length) { return; }
      var wrapped = (index + list.length) % list.length;
      list[wrapped].focus();
    }

    function indexOfActive() {
      var list = items();
      for (var i = 0; i < list.length; i++) {
        if (list[i] === document.activeElement) { return i; }
      }
      return -1;
    }

    function setOpen(next, options) {
      if (open === next) { return; }
      open = next;
      panel.hidden = !next;
      root.setAttribute('data-open', String(next));
      trigger.setAttribute('aria-expanded', String(next));
      if (next) {
        if (spec.onOpen) { spec.onOpen(panel); }
        if (!options || options.focus !== false) { focusItem(0); }
      } else if (options && options.restoreFocus) {
        trigger.focus();
      }
    }

    trigger.addEventListener('click', function () {
      setOpen(!open, { focus: true });
    });

    panel.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.stopPropagation();
        setOpen(false, { restoreFocus: true });
      } else if (event.key === 'ArrowDown') {
        event.preventDefault();
        focusItem(indexOfActive() + 1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        focusItem(indexOfActive() - 1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        focusItem(0);
      } else if (event.key === 'End') {
        event.preventDefault();
        focusItem(items().length - 1);
      } else if (event.key === 'Tab') {
        setOpen(false);
      }
    });

    trigger.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown' && !open) {
        event.preventDefault();
        setOpen(true, { focus: true });
      } else if (event.key === 'Escape' && open) {
        setOpen(false, { restoreFocus: true });
      }
    });

    // Pointer-down rather than click: a click that starts inside the panel and
    // ends outside it — a drag over a label — would otherwise close the menu.
    function onDocumentMouseDown(event) {
      if (!open || root.contains(event.target)) { return; }
      setOpen(false);
    }

    // Focus leaving the menu entirely closes it, which covers Tab out and
    // clicking straight into another control.
    function onDocumentFocusIn(event) {
      if (!open || root.contains(event.target)) { return; }
      setOpen(false);
    }

    document.addEventListener('mousedown', onDocumentMouseDown);
    document.addEventListener('focusin', onDocumentFocusIn);

    return {
      el: root,
      panel: panel,
      trigger: trigger,
      open: function () { setOpen(true, { focus: true }); },
      close: function (options) { setOpen(false, options); },
      isOpen: function () { return open; },
      // These two listeners are on `document`, not on the menu, so dropping the
      // element is not enough to collect them — a caller that mounts a menu per
      // view would otherwise accumulate one pair per mount.
      destroy: function () {
        document.removeEventListener('mousedown', onDocumentMouseDown);
        document.removeEventListener('focusin', onDocumentFocusIn);
      }
    };
  }

  /* The dark-mode switch that sits at the foot of the rail.
   *
   * It reads the *painted* theme rather than the stored preference: with
   * nothing stored the page is following the operating system, and a switch
   * reading storage would sit at "off" on a dark page. Flipping it is what
   * turns the OS default into an explicit choice.
   *
   * `role="switch"` with `aria-checked` is what is announced; the track and
   * thumb are drawn, and only follow. The listener is on the theme, not on the
   * button, so the switch is also right when the OS changes underneath it.
   */
  function themeSwitch() {
    var track = el('span', { className: 'switch' }, [
      el('span', { className: 'switch__thumb' })
    ]);

    var node = el('button', {
      className: 'switchrow',
      attrs: {
        type: 'button',
        role: 'switch',
        'aria-checked': 'false',
        'aria-label': 'Dark mode'
      },
      on: {
        click: function () {
          util.theme.toggle();
          // `toggle` notifies the theme listeners, and `sync` is one of them,
          // so the switch moves without being told twice.
        }
      }
    }, [
      icon('moon', 17),
      el('span', { className: 'switchrow__label', text: 'Dark mode' }),
      track
    ]);

    function sync() {
      node.setAttribute('aria-checked', String(util.theme.current() === 'dark'));
    }

    var stopListening = util.theme.onChange(sync);
    sync();

    return {
      el: node,
      destroy: function () { if (stopListening) { stopListening(); } }
    };
  }

  function menuSection(title, children) {
    return el('div', { className: 'menu__section' }, [
      title ? el('div', { className: 'menu__label', text: title }) : null,
      el('div', { className: 'menu__group' }, children)
    ]);
  }

  TestHub.ui = {
    icon: icon,
    menu: menu,
    menuSection: menuSection,
    themeSwitch: themeSwitch,
    statusPill: statusPill,
    liveBadge: liveBadge,
    tag: tag,
    tagList: tagList,
    link: link,
    externalLink: externalLink,
    deltaText: deltaText,
    countBar: countBar,
    countsSentence: countsSentence,
    card: card,
    section: section,
    kpi: kpi,
    statTile: statTile,
    meter: meter,
    chip: chip,
    verdictBadge: verdictBadge,
    statStrip: statStrip,
    streak: streak,
    kv: kv,
    metaGrid: metaGrid,
    banner: banner,
    skeleton: skeleton,
    loadingBlock: loadingBlock,
    emptyState: emptyState,
    errorState: errorState,
    table: table,
    pager: pager,
    selectField: selectField,
    filterPanel: filterPanel,
    portfolioFields: portfolioFields,
    searchField: searchField,
    segmented: segmented
  };
})(window, document);
