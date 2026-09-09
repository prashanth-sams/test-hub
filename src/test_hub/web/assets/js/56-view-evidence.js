/* Test Hub — the evidence pages: screenshots, API logs, coverage, steps.
 *
 * Four routes, one file, because they are four questions about the same thing:
 * what did the run leave behind? They read the same dataset the analytics page
 * reads (the newest finished runs, in full — see `DETAIL_ROUTES` in the store)
 * and each one picks its own evidence out of it.
 *
 * What actually exists in the model, and therefore what each page can honestly
 * show:
 *
 *   Screenshots  ArtifactKind.SCREENSHOT and .VIDEO on a run. Real data. The
 *                images are served by the API at /api/v1/artifacts/{id}; the
 *                CSP already allows `img-src 'self'`, so they render inline.
 *   API Logs     ArtifactKind.LOG / .TRACE files, plus the stdout, stderr and
 *                logs captured on each attempt. Test Hub has NO request/response
 *                model — nothing records an HTTP exchange as a first-class
 *                object — so this page shows the log evidence that does exist
 *                and says plainly what it cannot show.
 *   Coverage     ArtifactKind.COVERAGE files. Test Hub stores the report; it
 *                does not parse a percentage out of it, so there is no coverage
 *                figure to print and this page does not invent one.
 *   Steps        Nothing in the schema. There is no step model: the finest
 *                grain Test Hub records is the attempt. The page reads
 *                `attempt.metadata.steps` — the one place a publisher can put
 *                them today — and when that is empty it says so, rather than
 *                dressing attempts up as steps.
 *
 * A page with nothing in it is still a page here. "No screenshots in the last
 * five runs" is a fact about the suite, and it is the fact somebody clicked to
 * find out. Every empty state below names what would fill it.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

  TestHub.views = TestHub.views || {};

  var FIELDS = ui.portfolioFields();

  //: Characters of one captured stream shown inline. The whole thing is on the
  //: test's own page; this is enough to recognise it by.
  var OUTPUT_LIMIT = 4000;
  //: Screenshots drawn before the gallery says "and N more". Each one is an
  //: image request, and forty of them is already a slow page on a phone.
  var SHOT_LIMIT = 60;

  /* -------------------------------------------------------------- shared */

  function isArray(value) {
    return Object.prototype.toString.call(value) === '[object Array]';
  }

  /**
   * Pull every piece of evidence out of the loaded runs, once.
   *
   * Artifacts carry `test_id`, so each file is matched back to the test that
   * produced it — a screenshot with no test beside it is a file, and a
   * screenshot captioned with the test that failed is evidence.
   */
  function collect(details) {
    var found = { files: [], outputs: [], steps: [], attempts: 0, results: 0 };
    var i, j, k;

    for (i = 0; i < details.length; i++) {
      var detail = details[i];
      var tests = detail.tests || [];
      var byId = {};
      for (j = 0; j < tests.length; j++) { byId[String(tests[j].id)] = tests[j]; }

      var artifacts = detail.artifacts || [];
      for (j = 0; j < artifacts.length; j++) {
        var artifact = artifacts[j];
        found.files.push({
          artifact: artifact,
          test: byId[String(artifact.test_id)] || null,
          detail: detail
        });
      }

      for (j = 0; j < tests.length; j++) {
        var test = tests[j];
        found.results += 1;
        var attempts = test.attempts || [];
        for (k = 0; k < attempts.length; k++) {
          var attempt = attempts[k];
          found.attempts += 1;
          if (attempt.logs || attempt.stdout || attempt.stderr) {
            found.outputs.push({ test: test, attempt: attempt, detail: detail });
          }
          var steps = attempt.metadata && attempt.metadata.steps;
          if (isArray(steps) && steps.length) {
            found.steps.push({
              test: test, attempt: attempt, detail: detail, steps: steps
            });
          }
        }
      }
    }
    return found;
  }

  function ofKind(files, kinds) {
    var wanted = {};
    for (var i = 0; i < kinds.length; i++) { wanted[kinds[i]] = true; }
    return files.filter(function (entry) { return wanted[entry.artifact.kind]; });
  }

  function testName(test) {
    return test ? (test.full_name || test.name) : null;
  }

  function runLabel(detail) {
    return detail.run.name || util.shortId(detail.run.id);
  }

  function testLink(entry) {
    var name = testName(entry.test);
    if (!name) { return el('span', { className: 'subtle', text: 'Whole run' }); }
    return ui.link(util.buildHash(['tests', entry.test.id], {}), name, 'cell-main');
  }

  function runLink(detail) {
    return ui.link(util.buildHash(['runs', detail.run.id], {}), runLabel(detail));
  }

  /** A download link, or the plain name when the provider gave us no URL. */
  function fileLink(artifact) {
    if (!artifact.url) {
      return el('span', { className: 'cell-main', text: artifact.name });
    }
    return el('a', {
      className: 'cell-main',
      attrs: { href: artifact.url, rel: 'noreferrer noopener' },
      text: artifact.name
    });
  }

  /**
   * The scaffold every page in this file shares: title, portfolio filters, and
   * the four states (error, loading, no finished runs, drawn) that each of them
   * would otherwise re-implement slightly differently.
   *
   * `spec.render(found, details, repaint, local)` is called only in the fourth.
   */
  function page(spec) {
    return function (ctx) {
      var store = ctx.store;
      var lastState = null;
      // Whatever view-local state this page's controls need, scoped to this
      // mount: leaving the page and coming back gives it back its defaults.
      var local = {};

      var panel = ui.filterPanel({
        label: 'Portfolio filters',
        fields: FIELDS,
        values: store.state.filters,
        onChange: function (key, value) {
          var patch = {};
          patch[key] = value;
          util.navigate(store.hashWith(patch));
        },
        onReset: function () {
          util.navigate(util.buildHash([spec.route], {}));
        }
      });

      var subtitle = el('p', { className: 'page-head__sub', text: '' });
      var content = el('div', { className: 'stack' });
      var root = el('div', {}, [
        el('div', { className: 'page-head' }, [
          el('div', { className: 'page-head__titles' }, [
            el('h1', { text: spec.title }),
            subtitle
          ])
        ]),
        panel.el,
        el('div', { className: 'section' }, content)
      ]);

      function repaint() {
        if (lastState) { render(lastState); }
      }

      function render(state) {
        lastState = state;

        if (state.errors.analytics) {
          util.replace(content, ui.card({
            title: spec.title,
            body: ui.errorState({
              title: 'The runs did not load',
              error: state.errors.analytics,
              onRetry: function () { store.retry(); }
            })
          }));
          return;
        }

        if (!state.analytics || !state.overview) {
          subtitle.textContent = 'Reading the most recent finished runs…';
          util.replace(content, ui.card({
            title: spec.title,
            body: ui.loadingBlock('Loading ' + spec.title.toLowerCase(), 'row', 4)
          }));
          return;
        }

        var details = state.analytics.runs || [];
        if (!(state.analytics.summaries || []).length) {
          subtitle.textContent = 'Across the last ' + state.overview.window_days + ' days.';
          util.replace(content, ui.card({
            title: spec.title,
            icon: spec.icon,
            body: ui.emptyState({
              icon: spec.icon,
              title: 'No finished run in this window',
              hint: 'This page reads the runs that finished. Widen the window, clear ' +
                'a filter, or wait for the run in flight to end.'
            })
          }));
          return;
        }

        var found = collect(details);
        // An offline export carries the runs but not the files they attached —
        // `include_artifacts` is off by default in the exporter, and an artifact
        // URL points at a server the export does not have. The empty states
        // below have to tell those two cases apart: "this suite captured
        // nothing" and "this file does not carry what it captured" are
        // different facts, and only one of them is about the tests.
        found.exported = state.providerKind === 'snapshot' &&
          !(state.capabilities && state.capabilities.artifacts);
        subtitle.textContent = spec.describe(found, details);
        util.replace(content, el('div', { className: 'stack' },
          spec.render(found, details, repaint, local)));
      }

      return {
        el: root,
        update: function (state, section) {
          panel.sync(state.filters, state.facets || {});
          if (section === 'facets' || section === 'status') { return; }
          render(state);
        },
        destroy: function () {}
      };
    };
  }

  /* --------------------------------------------------------- screenshots */

  /**
   * One screenshot, with the test it came from underneath it.
   *
   * The image is loaded from the API like any other artifact. Two things can go
   * wrong and both end in the same place: an offline export has no server to
   * serve it, and a file that is not really an image will not decode. The error
   * handler swaps the frame for the file itself, so a broken picture is never
   * what the reader is left with.
   */
  function shot(entry) {
    var artifact = entry.artifact;
    var isVideo = artifact.kind === 'video';
    var name = testName(entry.test);
    var frame = el('div', { className: 'shot__frame' });

    function fallback(label) {
      util.replace(frame, el('div', { className: 'shot__fallback' }, [
        ui.icon(isVideo ? 'monitor' : 'camera', 20),
        el('span', { text: label })
      ]));
    }

    if (isVideo) {
      // Video is deliberately not embedded: `media-src` is not in the CSP, and
      // an offline export could not play it anyway. The link still works.
      fallback('Video — open to play');
    } else if (artifact.url) {
      frame.appendChild(el('img', {
        className: 'shot__img',
        attrs: {
          src: artifact.url,
          alt: name ? 'Screenshot from ' + name : 'Screenshot from ' + runLabel(entry.detail),
          loading: 'lazy',
          decoding: 'async'
        },
        on: { error: function () { fallback('Image unavailable'); } }
      }));
    } else {
      fallback('Not stored with this export');
    }

    var caption = [
      el('div', { className: 'shot__title' }, entry.test
        ? ui.link(util.buildHash(['tests', entry.test.id], {}), name)
        : el('span', { text: artifact.name })),
      el('div', { className: 'shot__meta row row--tight' }, [
        entry.test ? ui.statusPill(entry.test.status) : null,
        runLink(entry.detail),
        el('span', { className: 'subtle', text: util.formatBytes(artifact.size_bytes) })
      ])
    ];

    return el('figure', { className: 'shot' }, [
      artifact.url
        ? el('a', {
          className: 'shot__open',
          attrs: {
            href: artifact.url,
            rel: 'noreferrer noopener',
            'aria-label': 'Open ' + artifact.name
          }
        }, frame)
        : frame,
      el('figcaption', {}, caption)
    ]);
  }

  var screenshots = page({
    route: 'screenshots',
    title: 'Screenshots',
    icon: 'camera',
    describe: function (found, details) {
      var shots = ofKind(found.files, ['screenshot', 'video']);
      if (!shots.length) {
        return found.exported
          ? 'This export carries the runs, not the files they attached.'
          : 'No screenshots in the last ' + util.formatNumber(details.length) +
            ' finished ' + util.pluralize(details.length, 'run') + '.';
      }
      return util.formatNumber(shots.length) + ' ' + util.pluralize(shots.length, 'capture') +
        ' from the last ' + util.formatNumber(details.length) + ' finished ' +
        util.pluralize(details.length, 'run') + '.';
    },
    render: function (found, details, repaint, local) {
      var all = ofKind(found.files, ['screenshot', 'video']);
      if (!local.filter) { local.filter = 'all'; }

      if (!all.length) {
        return [ui.card({
          title: 'Screenshots',
          icon: 'camera',
          body: ui.emptyState({
            icon: 'camera',
            title: found.exported
              ? 'This export does not carry its attachments'
              : 'Nothing was captured in these runs',
            hint: found.exported
              ? 'The runs are in this file; the files they attached are not. ' +
                'Screenshots stay on the Test Hub server that wrote the export, ' +
                'which is where this page can show them.'
              : 'Screenshots appear here when a run attaches a file with kind ' +
                '"screenshot" or "video". Publish them with the run and they are ' +
                'matched to the test that produced them.'
          })
        })];
      }

      // Failures first, always: a gallery of two hundred passing screenshots
      // with the one that matters somewhere in the middle is not a gallery.
      var failing = all.filter(function (entry) {
        return entry.test && util.isFailureish(entry.test.status);
      });
      var chosen = local.filter === 'failing' ? failing : all;
      var shown = chosen.slice(0, SHOT_LIMIT);

      var lens = ui.segmented({
        label: 'Which captures',
        value: local.filter,
        options: [
          { value: 'all', label: 'All ' + util.formatNumber(all.length) },
          { value: 'failing', label: 'Failures ' + util.formatNumber(failing.length) }
        ],
        onChange: function (value) { local.filter = value; repaint(); }
      });

      var body = shown.length
        ? el('div', { className: 'gallery' }, shown.map(shot))
        : ui.emptyState({
          icon: 'check',
          title: 'Nothing failed with a screenshot attached',
          hint: 'Every capture in these runs came from a test that passed.'
        });

      return [ui.card({
        title: 'Captures',
        icon: 'camera',
        flush: true,
        hint: chosen.length > shown.length
          ? 'Showing the first ' + util.formatNumber(shown.length) + ' of ' +
            util.formatNumber(chosen.length)
          : null,
        body: [
          el('div', { className: 'card__toolbar' }, [
            el('div', { className: 'filters__row' }, [lens.el])
          ]),
          el('div', { className: 'card__body' }, body)
        ]
      })];
    }
  });

  /* ------------------------------------------------------------ API logs */

  function outputBlock(label, value) {
    var text = String(value);
    var clipped = text.length > OUTPUT_LIMIT;
    return el('div', {}, [
      el('div', { className: 'metagrid__label', text: label }),
      el('pre', {
        className: 'pre pre--wrap',
        text: clipped ? text.slice(0, OUTPUT_LIMIT) : text
      }),
      clipped
        ? el('p', {
          className: 'subtle',
          text: 'Truncated here at ' + util.formatNumber(OUTPUT_LIMIT) +
            ' characters — the test page has the whole stream.'
        })
        : null
    ]);
  }

  function outputEntry(entry) {
    var streams = [];
    if (entry.attempt.logs) { streams.push('logs'); }
    if (entry.attempt.stdout) { streams.push('stdout'); }
    if (entry.attempt.stderr) { streams.push('stderr'); }

    var body = el('div', { className: 'stack--sm' }, [
      entry.attempt.logs ? outputBlock('Logs', entry.attempt.logs) : null,
      entry.attempt.stdout ? outputBlock('stdout', entry.attempt.stdout) : null,
      entry.attempt.stderr ? outputBlock('stderr', entry.attempt.stderr) : null
    ]);

    // A native disclosure: closed, the page is a list of tests that recorded
    // something; open, it is the recording. No JavaScript state to keep.
    return el('details', { className: 'disclosure' }, [
      el('summary', { className: 'disclosure__summary' }, [
        ui.statusPill(entry.attempt.status),
        el('span', { className: 'cell-main truncate', text: testName(entry.test) }),
        el('span', { className: 'subtle', text: streams.join(', ') })
      ]),
      el('div', { className: 'disclosure__body' }, [
        el('p', { className: 'subtle row row--tight' }, [
          document.createTextNode('Attempt ' + (entry.attempt.attempt_number || 1) + ' · '),
          runLink(entry.detail)
        ]),
        body
      ])
    ]);
  }

  function filesTable(entries, caption) {
    return ui.table({
      caption: caption,
      columns: [
        {
          key: 'name', label: 'File',
          render: function (entry) { return fileLink(entry.artifact); }
        },
        {
          key: 'kind', label: 'Kind',
          render: function (entry) { return ui.tag(entry.artifact.kind); }
        },
        {
          key: 'test', label: 'Test',
          render: function (entry) { return testLink(entry); }
        },
        {
          key: 'run', label: 'Run',
          render: function (entry) { return runLink(entry.detail); }
        },
        {
          key: 'size', label: 'Size', align: 'right',
          render: function (entry) { return util.formatBytes(entry.artifact.size_bytes); }
        },
        {
          key: 'created', label: 'Captured', align: 'right',
          render: function (entry) {
            return entry.artifact.created_at
              ? el('span', {
                text: util.timeAgo(entry.artifact.created_at),
                attrs: { title: util.formatDateTime(entry.artifact.created_at) }
              })
              : null;
          }
        }
      ],
      rows: entries
    });
  }

  var logs = page({
    route: 'logs',
    title: 'API Logs',
    icon: 'swap',
    describe: function (found, details) {
      var files = ofKind(found.files, ['log', 'trace']).length;
      var captured = found.outputs.length;
      if (!files && !captured) {
        return 'Nothing was logged in the last ' + util.formatNumber(details.length) +
          ' finished ' + util.pluralize(details.length, 'run') + '.';
      }
      var parts = [];
      if (files) {
        parts.push(util.formatNumber(files) + ' log ' + util.pluralize(files, 'file'));
      }
      if (captured) {
        parts.push(util.formatNumber(captured) + ' ' +
          util.pluralize(captured, 'attempt') + ' with captured output');
      }
      return parts.join(' and ') + ', from the last ' +
        util.formatNumber(details.length) + ' finished ' +
        util.pluralize(details.length, 'run') + '.';
    },
    render: function (found) {
      var files = ofKind(found.files, ['log', 'trace']);
      var outputs = found.outputs;
      var nodes = [];

      // Said once, at the top, and not repeated in every empty state below:
      // this page is named for a thing Test Hub does not model.
      nodes.push(ui.banner({
        tone: 'info',
        icon: 'alert',
        title: 'Test Hub does not record HTTP exchanges',
        text: 'There is no request/response object in the schema, so nothing here ' +
          'can be filtered by status code or endpoint. What a run can publish is ' +
          'a log file (artifact kind "log" or "trace") and the stdout, stderr and ' +
          'log stream captured on each attempt — both are below.'
      }));

      if (!files.length && !outputs.length) {
        nodes.push(ui.card({
          title: 'Logs',
          icon: 'swap',
          body: ui.emptyState({
            icon: 'swap',
            title: found.exported
              ? 'Nothing was captured, and no file came with this export'
              : 'No log files and no captured output',
            hint: found.exported
              ? 'Captured output travels inside an export; attached log files do ' +
                'not. Neither is in these runs.'
              : 'Attach a file with kind "log", or publish stdout, stderr and logs ' +
                'on the attempt, and they appear here against the test that wrote them.'
          })
        }));
        return nodes;
      }

      if (!files.length && found.exported) {
        nodes.push(ui.banner({
          tone: 'warn',
          title: 'Log files are not in this export',
          text: 'The captured output below travels with the runs. Files attached ' +
            'to a run stay on the server that wrote this export.'
        }));
      }

      if (files.length) {
        nodes.push(ui.card({
          title: 'Log files',
          icon: 'swap',
          flush: true,
          hint: util.formatNumber(files.length) + ' ' + util.pluralize(files.length, 'file'),
          body: filesTable(files, 'Log and trace files attached to the recent runs')
        }));
      }

      if (outputs.length) {
        // Failures first: the attempt somebody came here to read.
        var ordered = outputs.slice().sort(function (a, b) {
          var left = util.isFailureish(a.attempt.status) ? 0 : 1;
          var right = util.isFailureish(b.attempt.status) ? 0 : 1;
          return left - right;
        });
        nodes.push(ui.card({
          title: 'Captured output',
          icon: 'list',
          hint: util.formatNumber(ordered.length) + ' ' +
            util.pluralize(ordered.length, 'attempt') + ' recorded something',
          body: el('div', { className: 'stack--sm' }, ordered.map(outputEntry))
        }));
      }

      return nodes;
    }
  });

  /* ------------------------------------------------------------ coverage */

  var coverage = page({
    route: 'coverage',
    title: 'Test Coverage',
    icon: 'shield',
    describe: function (found, details) {
      var files = ofKind(found.files, ['coverage']).length;
      if (!files) {
        return found.exported
          ? 'This export carries the runs, not the files they attached.'
          : 'No coverage report in the last ' + util.formatNumber(details.length) +
            ' finished ' + util.pluralize(details.length, 'run') + '.';
      }
      return util.formatNumber(files) + ' coverage ' + util.pluralize(files, 'report') +
        ' from the last ' + util.formatNumber(details.length) + ' finished ' +
        util.pluralize(details.length, 'run') + '.';
    },
    render: function (found) {
      var files = ofKind(found.files, ['coverage']);

      if (!files.length) {
        return [ui.card({
          title: 'Test Coverage',
          icon: 'shield',
          body: ui.emptyState({
            icon: 'shield',
            title: found.exported
              ? 'This export does not carry its attachments'
              : 'No coverage report was attached',
            hint: found.exported
              ? 'The runs are in this file; the reports they attached stayed on the ' +
                'Test Hub server that wrote it.'
              : 'Publish the report your tooling already produces — coverage.xml, ' +
                'lcov.info, an HTML bundle — as an artifact with kind "coverage", and ' +
                'every run keeps its own copy here.'
          })
        })];
      }

      return [
        ui.banner({
          tone: 'info',
          icon: 'alert',
          title: 'Coverage is stored, not measured',
          text: 'Test Hub keeps the report each run produced and hands it back ' +
            'unchanged. It does not parse a percentage out of it, so there is no ' +
            'coverage figure on this page and no trend line — the numbers below are ' +
            'about the files, not about the code they describe.'
        }),
        ui.card({
          title: 'Reports',
          icon: 'shield',
          flush: true,
          hint: util.formatNumber(files.length) + ' ' + util.pluralize(files.length, 'file'),
          body: filesTable(files, 'Coverage reports attached to the recent runs')
        })
      ];
    }
  });

  /* --------------------------------------------------------------- steps */

  /** Accept the shapes a publisher might plausibly use, and say so once here. */
  function normalizeStep(raw, index) {
    if (typeof raw === 'string') {
      return { name: raw, status: null, duration_ms: null, index: index };
    }
    var step = raw || {};
    return {
      name: step.name || step.title || step.keyword || step.text || ('Step ' + (index + 1)),
      status: step.status || step.outcome || null,
      duration_ms: typeof step.duration_ms === 'number' ? step.duration_ms
        : (typeof step.duration === 'number' ? step.duration : null),
      index: index
    };
  }

  function stepsCard(entry) {
    var steps = entry.steps.map(normalizeStep);
    var attempt = entry.attempt.attempt_number || 1;
    // A retried test publishes steps twice, and two identical headings with
    // different outcomes under them is the most confusing thing this page could
    // show. Both cards are labelled with their attempt, not just the second:
    // "4 steps" followed by "Attempt 2 · 4 steps" leaves the reader working out
    // what the first one was.
    var retried = (entry.test.attempts || []).length > 1;
    return ui.card({
      title: testName(entry.test),
      icon: 'steps',
      hint: (retried ? 'Attempt ' + attempt + ' · ' : '') +
        util.formatNumber(steps.length) + ' ' + util.pluralize(steps.length, 'step'),
      actions: [runLink(entry.detail)],
      body: el('ol', { className: 'steps' }, steps.map(function (step) {
        return el('li', { className: 'steps__item' }, [
          el('span', { className: 'steps__name', text: step.name }),
          step.status ? ui.statusPill(step.status) : null,
          step.duration_ms === null
            ? null
            : el('span', { className: 'subtle num', text: util.formatDuration(step.duration_ms) })
        ]);
      }))
    });
  }

  var steps = page({
    route: 'steps',
    title: 'Test Steps',
    icon: 'steps',
    describe: function (found, details) {
      if (!found.steps.length) {
        return 'Nothing in the last ' + util.formatNumber(details.length) + ' finished ' +
          util.pluralize(details.length, 'run') + ' recorded steps — ' +
          util.formatNumber(found.attempts) + ' ' +
          util.pluralize(found.attempts, 'attempt') + ' checked.';
      }
      return util.formatNumber(found.steps.length) + ' ' +
        util.pluralize(found.steps.length, 'attempt') + ' recorded steps, from the last ' +
        util.formatNumber(details.length) + ' finished ' +
        util.pluralize(details.length, 'run') + '.';
    },
    render: function (found) {
      if (!found.steps.length) {
        return [ui.card({
          title: 'Test Steps',
          icon: 'steps',
          body: ui.emptyState({
            icon: 'steps',
            title: 'No steps have been published',
            hint: 'Test Hub has no step model: the finest grain it records is the ' +
              'attempt, with its status, timing, output and failure. Until there is ' +
              'one, a publisher can put a list under attempt metadata as "steps" — ' +
              'each entry a name, and optionally a status and a duration — and this ' +
              'page renders it. ' + util.formatNumber(found.attempts) + ' ' +
              util.pluralize(found.attempts, 'attempt') + ' in these runs carried none.'
          })
        })];
      }

      var nodes = [ui.banner({
        tone: 'info',
        icon: 'alert',
        title: 'Steps are publisher metadata, not a model',
        text: 'These came from "steps" in each attempt’s metadata. Nothing ' +
          'validates them, they are not searchable and they do not roll up into any ' +
          'count on another page — they are shown exactly as they were published.'
      })];
      for (var i = 0; i < found.steps.length; i++) {
        nodes.push(stepsCard(found.steps[i]));
      }
      return nodes;
    }
  });

  TestHub.views.screenshots = { create: screenshots };
  TestHub.views.logs = { create: logs };
  TestHub.views.coverage = { create: coverage };
  TestHub.views.steps = { create: steps };
})(window, document);
