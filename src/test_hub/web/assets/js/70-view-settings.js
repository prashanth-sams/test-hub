/* Test Hub — the settings view, and the only destructive control in the UI.
 *
 * Retention lives on a page rather than inside the header menu on purpose. A
 * dropdown is the wrong shape for something irreversible: it is small, it closes
 * when focus moves, and it puts a "delete 4,000 rows" button one stray click
 * from a theme switch. A page has room for the thing that actually makes this
 * safe — a preview of exactly what would go, read from the server, before
 * anything is deleted.
 *
 * The flow is deliberately two-step and cannot be collapsed into one:
 *
 *   1. Choose a cutoff and press Preview. Nothing is written; the server counts.
 *   2. The counts appear, and only then does the delete button exist at all.
 *
 * Changing the cutoff throws the preview away, so the button a reader presses
 * is always describing the numbers they are looking at.
 *
 * When the server says `admin: false` this view renders an explanation instead
 * of a form. That is not politeness — the endpoints are behind a separate token
 * (ADR-0002 D8), and a form that 403s on submit is worse than no form.
 */
(function (window, document) {
  'use strict';

  var TestHub = window.TestHub = window.TestHub || {};
  var util = TestHub.util;
  var ui = TestHub.ui;
  var el = util.el;

  /* Hours per unit, mirroring `test_hub.service.retention.UNIT_HOURS`. The
   * server sends its own table in GET /retention and that is what is used; this
   * is only the fallback for a server too old to send one. */
  var UNITS = [
    { value: 'h', label: 'hours' },
    { value: 'd', label: 'days' },
    { value: 'w', label: 'weeks' },
    { value: 'm', label: 'months' },
    { value: 'y', label: 'years' }
  ];

  function create(ctx) {
    var provider = ctx.provider;
    var root = el('div', {});
    var body = el('div', { className: 'stack' });
    var settings = null;
    var preview = null;
    var busy = false;

    var form = {
      amount: 90,
      unit: 'd',
      keepMin: null,
      includeUnfinished: false,
      byImportDate: false
    };

    root.appendChild(el('div', { className: 'page-head' }, [
      el('div', { className: 'page-head__titles' }, [
        el('h1', { text: 'Settings' }),
        el('p', {
          className: 'page-head__sub',
          text: 'Retention and housekeeping for this workspace.'
        })
      ]),
      el('div', { className: 'page-head__actions' }, [
        ui.link('#/overview', 'Back to overview', 'btn btn--sm')
      ])
    ]));
    root.appendChild(body);

    /* ------------------------------------------------------------ render */

    function setBusy(next) {
      busy = next;
      render();
    }

    function invalidatePreview() {
      preview = null;
      render();
    }

    function cutoffLabel() {
      var unit = null;
      for (var i = 0; i < UNITS.length; i++) {
        if (UNITS[i].value === form.unit) { unit = UNITS[i]; }
      }
      var label = unit ? unit.label : 'days';
      return form.amount + ' ' + (form.amount === 1 ? label.replace(/s$/, '') : label);
    }

    function requestBody(extra) {
      var body = {
        older_than: String(form.amount) + form.unit,
        include_unfinished: form.includeUnfinished,
        by_import_date: form.byImportDate
      };
      if (form.keepMin !== null && form.keepMin !== '') {
        body.keep_min = Number(form.keepMin);
      }
      if (extra) {
        for (var key in extra) {
          if (Object.prototype.hasOwnProperty.call(extra, key)) { body[key] = extra[key]; }
        }
      }
      return body;
    }

    function numberField(id, label, value, onChange, hint) {
      var input = el('input', {
        className: 'input',
        attrs: { id: id, type: 'number', min: '0', step: '1', value: String(value), inputmode: 'numeric' },
        on: {
          input: function (event) { onChange(event.target.value); }
        }
      });
      return el('div', { className: 'field' }, [
        el('label', { className: 'field__label', attrs: { for: id }, text: label }),
        input,
        hint ? el('div', { className: 'field__hint', text: hint }) : null
      ]);
    }

    function checkboxField(id, label, checked, onChange, hint) {
      var input = el('input', {
        attrs: { id: id, type: 'checkbox' },
        on: { change: function (event) { onChange(event.target.checked); } }
      });
      input.checked = !!checked;
      return el('div', { className: 'field field--check' }, [
        el('label', { className: 'check', attrs: { for: id } }, [
          input,
          el('span', { text: label })
        ]),
        hint ? el('div', { className: 'field__hint', text: hint }) : null
      ]);
    }

    function unitSelect() {
      var select = el('select', {
        className: 'select',
        attrs: { id: 'th-retention-unit', 'aria-label': 'Unit' },
        on: {
          change: function (event) {
            form.unit = event.target.value;
            invalidatePreview();
          }
        }
      });
      for (var i = 0; i < UNITS.length; i++) {
        var option = el('option', { attrs: { value: UNITS[i].value }, text: UNITS[i].label });
        if (UNITS[i].value === form.unit) { option.selected = true; }
        select.appendChild(option);
      }
      return el('div', { className: 'field' }, [
        el('label', { className: 'field__label', attrs: { for: 'th-retention-unit' }, text: 'Unit' }),
        select
      ]);
    }

    function policyCard() {
      var rows = [
        ['Configured default', settings.retention_days
          ? settings.retention_days + ' days'
          : 'none — this server deletes nothing on its own'],
        ['Runs kept per project', String(settings.min_runs)],
        ['New artifacts protected for', settings.grace_hours + ' hours']
      ];
      return ui.card({
        title: 'Policy',
        hint: 'Set on the server, not from here',
        body: [
          ui.kv(rows),
          el('p', { className: 'muted', text:
            'Nothing is deleted automatically. Retention runs when somebody runs it — ' +
            'here, or with `test-hub prune` on the host.' })
        ]
      });
    }

    function formCard() {
      return ui.card({
        title: 'Clear old data',
        hint: 'Runs, their tests and attempts, and any artifacts nothing else uses',
        body: [
          el('div', { className: 'formgrid' }, [
            numberField('th-retention-amount', 'Delete runs older than', form.amount, function (value) {
              form.amount = value === '' ? '' : Number(value);
              invalidatePreview();
            }),
            unitSelect(),
            numberField(
              'th-retention-keep', 'Always keep per project',
              form.keepMin === null ? settings.min_runs : form.keepMin,
              function (value) { form.keepMin = value; invalidatePreview(); },
              'Newest runs kept whatever their age'
            )
          ]),
          checkboxField('th-retention-unfinished', 'Include queued and running runs',
            form.includeUnfinished,
            function (value) { form.includeUnfinished = value; invalidatePreview(); },
            'Off by default: one of these may be a suite that is still going'),
          checkboxField('th-retention-imported', 'Use the date it was imported',
            form.byImportDate,
            function (value) { form.byImportDate = value; invalidatePreview(); },
            'Instead of the date the run happened'),
          el('div', { className: 'formactions' }, [
            el('button', {
              className: 'btn btn--primary',
              attrs: { type: 'button', disabled: busy || form.amount === '' || Number(form.amount) <= 0 },
              text: busy ? 'Checking…' : 'Preview',
              on: { click: doPreview }
            }),
            el('span', { className: 'muted', text: 'Nothing is deleted until you confirm.' })
          ])
        ]
      });
    }

    function previewCard() {
      if (preview === null) { return null; }
      if (!preview.runs) {
        return ui.card({
          title: 'Preview',
          body: ui.emptyState({
            title: 'Nothing to delete',
            hint: 'No run is older than ' + cutoffLabel() + ', once the per-project floor is applied.'
          })
        });
      }

      var deleted = preview.deleted || {};
      var rows = [
        ['Runs', util.formatNumber(preview.runs)],
        ['Suites', util.formatNumber(deleted.suites || 0)],
        ['Tests', util.formatNumber(deleted.test_cases || 0)],
        ['Attempts', util.formatNumber(deleted.attempts || 0)],
        ['Events', util.formatNumber(deleted.events || 0)],
        ['Artifact rows', util.formatNumber(deleted.artifacts || 0)]
      ];
      if (preview.blobs_unlinked) {
        rows.push(['Artifact files', util.formatNumber(preview.blobs_unlinked) +
          ' (' + util.formatBytes(preview.bytes_freed) + ')']);
      }

      var notes = [];
      if (preview.oldest && preview.newest) {
        notes.push('Spanning ' + util.formatDate(preview.oldest) + ' to ' +
          util.formatDate(preview.newest) + '.');
      }
      if (preview.protected) {
        notes.push(preview.protected + ' run(s) are held back by the per-project floor.');
      }
      if (preview.skipped_live) {
        notes.push(preview.skipped_live + ' run(s) past the cutoff are still queued or ' +
          'running and will be left alone.');
      }
      if (preview.blobs_retained) {
        notes.push(preview.blobs_retained + ' artifact file(s) stay: another run still ' +
          'uses them, or they are too new to collect.');
      }

      var projects = [];
      var byProject = preview.by_project || {};
      for (var key in byProject) {
        if (Object.prototype.hasOwnProperty.call(byProject, key)) {
          projects.push({ project: key, runs: byProject[key] });
        }
      }
      projects.sort(function (a, b) { return b.runs - a.runs; });

      return ui.card({
        title: 'This would be deleted',
        hint: 'Runs older than ' + cutoffLabel(),
        body: [
          ui.banner({
            tone: 'warn',
            icon: 'alert',
            title: util.formatNumber(preview.runs) + ' run(s), ' +
              util.formatNumber(preview.rows) + ' rows in total',
            text: 'This cannot be undone. Export the window first if you want a copy.'
          }),
          ui.kv(rows),
          projects.length ? ui.table({
            columns: [
              { key: 'project', label: 'Project' },
              { key: 'runs', label: 'Runs', align: 'right' }
            ],
            rows: projects
          }) : null,
          notes.length ? el('ul', { className: 'notes' }, notes.map(function (note) {
            return el('li', { text: note });
          })) : null,
          el('div', { className: 'formactions' }, [
            el('button', {
              className: 'btn btn--danger',
              attrs: { type: 'button', disabled: busy },
              text: busy ? 'Deleting…' : 'Delete ' + util.formatNumber(preview.runs) + ' run(s)',
              on: { click: doPrune }
            }),
            el('button', {
              className: 'btn btn--sm',
              attrs: { type: 'button', disabled: busy },
              text: 'Cancel',
              on: { click: invalidatePreview }
            })
          ])
        ]
      });
    }

    function resultCard(result) {
      var note = 'The database file has not shrunk — SQLite reuses the freed pages. ' +
        'Run `test-hub prune --vacuum` on the host to return them to the filesystem.';
      return ui.card({
        title: 'Done',
        body: [
          ui.banner({
            icon: 'check',
            title: 'Deleted ' + util.formatNumber(result.runs) + ' run(s), ' +
              util.formatNumber(result.rows) + ' rows',
            text: result.blobs_unlinked
              ? 'Freed ' + util.formatBytes(result.bytes_freed) + ' of artifacts. ' + note
              : note
          })
        ]
      });
    }

    function unavailableCard() {
      if (settings && settings.snapshot) {
        return ui.card({
          title: 'Retention',
          body: ui.emptyState({
            title: 'This is an offline export',
            hint: 'There is no server here and nothing to delete. Open the live dashboard ' +
              'to manage retention.'
          })
        });
      }
      return ui.card({
        title: 'Retention',
        body: ui.emptyState({
          title: 'This dashboard cannot delete data',
          hint: 'Clearing data needs an administrator token, which is deliberately separate ' +
            'from the token CI publishes with. Create one with `test-hub token create --admin` ' +
            'and set TEST_HUB_ADMIN_TOKEN_HASHES — or run `test-hub prune` on the host.'
        })
      });
    }

    function render() {
      util.clear(body);
      if (settings === null) {
        body.appendChild(ui.loadingBlock('Loading settings', 'text', 3));
        return;
      }
      if (!settings.admin) {
        body.appendChild(unavailableCard());
        return;
      }
      if (settings.lastResult) {
        body.appendChild(resultCard(settings.lastResult));
      }
      body.appendChild(policyCard());
      body.appendChild(formCard());
      var predicted = previewCard();
      if (predicted) { body.appendChild(predicted); }
    }

    function fail(error) {
      util.clear(body);
      body.appendChild(ui.errorState({
        title: 'That did not work',
        error: error,
        onRetry: function () { load(); }
      }));
    }

    /* ------------------------------------------------------------ actions */

    function doPreview() {
      setBusy(true);
      provider.previewPrune(requestBody()).then(function (result) {
        preview = result;
        busy = false;
        render();
      })['catch'](function (error) {
        busy = false;
        fail(error);
      });
    }

    function doPrune() {
      if (!preview || !preview.runs) { return; }
      setBusy(true);
      provider.runPrune(requestBody({ confirm: true })).then(function (result) {
        settings.lastResult = result;
        preview = null;
        busy = false;
        render();
        // The overview and run list are now wrong by exactly what was deleted.
        ctx.store.reload();
      })['catch'](function (error) {
        busy = false;
        fail(error);
      });
    }

    function load() {
      util.clear(body);
      body.appendChild(ui.loadingBlock('Loading settings', 'text', 3));
      if (!provider.getRetention) {
        settings = { admin: false, snapshot: provider.kind === 'snapshot' };
        render();
        return;
      }
      provider.getRetention().then(function (result) {
        settings = result;
        if (result && result.retention_days) {
          form.amount = result.retention_days;
          form.unit = 'd';
        }
        render();
      })['catch'](function (error) {
        fail(error);
      });
    }

    load();

    return {
      el: root,
      update: function () { /* nothing here follows the live stream */ },
      destroy: function () { /* no listeners of its own */ }
    };
  }

  TestHub.views = TestHub.views || {};
  TestHub.views.settings = { create: create };
})(window, document);
