/* Test Hub documentation site — the whole client.
 *
 * Classic script, no modules, no dependencies, no build step. ES modules do not
 * load over `file://`, and these pages have to open identically from a cloned
 * repository on a laptop with no network and from GitHub Pages. That is the
 * same constraint the product's own dashboard is written under (ADR-0001 D7).
 *
 * Five features live here:
 *
 *   theme     the toggle. The *initial* theme is decided by the small script
 *             `tools/build.py` inlines into every <head>, before first paint;
 *             all this file adds is the switch and the "follow the OS until
 *             the reader chooses" behaviour.
 *   copy      a button on every code block, created here rather than baked
 *             into the HTML — see below.
 *   nav       the mobile slide-in sidebar.
 *   search    Ctrl/Cmd-K over the generated index.
 *   toc       highlighting the section you are actually reading.
 *
 * Every localStorage access is wrapped. Reading it throws outright in a browser
 * configured to block site data, and over `file://` in some builds — and a docs
 * page that fails to render because of a colour preference is a bad trade.
 */
(function (window, document) {
  'use strict';

  var THEME_KEY = 'testHubDocsTheme';
  var DARK_QUERY = '(prefers-color-scheme: dark)';

  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try { window.localStorage.setItem(key, value); return true; } catch (e) { return false; }
  }

  function el(tag, props, children) {
    var node = document.createElement(tag);
    var key;
    for (key in (props || {})) {
      if (!Object.prototype.hasOwnProperty.call(props, key)) { continue; }
      if (key === 'text') { node.textContent = props[key]; }
      else if (key === 'html') { node.innerHTML = props[key]; }
      else if (key in node) { node[key] = props[key]; }
      else { node.setAttribute(key, props[key]); }
    }
    (children || []).forEach(function (child) {
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function svg(markup, extra) {
    var wrapper = document.createElement('div');
    wrapper.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ' +
      'focusable="false" class="' + (extra || '') + '">' + markup + '</svg>';
    return wrapper.firstChild;
  }

  /* ================================================================ theme */

  function setupTheme() {
    var button = document.querySelector('.themetoggle');
    var media = window.matchMedia ? window.matchMedia(DARK_QUERY) : null;

    function current() {
      return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    }
    function apply(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      if (button) {
        button.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
        button.setAttribute('title', theme === 'dark' ? 'Switch to the light theme'
                                                      : 'Switch to the dark theme');
      }
    }

    apply(current());

    if (button) {
      button.addEventListener('click', function () {
        var next = current() === 'dark' ? 'light' : 'dark';
        apply(next);
        // From here on this reader has an opinion, and it outranks the OS.
        storageSet(THEME_KEY, next);
      });
    }

    // Follow the operating system, but only until the reader chooses. After an
    // explicit choice, a colleague changing their desktop to dark at sunset
    // must not silently overrule it.
    if (media && media.addEventListener) {
      media.addEventListener('change', function (event) {
        if (storageGet(THEME_KEY)) { return; }
        apply(event.matches ? 'dark' : 'light');
      });
    }
  }

  /* ================================================================= copy */

  var COPY_IC = '<rect x="9" y="9" width="12" height="12" rx="2"/>' +
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>';
  var COPY_OK = '<path d="m20 6-11 11-5-5"/>';

  function setupCopyButtons() {
    // No clipboard API — an insecure origin, or an old browser — means no
    // button. A button that silently does nothing is worse than no button.
    if (!navigator.clipboard || !navigator.clipboard.writeText) { return; }

    var blocks = document.querySelectorAll('.codeblock');
    Array.prototype.forEach.call(blocks, function (block) {
      var pre = block.querySelector('pre');
      if (!pre) { return; }
      var head = block.querySelector('.codeblock__head');
      if (!head) {
        head = el('div', { className: 'codeblock__head' });
        block.insertBefore(head, block.firstChild);
      }

      var label = el('span', { className: 'copybtn__label', text: 'Copy' });
      var button = el('button', {
        className: 'copybtn',
        type: 'button',
        'aria-label': 'Copy this code block to the clipboard'
      }, [svg(COPY_IC, 'ic'), svg(COPY_OK, 'ok'), label]);

      var reset = null;
      button.addEventListener('click', function () {
        navigator.clipboard.writeText(pre.textContent).then(function () {
          button.classList.add('is-done');
          label.textContent = 'Copied';
          window.clearTimeout(reset);
          reset = window.setTimeout(function () {
            button.classList.remove('is-done');
            label.textContent = 'Copy';
          }, 1600);
        }, function () {
          label.textContent = 'Press Ctrl+C';
        });
      });
      head.appendChild(button);
    });
  }

  /* ============================================================== sidebar */

  function setupNav() {
    var burger = document.querySelector('.navburger');
    var sidebar = document.getElementById('sidebar');
    var scrim = document.querySelector('.navscrim');
    if (!burger || !sidebar) { return; }

    function close() {
      sidebar.classList.remove('is-open');
      burger.setAttribute('aria-expanded', 'false');
      if (scrim) { scrim.hidden = true; }
    }
    function open() {
      sidebar.classList.add('is-open');
      burger.setAttribute('aria-expanded', 'true');
      if (scrim) { scrim.hidden = false; }
      var first = sidebar.querySelector('a');
      if (first) { first.focus(); }
    }

    burger.addEventListener('click', function () {
      if (sidebar.classList.contains('is-open')) { close(); } else { open(); }
    });
    if (scrim) { scrim.addEventListener('click', close); }
    sidebar.addEventListener('click', function (event) {
      if (event.target.closest('a')) { close(); }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && sidebar.classList.contains('is-open')) {
        close();
        burger.focus();
      }
    });
    // Coming back from a phone-width layout with the panel open would otherwise
    // leave a floating sidebar over the desktop grid.
    window.addEventListener('resize', function () {
      if (window.innerWidth > 860) { close(); }
    });
  }

  /* =============================================================== search */

  var SEARCH_GLYPH = '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>';

  function setupSearch() {
    var trigger = document.querySelector('.searchbtn');
    if (!trigger) { return; }
    var root = trigger.getAttribute('data-search-root') || '';
    var records = null;
    var loading = null;
    var dialog = null;
    var input = null;
    var list = null;
    var hits = [];
    var active = -1;
    var opener = null;

    // Ctrl-K on Windows and Linux, Cmd-K on a Mac. The label in the button says
    // "Ctrl"; rewrite it on an Apple keyboard rather than showing both.
    var mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    if (mac) {
      var mod = trigger.querySelector('.searchbtn__mod');
      if (mod) { mod.textContent = '⌘'; }
    }

    /* The index is written twice by the build, from one list of records:
     * `search-index.json` is the artifact, and `search-index.js` assigns the
     * same payload to a global. Chromium treats a `fetch()` of a local file as
     * a cross-origin request and blocks it, so over `file://` the JSON is
     * unreachable — the script shim is the fallback that keeps search alive for
     * somebody reading a cloned repository offline. */
    function load() {
      if (records) { return Promise.resolve(records); }
      if (loading) { return loading; }
      if (window.TEST_HUB_DOCS_SEARCH) {
        records = window.TEST_HUB_DOCS_SEARCH.d || [];
        return Promise.resolve(records);
      }
      loading = fetch(root + 'assets/search-index.json')
        .then(function (response) {
          if (!response.ok) { throw new Error('HTTP ' + response.status); }
          return response.json();
        })
        .then(function (payload) {
          records = payload.d || [];
          return records;
        })
        .catch(function () {
          return injectShim().then(function () {
            records = (window.TEST_HUB_DOCS_SEARCH || {}).d || [];
            return records;
          });
        });
      return loading;
    }

    function injectShim() {
      return new Promise(function (resolve) {
        var script = document.createElement('script');
        script.src = root + 'assets/search-index.js';
        script.onload = function () { resolve(); };
        script.onerror = function () { resolve(); };
        document.head.appendChild(script);
      });
    }

    /* Scoring, in one place so it can be argued with.
     *
     * Every term must appear somewhere in the record — an AND, not an OR, so
     * "sse replay" does not return every page that says "sse". Where it appears
     * decides the weight: a heading beats a page title beats a code token beats
     * the body. Exact `--flag` and endpoint matches sit in `k` (keywords), which
     * is why searching for a flag lands on the page that documents it rather
     * than on the six that mention it. */
    function score(record, terms) {
      var title = record.t.toLowerCase();
      var heading = (record.h || '').toLowerCase();
      var keys = (record.k || '').toLowerCase();
      var body = (record.x || '').toLowerCase();
      var total = 0;
      for (var i = 0; i < terms.length; i++) {
        var term = terms[i];
        var points = 0;
        if (heading.indexOf(term) === 0) { points = 60; }
        else if (heading.indexOf(term) >= 0) { points = 40; }
        if (title.indexOf(term) >= 0) { points += 22; }
        if (keys.indexOf(term) >= 0) { points += 30; }
        if (body.indexOf(term) >= 0) { points += 8; }
        if (!points) { return 0; }
        total += points;
      }
      // A page's opening record has no heading; prefer the section that is
      // actually about the term over the page it happens to live on.
      return record.h ? total + 4 : total;
    }

    function search(query) {
      var terms = query.toLowerCase().split(/\s+/).filter(Boolean);
      if (!terms.length) { return []; }
      var scored = [];
      for (var i = 0; i < records.length; i++) {
        var value = score(records[i], terms);
        if (value > 0) { scored.push({ r: records[i], s: value }); }
      }
      scored.sort(function (a, b) { return b.s - a.s; });
      return scored.slice(0, 24);
    }

    function render(query) {
      list.textContent = '';
      hits = [];
      active = -1;
      if (!query.trim()) {
        list.appendChild(el('li', {}, [el('div', {
          className: 'searchdlg__empty',
          text: 'Search titles, headings, flags, endpoints and settings.'
        })]));
        return;
      }
      var results = search(query);
      if (!results.length) {
        list.appendChild(el('li', {}, [el('div', {
          className: 'searchdlg__empty', text: 'No matches for “' + query + '”.'
        })]));
        return;
      }
      results.forEach(function (entry, index) {
        var record = entry.r;
        var href = root + record.p + (record.a ? '#' + record.a : '');
        var link = el('a', {
          className: 'searchdlg__hit', href: href, id: 'search-hit-' + index, role: 'option'
        }, [
          el('span', { className: 'searchdlg__crumb', text: record.t }),
          el('b', { text: record.h || record.t }),
          el('span', { className: 'searchdlg__snip', text: record.x || '' })
        ]);
        link.addEventListener('mouseenter', function () { highlight(index); });
        hits.push(link);
        list.appendChild(el('li', {}, [link]));
      });
      highlight(0);
    }

    function highlight(index) {
      if (!hits.length) { return; }
      if (active >= 0 && hits[active]) { hits[active].classList.remove('is-active'); }
      active = Math.max(0, Math.min(index, hits.length - 1));
      hits[active].classList.add('is-active');
      hits[active].scrollIntoView({ block: 'nearest' });
      input.setAttribute('aria-activedescendant', hits[active].id);
    }

    function build() {
      input = el('input', {
        className: 'searchdlg__input',
        type: 'search',
        placeholder: 'Search the documentation…',
        autocomplete: 'off',
        spellcheck: false,
        'aria-label': 'Search the documentation',
        'aria-controls': 'search-results',
        'aria-autocomplete': 'list'
      });
      list = el('ul', {
        className: 'searchdlg__results', id: 'search-results', role: 'listbox',
        'aria-label': 'Search results'
      });

      var panel = el('div', {
        className: 'searchdlg__panel', role: 'dialog', 'aria-modal': 'true',
        'aria-label': 'Search the documentation'
      }, [
        el('div', { className: 'searchdlg__top' }, [svg(SEARCH_GLYPH), input]),
        list,
        el('div', { className: 'searchdlg__foot' }, [
          el('span', { html: '<kbd>&uarr;</kbd> <kbd>&darr;</kbd> to move' }),
          el('span', { html: '<kbd>Enter</kbd> to open' }),
          el('span', { html: '<kbd>Esc</kbd> to close' })
        ])
      ]);

      dialog = el('div', { className: 'searchdlg', hidden: true }, [panel]);
      dialog.addEventListener('mousedown', function (event) {
        if (event.target === dialog) { close(); }
      });
      input.addEventListener('input', function () { render(input.value); });
      input.addEventListener('keydown', function (event) {
        if (event.key === 'ArrowDown') { event.preventDefault(); highlight(active + 1); }
        else if (event.key === 'ArrowUp') { event.preventDefault(); highlight(active - 1); }
        else if (event.key === 'Home' && hits.length) { event.preventDefault(); highlight(0); }
        else if (event.key === 'End' && hits.length) { event.preventDefault(); highlight(hits.length - 1); }
        else if (event.key === 'Enter' && hits[active]) { event.preventDefault(); hits[active].click(); }
        else if (event.key === 'Escape') { event.preventDefault(); close(); }
      });
      document.body.appendChild(dialog);
    }

    function open() {
      if (!dialog) { build(); }
      opener = document.activeElement;
      dialog.hidden = false;
      input.value = '';
      render('');
      input.focus();
      load().then(function () { render(input.value); });
    }

    function close() {
      if (!dialog || dialog.hidden) { return; }
      dialog.hidden = true;
      if (opener && opener.focus) { opener.focus(); }
    }

    trigger.addEventListener('click', open);

    document.addEventListener('keydown', function (event) {
      var isK = event.key === 'k' || event.key === 'K';
      if (isK && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        if (dialog && !dialog.hidden) { close(); } else { open(); }
        return;
      }
      if (event.key === '/' && !dialog) {
        // Only when the reader is not already typing somewhere.
        var tag = (document.activeElement || {}).tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') { return; }
        event.preventDefault();
        open();
      }
    });
  }

  /* ================================================================== toc */

  function setupToc() {
    var links = document.querySelectorAll('.toc a');
    if (!links.length || !window.IntersectionObserver) { return; }

    var byId = {};
    var targets = [];
    Array.prototype.forEach.call(links, function (link) {
      var id = decodeURIComponent((link.getAttribute('href') || '').slice(1));
      var heading = id && document.getElementById(id);
      if (!heading) { return; }
      byId[id] = link;
      targets.push(heading);
    });

    var visible = {};
    var observer = new window.IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        visible[entry.target.id] = entry.isIntersecting;
      });
      var current = null;
      for (var i = 0; i < targets.length; i++) {
        if (visible[targets[i].id]) { current = targets[i].id; break; }
      }
      // Nothing intersecting means the reader is in the middle of a long
      // section: keep the last heading they passed rather than clearing it.
      if (!current) { return; }
      Array.prototype.forEach.call(links, function (link) { link.classList.remove('is-current'); });
      if (byId[current]) { byId[current].classList.add('is-current'); }
    }, { rootMargin: '-72px 0px -70% 0px', threshold: 0 });

    targets.forEach(function (heading) { observer.observe(heading); });
  }

  /* ============================================================== anchors */

  function setupAnchors() {
    var headings = document.querySelectorAll('.docmain h2[id], .docmain h3[id]');
    Array.prototype.forEach.call(headings, function (heading) {
      var link = el('a', {
        className: 'anchor',
        href: '#' + heading.id,
        text: '#',
        'aria-label': 'Link to this section'
      });
      heading.appendChild(link);
    });
  }

  /* =================================================================== go */

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  ready(function () {
    setupTheme();
    setupCopyButtons();
    setupNav();
    setupSearch();
    setupToc();
    setupAnchors();
  });
}(window, document));
