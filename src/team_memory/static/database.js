'use strict';
(() => {
  // Loaded by index.html for the setup wizard and again as the Database section script.
  if (window.TamDatabase) return;
  const POLL_MS = 1000;
  const EXAMPLE_DSN = 'postgresql://tam:password@db.example.com:5432/tam?sslmode=require';
  const CHECKS = {connect: 'Connection and sign-in', server_version: 'PostgreSQL version', encoding: 'Encoding and collation',
    extensions: 'Extension vector', privileges: 'Role privileges', target_state: 'Target database'};
  const STATUS = {passed: ['Passed', 'good'], warning: ['Warning', 'warn'], failed: ['Failed', 'bad'], skipped: ['Skipped', '']};
  const STATUS_ICON = {passed: 'check', warning: 'alert', failed: 'alert', skipped: 'clock'};
  const TARGETS = {empty: 'Empty database', same_installation: 'This installation (the copy resumes)',
    foreign_installation: 'Another TAM installation', unknown: 'Unknown'};
  const PHASES = {preflight: 'Checking the target', maintenance: 'Pausing writes', copy_control: 'Copying accounts and settings',
    copy_workspaces: 'Copying memory spaces', verify: 'Verifying the copy', activate: 'Switching over', done: 'Finished',
    failed: 'Failed', cancelled: 'Cancelled'};
  const BACKENDS = {sqlite: 'SQLite', postgres: 'PostgreSQL'};
  const SOURCES = {web: ['Set here', 'accent'], env: ['From environment', 'violet'], default: ['Default', '']};
  const KINDS = {identity: 'Accounts', learning: 'Learning', workspace: 'Memory space'};
  const number = (value) => new Intl.NumberFormat('en').format(value);

  function bytes(value) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = value;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return (unit ? size.toFixed(1) : String(size)) + ' ' + units[unit];
  }

  function duration(seconds) {
    if (seconds < 60) return Math.max(1, Math.round(seconds)) + ' s';
    if (seconds < 3600) return Math.round(seconds / 60) + ' min';
    return (seconds / 3600).toFixed(1) + ' h';
  }

  function sinceText(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : 'at ' + date.toLocaleString();
  }

  function databaseName(masked) {
    const tail = masked.slice(masked.lastIndexOf('@') + 1);
    const path = tail.slice(tail.indexOf('/') + 1).split('?')[0];
    try { return decodeURIComponent(path); } catch (error) { return path; }
  }

  let fieldIds = 0;
  function dsnInput(dash, {placeholder, label = 'Connection string'}) {
    const {h} = dash;
    fieldIds += 1;
    const id = 'dsn-' + fieldIds;
    const input = h('input', {id, type: 'password', class: 'mono', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off',
      maxlength: '1024', placeholder: placeholder || EXAMPLE_DSN, 'aria-describedby': id + '-hint'});
    const toggle = h('button', {type: 'button', class: 'btn sm', 'aria-pressed': 'false', 'aria-controls': id}, 'Show');
    const paintToggle = (shown) => {
      input.type = shown ? 'text' : 'password';
      toggle.setAttribute('aria-pressed', String(shown));
      toggle.textContent = shown ? 'Hide' : 'Show';
      toggle.setAttribute('aria-label', (shown ? 'Hide' : 'Show') + ' connection string');
    };
    toggle.addEventListener('click', () => paintToggle(input.type === 'password'));
    paintToggle(false);
    const node = h('div', {class: 'field'}, h('label', {for: id}, h('span', {text: label})), h('div', {class: 'dsn-input'}, input, toggle),
      h('small', {id: id + '-hint', text: 'One postgresql:// URI including the password. Allowed parameters: sslmode, sslrootcert=system, connect_timeout, application_name, target_session_attrs, channel_binding. Certificate, socket or passwordless login: set TAM_TEAM_DATABASE_URL on the server instead.'}));
    return {node, input, value: () => input.value.trim(), clear: () => { input.value = ''; paintToggle(false); }};
  }

  function backendSwitch(dash, current, onChange, label = 'Database backend') {
    const {h} = dash;
    const group = h('div', {class: 'segmented two', role: 'radiogroup', 'aria-label': label});
    let chosen = current;
    const paint = () => group.replaceChildren(...Object.entries(BACKENDS).map(([id, text]) => h('button', {type: 'button', role: 'radio',
      'aria-checked': String(id === chosen), tabindex: id === chosen ? '0' : '-1', 'data-backend': id,
      onclick: () => { if (id !== chosen) { chosen = id; paint(); group.querySelector('[aria-checked=true]').focus(); onChange(id); } }}, text)));
    group.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      const ids = Object.keys(BACKENDS);
      chosen = ids[(ids.indexOf(chosen) + 1) % ids.length];
      paint();
      group.querySelector('[aria-checked=true]').focus();
      onChange(chosen);
    });
    paint();
    return group;
  }

  function checkList(dash, report) {
    const {h, ui} = dash;
    const items = report.checks.map((check) => {
      const [label, tone] = STATUS[check.status];
      return h('li', {class: 'db-check ' + check.status},
        h('span', {class: 'op ' + (tone || ''), 'aria-hidden': 'true'}, ui.icon(STATUS_ICON[check.status], 'sm')),
        h('div', {class: 'what'}, h('strong', {text: CHECKS[check.id] || check.id}), h('p', {text: check.message}),
          check.dba_sql.length ? h('div', {class: 'db-sql'}, h('small', {text: 'Ask your database administrator to run:'}),
            h('pre', {class: 'cmd', text: check.dba_sql.join('\n')}), h('div', {class: 'actions'}, ui.copyButton(check.dba_sql.join('\n')))) : null),
        ui.chip(label, tone, {dot: true}));
    });
    const head = h('div', {class: 'record-meta'}, ui.chip(report.ok ? 'Ready' : 'Not ready', report.ok ? 'good' : 'bad', {dot: true}),
      h('code', {text: report.dsn_masked}), report.server_version ? ui.chip('PostgreSQL ' + report.server_version) : null,
      ui.chip(TARGETS[report.target_state] || report.target_state, report.target_state === 'foreign_installation' ? 'bad' : ''),
      ui.time(report.checked_at));
    return h('div', {class: 'db-report', role: 'region', 'aria-label': 'Connection test result'}, head,
      report.warnings.map((text) => h('p', {class: 'db-warning', role: 'note'}, ui.icon('alert', 'sm'), text)),
      h('ul', {class: 'feed'}, items));
  }

  function quarantineGroup(dash, title, items) {
    const {h, ui} = dash;
    if (!items.length) return null;
    return h('div', {class: 'db-quarantine-group'}, h('strong', {text: title}), ui.table([
      {title: 'Table', render: (q) => h('div', {class: 'person'}, h('div', {}, h('strong', {class: 'mono', text: q.table}),
        h('small', {class: 'mono', text: q.database.length > 24 ? q.database.slice(0, 24) + '…' : q.database})))},
      {title: 'Rows', numeric: true, render: (q) => h('span', {class: 'num', text: number(q.rows)})},
      {title: 'Why', render: (q) => h('div', {class: 'record-meta'}, q.reasons.map((reason) => h('code', {text: reason})))},
      {title: 'Examples', render: (q) => q.sample_pks.length ? h('code', {class: 'dim', text: q.sample_pks.join(', ')}) : '—'},
    ], items));
  }

  function quarantineView(dash, items, total, running = false) {
    const {h, ui} = dash;
    if (!total) return null;
    const records = items.filter((q) => !q.audit);
    const history = items.filter((q) => q.audit);
    return h('div', {class: 'db-quarantine', role: 'region', 'aria-label': 'Quarantined rows'},
      h('div', {class: 'setup-note', role: 'note'}, ui.icon('alert', 'sm'), h('p', {text: number(total) + ' row' + (total === 1 ? '' : 's') +
        (running ? ' were' : ' will be') + ' set aside: they point to records that no longer exist, so PostgreSQL would reject them. ' +
        'They are kept in a quarantine table of their memory space instead of being dropped; the migration is not blocked.'})),
      h('details', {class: 'advanced'}, h('summary', {text: 'Quarantined rows by table (' + items.length + ')'}),
        h('div', {class: 'db-table'}, quarantineGroup(dash, 'Memory records', records),
          quarantineGroup(dash, 'Audit history (edit history and authorship)', history))));
  }

  function planView(dash, plan) {
    const {h, ui} = dash;
    const databases = [...plan.databases].sort((a, b) => b.bytes - a.bytes);
    const workspaces = databases.filter((d) => d.kind === 'workspace').length;
    return h('div', {class: 'db-report', role: 'region', 'aria-label': 'Dry run result'},
      h('div', {class: 'kpis'},
        ui.kpi({label: 'Rows to copy', value: number(plan.total_rows)}),
        ui.kpi({label: 'Data size', value: bytes(plan.total_bytes)}),
        ui.kpi({label: 'Memory spaces', value: number(workspaces)}),
        ui.kpi({label: 'Estimated downtime', value: duration(plan.estimated_seconds), tone: plan.ready ? 'accent' : 'warn'})),
      plan.blockers.length ? h('div', {class: 'setup-note', role: 'note'}, ui.icon('alert', 'sm'),
        h('div', {}, h('strong', {text: 'Migration is blocked'}), h('ul', {}, plan.blockers.map((text) => h('li', {text})))))
        : h('div', {class: 'setup-note info', role: 'note'}, ui.icon('check', 'sm'),
          h('p', {text: (plan.resumable ? 'The target already holds part of this installation; the copy resumes. ' : '') +
            'This plan is valid until ' + new Date(plan.expires_at).toLocaleTimeString() + '. Nothing was written to the target.'})),
      quarantineView(dash, plan.quarantine, plan.quarantined_rows),
      checkList(dash, plan.report),
      h('details', {class: 'advanced'}, h('summary', {text: 'Rows and size per database (' + databases.length + ')'}),
        h('div', {class: 'db-table'}, ui.table([
          {title: 'Database', render: (d) => h('div', {class: 'person'}, h('div', {}, h('strong', {text: KINDS[d.kind] || d.kind}),
            h('small', {class: 'mono', text: d.name.length > 24 ? d.name.slice(0, 24) + '…' : d.name})))},
          {title: 'Tables', numeric: true, render: (d) => h('span', {class: 'num', text: number(d.tables.length)})},
          {title: 'Rows', numeric: true, render: (d) => h('span', {class: 'num', text: number(d.rows)})},
          {title: 'Size', numeric: true, render: (d) => h('span', {class: 'num', text: bytes(d.bytes)})},
        ], databases, 'No data to copy.'))));
  }

  function progressPanel(dash, initial, {onFinish}) {
    const {h, ui} = dash;
    const bar = h('progress', {max: '100', value: '0', 'aria-label': 'Migration progress'});
    const title = h('strong');
    const percent = h('span', {class: 'num'});
    const detail = h('p', {class: 'muted'});
    const note = h('p', {class: 'muted', role: 'status', 'aria-live': 'polite'});
    const cancel = h('button', {type: 'button', class: 'btn danger'}, ui.icon('x', 'sm'), 'Cancel migration');
    const quarantine = h('div');
    const paused = h('p', {class: 'db-warning', role: 'note'}, ui.icon('alert', 'sm'),
      'Changes are paused until the migration ends: signing in and out, saving and editing all answer “temporarily unavailable”. Stay signed in to follow the progress here.');
    const node = h('div', {class: 'db-progress', role: 'region', 'aria-label': 'Migration progress'},
      h('div', {class: 'db-progress-head'}, title, percent), bar, detail, note, paused, quarantine, h('div', {class: 'actions'}, cancel));
    let shownQuarantine = -1;
    let job = initial;
    let timer = null;

    function paint() {
      title.textContent = PHASES[job.phase] || job.phase;
      percent.textContent = job.percent.toFixed(1) + ' %';
      bar.value = job.percent;
      bar.setAttribute('aria-valuetext', job.percent.toFixed(1) + ' percent, ' + title.textContent);
      const parts = [];
      if (job.workspace_total) parts.push('Memory space ' + job.workspace_index + ' of ' + job.workspace_total);
      if (job.table) parts.push('table ' + job.table);
      if (job.rows_total) parts.push(number(job.rows_copied) + ' of ' + number(job.rows_total) + ' rows');
      if (job.quarantined_rows) parts.push(number(job.quarantined_rows) + ' set aside');
      if (job.resumed) parts.push('resumed');
      detail.textContent = parts.join(' · ') || 'Started by ' + job.started_by;
      cancel.hidden = !job.cancellable;
      paused.hidden = job.terminal;
      if (job.quarantined_rows !== shownQuarantine) {
        shownQuarantine = job.quarantined_rows;
        quarantine.replaceChildren(...[quarantineView(dash, job.quarantine, job.quarantined_rows, true)].filter(Boolean));
      }
      if (job.cancel_requested && !job.terminal) note.textContent = 'Cancelling after the current step…';
      if (job.phase === 'failed') note.textContent = job.error;
      node.classList.toggle('failed', job.phase === 'failed');
    }

    async function poll() {
      timer = null;
      if (!node.isConnected) return;
      try {
        job = (await dash.api('admin/database/migration')) || job;
        note.textContent = '';
        paint();
      } catch (error) {
        if (error.status === 401) return;
        note.textContent = 'Waiting for the server: ' + error.message;
      }
      if (job.terminal) { onFinish(job); return; }
      timer = setTimeout(poll, POLL_MS);
    }

    cancel.addEventListener('click', async () => {
      const ok = await ui.confirm('Cancel the migration?', 'The copy stops, partial data in PostgreSQL is removed and the server keeps running on SQLite.', 'Cancel migration');
      if (!ok) return;
      const reply = await dash.busy(cancel, () => dash.api('admin/database/migration/cancel', {method: 'POST'}));
      if (reply) { job = reply; paint(); }
    });
    paint();
    if (job.terminal) onFinish(job);
    else timer = setTimeout(poll, POLL_MS);
    return {node, stop: () => { if (timer) clearTimeout(timer); }};
  }

  async function typedConfirm(dash, {title, body, label, expected, confirm}) {
    const {h, ui} = dash;
    const input = h('input', {autocomplete: 'off', spellcheck: 'false', 'aria-label': label});
    const pending = ui.modal({title, confirm, tone: 'danger', body: [...body, ui.field(label, input)]});
    const button = input.closest('dialog').querySelector('.dialog-foot .btn:last-child');
    const sync = () => { button.disabled = input.value.trim() !== expected; };
    input.addEventListener('input', sync);
    sync();
    input.focus();
    return (await pending) && input.value.trim() === expected;
  }

  function admin(ctx) {
    const {h, ui} = ctx;
    const host = h('div', {class: 'grid'});
    let view = null;
    let chosen = null;
    let progress = null;

    async function reload() {
      if (progress) progress.stop();
      progress = null;
      view = await ctx.api('admin/database');
      chosen = view.backend;
      render();
    }

    function summary() {
      const [sourceText, sourceTone] = SOURCES[view.source];
      const rows = [h('div', {class: 'record-meta'}, ui.chip(BACKENDS[view.backend], 'accent', {dot: true}), ui.chip(sourceText, sourceTone),
        view.generation ? h('span', {text: 'Configuration ' + view.generation}) : null,
        view.updated_at ? h('span', {}, 'changed ', ui.time(view.updated_at), view.updated_by ? ' by ' + view.updated_by : '') : null)];
      if (view.dsn_masked) rows.push(h('div', {class: 'masked'}, h('code', {text: view.dsn_masked})));
      if (view.source === 'env') rows.push(h('p', {class: 'muted', text: 'Set by TAM_TEAM_DATABASE_URL on the server. A connection saved here takes precedence over it.'}));
      for (const text of view.warnings) rows.push(h('p', {class: 'db-warning', role: 'note'}, ui.icon('alert', 'sm'), text));
      return rows;
    }

    function lastJob() {
      const job = view.migration;
      if (!job || !job.terminal) return null;
      const tone = {done: 'good', failed: 'bad', cancelled: 'warn'}[job.phase];
      return h('div', {class: 'record-meta'}, h('span', {text: 'Last migration'}), ui.chip(PHASES[job.phase], tone, {dot: true}),
        ui.time(job.finished_at), job.error ? h('span', {class: 'dim', text: job.error}) : null);
    }

    function postgresForm() {
      const dsn = dsnInput(ctx, {placeholder: view.backend === 'postgres' ? view.dsn_masked : EXAMPLE_DSN,
        label: view.backend === 'postgres' ? 'New connection string' : 'Connection string'});
      const result = h('div', {class: 'grid'});
      if (view.last_check) result.append(checkList(ctx, view.last_check));
      let plan = null;
      const need = () => {
        if (!dsn.value()) throw new Error('Paste the PostgreSQL connection string first');
        return {dsn: dsn.value()};
      };
      const test = h('button', {type: 'button', class: 'btn'}, ui.icon('pulse', 'sm'), 'Test');
      test.addEventListener('click', () => ctx.busy(test, async () => {
        const report = await ctx.api('admin/database/test', {method: 'POST', body: need()});
        result.replaceChildren(checkList(ctx, report));
      }));
      const actions = [test];
      if (view.backend === 'sqlite') {
        const dry = h('button', {type: 'button', class: 'btn'}, ui.icon('list', 'sm'), 'Dry run');
        const migrate = h('button', {type: 'button', class: 'btn primary', disabled: true}, ui.icon('arrow-up', 'sm'), 'Migrate');
        const reason = h('small', {class: 'muted', text: 'Run a dry run first; Migrate uses its plan.'});
        const invalidate = () => { plan = null; migrate.disabled = true; reason.hidden = false; };
        dsn.input.addEventListener('input', invalidate);
        dry.addEventListener('click', () => ctx.busy(dry, async () => {
          invalidate();
          const reply = await ctx.api('admin/database/plan', {method: 'POST', body: need()});
          result.replaceChildren(planView(ctx, reply));
          plan = reply.ready ? reply : null;
          migrate.disabled = !plan;
          reason.hidden = Boolean(plan);
        }));
        migrate.addEventListener('click', async () => {
          if (!plan) return;
          const name = databaseName(plan.target);
          const ok = await typedConfirm(ctx, {title: 'Move all data to PostgreSQL?', confirm: 'Start migration', label: 'Type the database name "' + name + '" to confirm', expected: name,
            body: [h('p', {text: 'The server stops accepting changes for about ' + duration(plan.estimated_seconds) + ' while it copies ' + number(plan.total_rows) + ' rows (' + bytes(plan.total_bytes) + ') to ' + plan.target + '.'}),
              h('p', {text: 'AI clients get “temporarily unavailable” until the switch finishes, and nobody can sign in or out meanwhile. SQLite files are archived, not deleted, and stay available for rollback.'})]});
          if (!ok) return;
          const job = await ctx.busy(migrate, () => ctx.api('admin/database/migrate', {method: 'POST', body: {plan_id: plan.plan_id, confirm: true}}));
          if (!job) return;
          dsn.clear();
          view = {...view, migration: job};
          render();
        });
        actions.push(dry, migrate, reason);
      } else {
        const repoint = h('button', {type: 'button', class: 'btn primary'}, ui.icon('edit', 'sm'), 'Repoint');
        repoint.addEventListener('click', async () => {
          const body = await ctx.busy(null, async () => need());
          if (!body) return;
          const ok = await ui.confirm('Use the new connection?', 'Use this when the same database moved to another host or got a new password. The server checks that it is the same installation, then restarts its workers.', 'Repoint', 'primary');
          if (!ok) return;
          const reply = await ctx.busy(repoint, () => ctx.api('admin/database/repoint', {method: 'POST', body}));
          if (!reply) return;
          dsn.clear();
          ui.toast('Connection updated', 'good');
          await reload();
        });
        actions.push(repoint);
      }
      return [dsn.node, h('div', {class: 'actions'}, actions), result];
    }

    function rollbackForm() {
      if (view.backend === 'sqlite') {
        return [h('p', {class: 'muted', text: 'Everything is stored in SQLite files in the server data directory. Nothing else to run.'})];
      }
      if (!view.rollback_available) {
        return [h('p', {class: 'muted', text: view.source === 'env'
          ? 'PostgreSQL is configured by TAM_TEAM_DATABASE_URL; remove it on the server to change the backend.'
          : 'There is no SQLite archive to return to.'})];
      }
      const archive = view.archive;
      const rollback = h('button', {type: 'button', class: 'btn danger'}, ui.icon('history', 'sm'), 'Roll back to SQLite');
      rollback.addEventListener('click', async () => {
        const org = await ctx.busy(rollback, () => ctx.api('admin/organization'));
        if (!org) return;
        const expected = org.name || '';
        if (!expected) { ui.toast('Set the organization name under System first; it confirms the rollback', 'bad'); return; }
        const ok = await typedConfirm(ctx, {title: 'Roll back to SQLite?', confirm: 'Roll back', label: 'Type the organization name "' + expected + '" to confirm', expected,
          body: [h('p', {class: 'db-warning', role: 'note'}, ui.icon('alert', 'sm'), 'Everything written to PostgreSQL after the switch is lost: memories, accounts, tokens and settings changed since then.'),
            h('p', {text: 'The server returns to the SQLite archive from ' + new Date(archive.created_at).toLocaleString() + '. PostgreSQL itself is not changed.'})]});
        if (!ok) return;
        const reply = await ctx.busy(rollback, () => ctx.api('admin/database/rollback', {method: 'POST', body: {organization: expected}}));
        if (!reply) return;
        ui.toast('Rolled back to SQLite', 'good');
        await reload();
      });
      return [h('div', {class: 'setup-note', role: 'note'}, ui.icon('alert', 'sm'),
        h('p', {text: 'Rolling back restores the SQLite archive made when PostgreSQL was activated (' + bytes(archive.bytes) + '). Changes made on PostgreSQL since then are lost.'})),
      h('div', {class: 'actions'}, rollback)];
    }

    function render() {
      const running = view.migration && !view.migration.terminal;
      const body = [...summary()];
      if (view.maintenance && view.maintenance.reason === 'lease_lost') {
        body.push(h('div', {class: 'setup-note bad', role: 'alert'}, ui.icon('alert', 'sm'), h('div', {},
          h('strong', {text: 'Another TAM server is using this database'}),
          h('p', {text: 'This server lost its lease on PostgreSQL ' + sinceText(view.maintenance.since) + ' and accepts no changes: AI clients and dashboard edits get “temporarily unavailable”. Stop the other server or correct the connection, then restart this server.'}))));
      } else if (running) {
        progress = progressPanel(ctx, view.migration, {onFinish: (job) => {
          const messages = {done: ['Migration finished; the server now runs on PostgreSQL', 'good'], cancelled: ['Migration cancelled; still on SQLite', 'info'],
            failed: ['Migration failed; still on SQLite', 'bad']};
          ui.toast(...messages[job.phase]);
          reload().catch((error) => ui.toast(error.message, 'bad'));
        }});
        body.push(progress.node);
      } else {
        body.push(h('div', {class: 'field'}, h('span', {text: 'Backend', 'aria-hidden': 'true'}), backendSwitch(ctx, chosen, (id) => { chosen = id; render(); })));
        body.push(...(chosen === 'postgres' ? postgresForm() : rollbackForm()));
        body.push(lastJob());
      }
      host.replaceChildren(ui.card({title: 'Database', subtitle: 'Where the server keeps accounts, settings and memory. The connection string is encrypted at rest; only its masked form is shown.'}, h('div', {class: 'grid'}, body)));
    }

    host.append(ui.skeleton(3, true));
    reload().catch((error) => host.replaceChildren(ui.empty({icon: 'alert', title: 'Database settings could not be loaded', text: error.message,
      action: {label: 'Try again', onClick: () => reload().catch((again) => ui.toast(again.message, 'bad'))}})));
    return host;
  }

  window.TamDatabase = {admin, dsnInput, backendSwitch, checkList, BACKENDS, EXAMPLE_DSN};
  window.TamSections = window.TamSections || {};
  window.TamSections.database = async (container, ctx) => { container.append(admin(ctx)); };
})();
