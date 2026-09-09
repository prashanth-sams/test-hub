/* Test Hub — boot, routing and the chrome around the views.
 *
 * The one decision this file makes that the rest of the app must never make:
 * which provider is mounted. `window.TEST_HUB_SNAPSHOT` present means the page
 * is a self-contained export and everything comes out of that object; absent
 * means there is a server, and everything comes over REST and SSE (ADR-0001
 * D1). After the two lines below, nothing else in the codebase can tell.
 *
 * Everything else here is chrome: the header's connection pill and last-updated
 * clock, the theme toggle, the disconnected banner, and a router that mounts one
 * view at a time and lets it own its own DOM.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

  var VIEWS = {
    overview: function () { return TestHub.views.overview; },
    runs: function () { return TestHub.views.runs; },
    run: function () { return TestHub.views.run; },
    test: function () { return TestHub.views.test; }
  };

  var STATUS_TEXT = {
    connecting: 'Connecting',
    live: 'Live',
    reconnecting: 'Reconnecting',
    offline: 'Offline',
    static: 'Static export'
  };

  function boot() {
    var snapshot = window.TEST_HUB_SNAPSHOT || null;
    var provider = snapshot
      ? TestHub.SnapshotDataProvider.create(snapshot)
      : TestHub.LiveDataProvider.create();

    var store = TestHub.store.create(provider);
    var ctx = { store: store, provider: provider };

    var viewHost = document.getElementById('th-view');
    var bannerHost = document.getElementById('th-banner');
    var connPill = document.getElementById('th-conn');
    var connLabel = document.getElementById('th-conn-label');
    var updatedNode = document.getElementById('th-updated');
    var themeButton = document.getElementById('th-theme');
    var brandSub = document.getElementById('th-brand-sub');
    var footerMeta = document.getElementById('th-footer-meta');
    var navLinks = document.querySelectorAll('[data-route]');

    var current = { name: null, view: null };

    /* ------------------------------------------------------------- theme */

    function syncThemeButton() {
      if (!themeButton) { return; }
      var dark = util.theme.current() === 'dark';
      themeButton.setAttribute('aria-pressed', String(dark));
      themeButton.setAttribute('title', dark ? 'Switch to light theme' : 'Switch to dark theme');
      themeButton.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
    }

    if (themeButton) {
      themeButton.addEventListener('click', function () {
        util.theme.toggle();
      });
    }
    util.theme.onChange(syncThemeButton);
    syncThemeButton();

    /* -------------------------------------------------------------- head */

    function syncStatus(state) {
      if (!connPill) { return; }
      var status = state.status || provider.status;
      connPill.setAttribute('data-status', status);
      var text = STATUS_TEXT[status] || util.titleCase(status);
      if (connLabel) { connLabel.textContent = text; }
      var title = state.statusDetail ||
        (status === 'static'
          ? 'This page is a frozen export. Nothing will change while it is open.'
          : 'Live updates over server-sent events.');
      connPill.setAttribute('title', title);
    }

    function syncUpdated(state) {
      if (!updatedNode) { return; }
      var stamp = state.lastUpdated ||
        (provider.meta && provider.meta.generated_at) || null;
      if (!stamp) {
        updatedNode.textContent = '';
        return;
      }
      var prefix = provider.kind === 'snapshot' && !state.lastUpdated ? 'Exported ' : 'Updated ';
      updatedNode.textContent = prefix + util.timeAgo(stamp);
      updatedNode.setAttribute('title', util.formatDateTime(stamp));
    }

    function syncBanner(state) {
      if (!bannerHost) { return; }
      util.clear(bannerHost);
      if (state.status === 'offline' || state.status === 'reconnecting') {
        bannerHost.appendChild(ui.banner({
          tone: state.status === 'offline' ? 'error' : 'warn',
          icon: 'alert',
          title: state.status === 'offline'
            ? 'Disconnected from the Test Hub server'
            : 'Reconnecting to the live stream',
          text: state.status === 'offline'
            ? 'The numbers on this page are the last ones that arrived. ' +
              (state.statusDetail || 'Check that the server is still running.')
            : (state.statusDetail || 'Live updates will resume by themselves.'),
          actions: [
            el('button', {
              className: 'btn btn--sm',
              attrs: { type: 'button' },
              text: 'Retry now',
              on: {
                click: function () {
                  if (provider.reconnect) { provider.reconnect(); }
                  store.reload();
                }
              }
            })
          ]
        }));
      }
    }

    function syncNav(state) {
      var name = state.route.name === 'run' ? 'runs' : (state.route.name === 'test' ? 'runs' : state.route.name);
      for (var i = 0; i < navLinks.length; i++) {
        var link = navLinks[i];
        if (link.getAttribute('data-route') === name) {
          link.setAttribute('aria-current', 'page');
        } else {
          link.removeAttribute('aria-current');
        }
      }
    }

    function syncMeta() {
      var meta = provider.meta || {};
      if (brandSub) {
        brandSub.textContent = provider.kind === 'snapshot'
          ? (meta.title || 'Snapshot')
          : 'Live';
      }
      if (provider.kind === 'snapshot' && meta.title) {
        document.title = meta.title + ' · Test Hub';
      }
      if (footerMeta) {
        util.clear(footerMeta);
        var parts = [];
        if (meta.generator_version) { parts.push('test-hub ' + meta.generator_version); }
        if (meta.schema_version) { parts.push('schema v' + meta.schema_version); }
        if (provider.kind === 'snapshot') {
          parts.push(util.formatNumber(meta.run_count || 0) + ' runs in this file');
        } else if (meta.apiBase) {
          parts.push('API ' + meta.apiBase);
        }
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
      if (event.section === 'status') {
        syncStatus(state);
        syncBanner(state);
      }
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
