/* Test Hub — the settings menu in the header.
 *
 * Two things about this menu are decisions rather than defaults.
 *
 * **It is settings, not an account.** Test Hub has no users: authentication is
 * one shared bearer token and there is exactly one workspace (ADR-0001 D9), so
 * an avatar here would imply a login that does not exist. The menu shows what
 * there actually is — where the data is kept, and what this page is connected
 * to.
 *
 * **The display preference is not in here.** The dark-mode switch lives at the
 * foot of the navigation rail (`ui.themeSwitch`), where it is one click rather
 * than two and visible without opening anything. What is left in this menu is
 * the doorway to retention and the About block — the things you go looking for
 * rather than flip in passing.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

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
     * something irreversible — it is small, it closes when focus moves, and a
     * dropdown is no place to put "delete 4,000 rows" one stray click from
     * anything else. This is the doorway; the preview and the confirmation are
     * on the page. */
    var dataItems = [
      el('a', {
        className: 'menu__item',
        attrs: { role: 'menuitem', href: '#/settings' },
        on: { click: function () { control.close(); } }
      }, [
        ui.icon('gear', 17),
        el('span', { className: 'menu__item-label', text: 'Retention and data\u2026' })
      ])
    ];

    util.appendChild(control.panel, [
      provider.kind === 'snapshot' ? null : ui.menuSection('Data', dataItems),
      about
    ]);
    fillAbout();

    return {
      el: control.el,
      menu: control,
      destroy: control.destroy
    };
  }

  TestHub.settings = { create: create };
})(window, document);
