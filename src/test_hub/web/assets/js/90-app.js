/* Test Hub — boot, routing and the chrome around the views.
 *
 * The one decision this file makes that the rest of the app must never make:
 * which provider is mounted. There is one — the live REST + SSE client — and it
 * is named here and nowhere else, so a view still cannot tell where its data
 * came from (ADR-0001 D1, narrowed by ADR-0003).
 *
 * Everything else here is chrome: the header's connection pill and last-updated
 * clock, the settings menu, and a router that mounts one view at a time and
 * lets it own its own DOM.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

  var VIEWS = {
    overview: function () { return TestHub.views.overview; },
    analytics: function () { return TestHub.views.analytics; },
    suites: function () { return TestHub.views.suites; },
    metrics: function () { return TestHub.views.metrics; },
    steps: function () { return TestHub.views.steps; },
    screenshots: function () { return TestHub.views.screenshots; },
    logs: function () { return TestHub.views.logs; },
    coverage: function () { return TestHub.views.coverage; },
    runs: function () { return TestHub.views.runs; },
    run: function () { return TestHub.views.run; },
    test: function () { return TestHub.views.test; },
    settings: function () { return TestHub.views.settings; }
  };

  var STATUS_TEXT = {
    connecting: 'Connecting',
    live: 'Live',
    reconnecting: 'Reconnecting',
    offline: 'Offline'
  };

  //: The status details' headline: the pill says the state, this says what it means.
  var STATUS_HEADLINE = {
    connecting: 'Connecting to live updates',
    live: 'Live updates are on',
    reconnecting: 'Reconnecting to live updates',
    offline: 'Live updates are offline'
  };

  //: What the status details say when the provider has nothing more specific.
  var STATUS_HELP = {
    connecting: 'Opening the live stream to the Test Hub server.',
    live: 'Connected. Results land on this page as they arrive.',
    reconnecting: 'The live stream dropped. It reconnects by itself, and the page ' +
      'catches up on everything it missed when it does.',
    offline: 'The numbers on this page are the last ones that arrived. Check that ' +
      'the server is still running.'
  };

  function boot() {
    var provider = TestHub.LiveDataProvider.create();

    var store = TestHub.store.create(provider);
    var ctx = { store: store, provider: provider };

    var viewHost = document.getElementById('th-view');
    var connSlot = document.getElementById('th-conn-slot');
    var connPill = document.getElementById('th-conn');
    var connLabel = document.getElementById('th-conn-label');
    var connAnnounce = document.getElementById('th-conn-announce');
    var updatedNode = document.getElementById('th-updated');
    var settingsHost = document.getElementById('th-settings');
    var appearanceHost = document.getElementById('th-appearance');
    var brandSub = document.getElementById('th-brand-sub');
    var footerMeta = document.getElementById('th-footer-meta');
    var navLinks = document.querySelectorAll('[data-route]');

    /* The rail's icons come from the one icon set rather than from inline SVG
     * in index.html, so there is a single copy of each path to fix. */
    for (var n = 0; n < navLinks.length; n++) {
      var iconName = navLinks[n].getAttribute('data-icon');
      if (iconName) {
        navLinks[n].insertBefore(ui.railIcon(iconName, 16), navLinks[n].firstChild);
      }
    }

    var current = { name: null, view: null };

    /* ---------------------------------------------------------- settings */

    // Mounted after the provider is chosen, because the About block reports
    // what it is connected to.
    if (settingsHost) {
      util.replace(settingsHost, TestHub.settings.create(ctx).el);
    }

    // The theme is already resolved by the bootstrap in 00-util.js; this only
    // mounts the control that changes it.
    if (appearanceHost) {
      util.replace(appearanceHost, ui.themeSwitch().el);
    }

    /* -------------------------------------------------------------- head */

    /* The connection pill. It sits in a slot as wide as its longest label —
     * every label is laid into the slot's one grid cell, hidden — so a status
     * change re-labels the pill and moves nothing else on the page. The
     * details, and the way to retry, open over the page in a popover; there
     * is no banner pushing the view down while the stream is away. */
    var conn = (function () {
      if (!connPill || !connSlot) { return null; }
      var labels = Object.keys(STATUS_TEXT);
      for (var k = 0; k < labels.length; k++) {
        connSlot.insertBefore(el('span', {
          className: 'conn conn-slot__sizer',
          attrs: { 'aria-hidden': 'true' }
        }, [
          el('span', { className: 'conn__dot' }),
          el('span', { text: STATUS_TEXT[labels[k]] })
        ]), connPill);
      }

      var headline = el('span');
      var head = el('div', { className: 'connpop__head' }, [
        el('span', { className: 'conn__dot', attrs: { 'aria-hidden': 'true' } }),
        headline
      ]);
      var text = el('p', { className: 'connpop__text' });
      var rows = el('dl', { className: 'connpop__rows' });
      var actions = el('div', { className: 'connpop__actions' });

      var pop = ui.popover({
        trigger: connPill,
        label: 'Live updates',
        className: 'connpop',
        align: 'end',
        body: [head, text, rows, actions],
        onOpen: function () { fill(store.state); }
      });
      connSlot.appendChild(pop.el);

      function row(term, value) {
        rows.appendChild(el('dt', { text: term }));
        rows.appendChild(el('dd', { text: value }));
      }

      /** The API's host, not its whole URL: the path is the same everywhere. */
      function hostOf(base) {
        try {
          return new URL(base, window.location.href).host || base;
        } catch (err) {
          return base;
        }
      }

      function fill(state) {
        var status = state.status || provider.status;
        head.setAttribute('data-status', status);
        headline.textContent = STATUS_HEADLINE[status] ||
          'Live updates: ' + (STATUS_TEXT[status] || util.titleCase(status));
        text.textContent = STATUS_HELP[status] || '';

        util.clear(rows);
        // What the provider knows about this particular drop — "the browser
        // reports no network", "no heartbeat" — under the general explanation
        // rather than instead of it.
        if (state.statusDetail && status !== 'live') { row('Reason', state.statusDetail); }
        var stamp = state.lastUpdated;
        row('Last update', stamp ? util.timeAgo(stamp) + ' \u00b7 ' + util.formatTime(stamp) : 'Not yet');
        var meta = provider.meta || {};
        if (meta.apiBase) { row('Server', hostOf(meta.apiBase)); }

        util.clear(actions);
        if (status === 'reconnecting' || status === 'offline') {
          actions.appendChild(el('button', {
            className: 'btn btn--sm',
            attrs: { type: 'button' },
            on: {
              click: function () {
                if (provider.reconnect) { provider.reconnect(); }
                store.reload();
              }
            }
          }, [ui.icon('refresh', 14), el('span', { text: 'Retry now' })]));
        }
      }

      return { pop: pop, fill: fill };
    })();

    var announced = store.state.status || provider.status;

    function syncStatus(state) {
      if (!connPill) { return; }
      var status = state.status || provider.status;
      var text = STATUS_TEXT[status] || util.titleCase(status);
      connPill.setAttribute('data-status', status);
      if (connLabel && connLabel.textContent !== text) { connLabel.textContent = text; }
      connPill.setAttribute('aria-label', 'Live updates: ' + text + '. Show details');
      connPill.setAttribute('title', 'Live updates: ' + text);
      // Said once per change, by a live region of its own: the pill is a
      // button now, and a button that announces itself on every repaint is
      // noise.
      if (connAnnounce && status !== announced) {
        announced = status;
        connAnnounce.textContent = 'Live updates: ' + text;
      }
      if (conn && conn.pop.isOpen()) { conn.fill(state); }
    }

    function syncUpdated(state) {
      if (!updatedNode) { return; }
      var stamp = state.lastUpdated ||
        (provider.meta && provider.meta.generated_at) || null;
      if (!stamp) {
        updatedNode.textContent = '';
        return;
      }
      updatedNode.textContent = 'Updated ' + util.timeAgo(stamp);
      updatedNode.setAttribute('title', util.formatDateTime(stamp));
      if (conn && conn.pop.isOpen()) { conn.fill(state); }
    }

    var navScroller = document.querySelector('.nav');

    function syncNav(state) {
      var name = state.route.name === 'run' || state.route.name === 'test'
        ? 'runs'
        : state.route.name;
      var active = null;
      for (var i = 0; i < navLinks.length; i++) {
        var link = navLinks[i];
        if (link.getAttribute('data-route') === name) {
          link.setAttribute('aria-current', 'page');
          active = link;
        } else {
          link.removeAttribute('aria-current');
        }
      }
      // On a phone the rail is a horizontal strip that scrolls, and nine routes
      // do not fit in it. Landing on a page whose own link is off the end of
      // that strip reads as "this page is not in the nav", so the strip is
      // moved rather than the reader being asked to go looking. `scrollLeft` is
      // set directly rather than using scrollIntoView, which would also scroll
      // the document and take the page heading off the top of the screen.
      if (active && navScroller && navScroller.scrollWidth > navScroller.clientWidth) {
        var margin = 16;
        var left = active.offsetLeft - margin;
        var right = active.offsetLeft + active.offsetWidth + margin;
        if (left < navScroller.scrollLeft) {
          navScroller.scrollLeft = Math.max(0, left);
        } else if (right > navScroller.scrollLeft + navScroller.clientWidth) {
          navScroller.scrollLeft = right - navScroller.clientWidth;
        }
      }
    }

    function syncMeta() {
      var meta = provider.meta || {};
      if (brandSub) {
        brandSub.textContent = 'Live';
        brandSub.title = brandSub.textContent;
      }
      if (footerMeta) {
        util.clear(footerMeta);
        var parts = [];
        if (meta.generator_version) { parts.push('test-hub ' + meta.generator_version); }
        if (meta.schema_version) { parts.push('schema v' + meta.schema_version); }
        if (meta.apiBase) { parts.push('API ' + meta.apiBase); }
        footerMeta.textContent = parts.join(' · ');
      }
    }

    /* ------------------------------------------------------------ router */

    function mount(state) {
      var name = state.route.name;
      if (current.name === name && current.view) { return; }
      if (current.view && current.view.destroy) { current.view.destroy(); }
      var factory = VIEWS[name] || VIEWS.overview;
      var module = factory();
      current = { name: name, view: module.create(ctx) };
      util.replace(viewHost, current.view.el);
    }

    store.on(function (event) {
      var state = event.state;
      if (event.section === 'route') {
        mount(state);
        syncNav(state);
      }
      if (!current.view) { mount(state); }
      if (event.section === 'status') { syncStatus(state); }
      syncUpdated(state);
      if (current.view) { current.view.update(state, event.section); }
    });

    window.addEventListener('hashchange', function () {
      store.applyRoute();
    });

    // The relative clock in the header has to move on its own; otherwise a page
    // left open all afternoon still claims it was updated "just now".
    window.setInterval(function () { syncUpdated(store.state); }, 30000);

    syncMeta();
    syncStatus(store.state);
    syncUpdated(store.state);
    store.start();
    // The provider learns the server's version a round trip after start, so
    // the footer is drawn twice: once with what we knew, once with what the
    // server said.
    if (provider.serverInfo && provider.serverInfo.then) {
      provider.serverInfo.then(syncMeta);
    }
    mount(store.state);
    syncNav(store.state);
    if (current.view) { current.view.update(store.state, 'route'); }
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  ready(function () {
    try {
      boot();
    } catch (err) {
      util.reportError('the dashboard failed to start', err);
      var host = document.getElementById('th-view');
      if (host) {
        util.replace(host, TestHub.ui.errorState({
          title: 'The dashboard failed to start',
          error: { message: (err && err.message) || String(err), code: 'boot_failed' }
        }));
      }
    }
  });

  TestHub.app = { boot: boot };
})(window, document);
