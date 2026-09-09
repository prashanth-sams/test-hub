/* Test Hub — the settings menu in the header.
 *
 * Two things about this menu are decisions rather than defaults.
 *
 * **It is settings, not an account.** Test Hub has no users: authentication is
 * one shared bearer token and there is exactly one workspace (ADR-0001 D9), so
 * an avatar here would imply a login that does not exist. The menu shows what
 * there actually is — a display preference and what this page is connected to.
 *
 * **Theme has three states, not two.** The old header button flipped
 * light/dark, which meant that once a reader had clicked it once they could
 * never get back to following their operating system; `theme.followSystem()`
 * existed in 00-util.js with nothing calling it. "System" is the absence of a
 * stored choice, so it is the honest default and the one this menu can return
 * you to.
 *
 * The menu stays open when the theme changes. That is deliberate: the whole
 * page repaints underneath it, and closing would hide the thing the reader
 * just asked to see.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

  var THEME_OPTIONS = [
    { mode: 'system', label: 'System', icon: 'monitor' },
    { mode: 'light', label: 'Light', icon: 'sun' },
    { mode: 'dark', label: 'Dark', icon: 'moon' }
  ];

  function themeItem(option, onChoose) {
    var check = ui.icon('check', 14);
    check.setAttribute('class', 'menu__check');

    var node = el('button', {
      className: 'menu__item',
      attrs: {
        type: 'button',
        role: 'menuitemradio',
        'aria-checked': 'false'
      },
      dataset: { mode: option.mode },
      on: {
        click: function () { onChoose(option.mode); }
      }
    }, [
      ui.icon(option.icon, 15),
      el('span', { className: 'menu__item-label', text: option.label }),
      check
    ]);
    return node;
  }

  function aboutRow(label, value) {
    if (!value) { return null; }
    return el('div', { className: 'menu__row' }, [
      el('span', { className: 'menu__row-key', text: label }),
      el('span', { className: 'menu__row-value', text: value })
    ]);
  }

  /* The About block answers "what am I looking at" — which for this product is
   * a genuine question, because the same dashboard code renders both a live
   * server and a frozen file (ADR-0001 D1) and the two look identical. */
  function aboutRows(provider) {
    var meta = provider.meta || {};
    var snapshot = provider.kind === 'snapshot';
    var rows = [
      aboutRow('Source', snapshot ? 'Offline export' : 'Live server'),
      aboutRow('Version', meta.generator_version ? 'test-hub ' + meta.generator_version : null),
      aboutRow('Schema', meta.schema_version ? 'v' + meta.schema_version : null)
    ];
    if (snapshot) {
      rows.push(aboutRow('Runs in file', util.formatNumber(meta.run_count || 0)));
      if (meta.generated_at) {
        rows.push(aboutRow('Exported', util.formatDateTime(meta.generated_at)));
      }
    } else {
      rows.push(aboutRow('API', meta.apiBase || '/api/v1'));
    }
    return rows;
  }

  function create(ctx) {
    var provider = (ctx && ctx.provider) || {};
    var items = [];

    function choose(mode) {
      util.theme.setMode(mode);
      // `setMode` notifies the theme listeners, and `sync` is one of them, so
      // the checkmarks move without being told twice.
    }

    var themeItems = [];
    for (var i = 0; i < THEME_OPTIONS.length; i++) {
      var node = themeItem(THEME_OPTIONS[i], choose);
      items.push({ mode: THEME_OPTIONS[i].mode, node: node });
      themeItems.push(node);
    }

    var systemHint = el('div', { className: 'menu__hint' });
    var about = el('div', { className: 'menu__about' });

    /* Rebuilt on every open rather than once at mount. The live provider learns
     * the server's version a round trip after boot (see `serverInfo`), so a
     * block built at mount time would be permanently missing the two rows that
     * make it worth having. */
    function fillAbout() {
      util.replace(about, ui.menuSection('About', aboutRows(provider)));
    }

    var control = ui.menu({
      id: 'th-settings-menu',
      icon: 'gear',
      label: 'Settings',
      title: 'Settings',
      onOpen: fillAbout
    });

    /* Retention lives on a page, not in here. A dropdown is the wrong shape for
     * something irreversible — it is small, it closes when focus moves, and it
     * would put "delete 4,000 rows" one stray click from the theme switch. This
     * is the doorway; the preview and the confirmation are on the page. */
    var dataItems = [
      el('a', {
        className: 'menu__item',
        attrs: { role: 'menuitem', href: '#/settings' },
        on: { click: function () { control.close(); } }
      }, [
        ui.icon('gear', 15),
        el('span', { className: 'menu__item-label', text: 'Retention and data\u2026' })
      ])
    ];

    util.appendChild(control.panel, [
      ui.menuSection('Theme', themeItems),
      systemHint,
      provider.kind === 'snapshot' ? null : ui.menuSection('Data', dataItems),
      about
    ]);
    fillAbout();

    function sync() {
      var mode = util.theme.mode();
      for (var index = 0; index < items.length; index++) {
        var selected = items[index].mode === mode;
        items[index].node.setAttribute('aria-checked', String(selected));
        items[index].node.setAttribute('data-selected', String(selected));
      }
      systemHint.textContent = mode === 'system'
        ? 'Following your operating system, which is ' + util.theme.system() + ' right now.'
        : 'Your choice overrides the operating system on this browser.';
    }

    var stopListening = util.theme.onChange(sync);
    sync();

    return {
      el: control.el,
      menu: control,
      destroy: function () {
        if (stopListening) { stopListening(); }
        control.destroy();
      }
    };
  }

  TestHub.settings = { create: create, THEME_OPTIONS: THEME_OPTIONS };
})(window, document);
