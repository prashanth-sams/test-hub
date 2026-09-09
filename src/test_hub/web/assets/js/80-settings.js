/* Test Hub — the settings menu in the header.
 *
 * Two things about this menu are decisions rather than defaults.
 *
 * **It is settings, not an account.** Test Hub has no users: authentication is
 * one shared bearer token and there is exactly one workspace (ADR-0001 D9), so
 * an avatar here would imply a login that does not exist. The menu shows what
 * there actually is — a display preference and what this page is connected to.
 *
 * **Theme is one row.** A menu this small should not spend half its height on
 * a display preference, so the three theme options collapsed into a single
 * "Dark mode" switch. The switch reads the *painted* theme rather than the
 * stored one, so a reader who has never chosen sees their operating system
 * reflected back and flipping it is what turns that into an explicit choice.
 * Following the OS again means clearing the choice — `util.theme.followSystem()`
 * still does it, and the OS still wins until there is a stored choice, but the
 * menu no longer offers a control for it.
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

  /* A `switch` rather than a `menuitemcheckbox`: it is a two-state display
   * preference that takes effect immediately, not a choice among the menu's
   * navigation items. The track is decorative — `aria-checked` on the button is
   * what is announced. */
  function darkModeSwitch(onToggle) {
    var track = el('span', { className: 'switch' }, [
      el('span', { className: 'switch__thumb' })
    ]);

    return el('button', {
      className: 'menu__item menu__item--switch',
      attrs: {
        type: 'button',
        role: 'switch',
        'aria-checked': 'false'
      },
      on: {
        click: onToggle
      }
    }, [
      ui.icon('moon', 15),
      el('span', { className: 'menu__item-label', text: 'Dark mode' }),
      track
    ]);
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

    var themeSwitch = darkModeSwitch(function () {
      util.theme.toggle();
      // `toggle` notifies the theme listeners, and `sync` is one of them, so
      // the switch moves without being told twice.
    });

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
      ui.menuSection(null, [themeSwitch]),
      provider.kind === 'snapshot' ? null : ui.menuSection('Data', dataItems),
      about
    ]);
    fillAbout();

    function sync() {
      themeSwitch.setAttribute('aria-checked', String(util.theme.current() === 'dark'));
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

  TestHub.settings = { create: create };
})(window, document);
