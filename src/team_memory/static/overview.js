'use strict';
(() => {
  const OPS = {insert: ['plus', 'Saved'], update: ['edit', 'Edited'], delete: ['x', 'Deleted'], confirm: ['check', 'Confirmed']};
  const ROLE_TONE = {manager: 'accent', editor: 'violet', reader: ''};

  function number(value) { return new Intl.NumberFormat('en').format(value); }

  function duration(seconds) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return days ? days + 'd ' + hours + 'h' : hours ? hours + 'h ' + minutes + 'm' : minutes + 'm';
  }

  function feed(ui, items, emptyState) {
    if (!items.length) return emptyState;
    return ui.h('ul', {class: 'feed'}, items.map((item) => {
      const [name, verb] = OPS[item.operation] || ['spark', item.operation];
      return ui.h('li', {}, ui.h('span', {class: 'op ' + item.operation, 'aria-hidden': 'true'}, ui.icon(name, 'sm')),
        ui.h('div', {class: 'what'}, ui.h('small', {text: verb + ' · ' + (item.scope_label || item.actor) + ' · #' + item.record_id}),
          ui.h('p', {text: item.excerpt || '(no text)'})), ui.time(item.at));
    }));
  }

  function shareRows(ui, scopes) {
    const peak = Math.max(1, ...scopes.map((s) => s.records));
    return ui.h('div', {class: 'grid'}, scopes.map((s) => ui.h('div', {class: 'grid'},
      ui.h('div', {class: 'record-meta'}, ui.chip(s.label, s.scope.kind === 'personal' ? 'accent' : s.scope.kind === 'team' ? 'violet' : ''),
        ui.h('span', {class: 'num', text: number(s.records) + (s.records === 1 ? ' record' : ' records')})),
      ui.svg('svg', {class: 'chart', viewBox: '0 0 100 6', height: 6, preserveAspectRatio: 'none', 'aria-hidden': 'true'},
        ui.svg('rect', {class: 'bar zero', x: 0, y: 0, width: 100, height: 6, rx: 3}),
        ui.svg('rect', {class: 'bar', x: 0, y: 0, width: Math.max(s.records ? 2 : 0, (s.records / peak) * 100), height: 6, rx: 3})))));
  }

  async function personal(ctx, ui) {
    const me = await ctx.api('overview/me');
    const records = me.scopes.reduce((sum, s) => sum + s.records, 0);
    return [
      ui.h('div', {class: 'kpis'},
        ui.kpi({label: 'My saves · 30 days', value: number(me.saves_30d), tone: 'accent',
          chart: ui.sparkline(me.trend_30d, {label: 'My saves per day, last 30 days'})}),
        ui.kpi({label: 'Records I can read', value: number(records), sub: me.scopes.length + ' workspaces'}),
        ui.kpi({label: 'Active tokens', value: number(me.active_tokens),
          sub: ui.h('a', {href: '#access', text: me.active_tokens ? 'Manage tokens' : 'Create one for your MCP client'})}),
        ui.kpi({label: 'Last activity', value: me.last_activity ? ui.relative(me.last_activity) : '—',
          sub: me.last_activity ? new Date(me.last_activity).toLocaleString() : 'Nothing saved yet'})),
      ui.h('div', {class: 'split'},
        ui.card({title: 'My recent changes', actions: ui.h('a', {class: 'btn sm', href: '#memory'}, 'Open memory')},
          feed(ui, me.recent, ui.empty({icon: 'brain', title: 'No changes yet',
            text: 'Save a decision or a fix so you and your agents can find it later.',
            action: {label: 'Save a record', onClick: () => ctx.navigate('memory')}}))),
        ui.card({title: 'Records by workspace', subtitle: 'Personal counts are visible only to you.'}, shareRows(ui, me.scopes))),
    ];
  }

  async function learningKpi(ctx, ui, path, describe) {
    if (!ctx.sections.some((s) => s.id === 'learning')) return null;
    try {
      return describe(await ctx.api('/learning/api/kpis/' + path));
    } catch (error) {
      return ui.kpi({label: 'Onboarding', value: '—', sub: error.message});
    }
  }

  function onboardingLink(ui, text) { return ui.h('a', {href: '#learning', text}); }

  async function team(ctx, ui, teamId) {
    const [data, learning] = await Promise.all([ctx.api('overview/team/' + encodeURIComponent(teamId)),
      learningKpi(ctx, ui, 'team/' + encodeURIComponent(teamId), (k) => (k.has_curriculum
        ? ui.kpi({label: 'Onboarding completion', value: k.completion_percent + '%', tone: k.pending_grading ? 'warn' : '',
          sub: [k.finished + '/' + k.members + ' finished', k.pending_grading ? onboardingLink(ui, k.pending_grading + ' to grade') : null]})
        : ui.kpi({label: 'Onboarding', value: 'No curriculum', sub: onboardingLink(ui, 'Build one from team memory')})))]);
    const members = ui.table([
      {title: 'Member', render: (m) => ui.person(m.name, m.user_id)},
      {title: 'Role', render: (m) => ui.chip(m.role, ROLE_TONE[m.role])},
      {title: '30 days', className: 'spark-cell', render: (m) => ui.sparkline(m.trend_30d, {height: 26, tone: 'violet', label: m.name + ' saves'})},
      {title: 'Saves', numeric: true, render: (m) => ui.h('span', {class: 'num', text: number(m.saves_30d)})},
      {title: 'Last activity', render: (m) => m.inactive
        ? ui.h('div', {class: 'record-meta'}, ui.chip('Inactive', 'warn', {dot: true}), ui.time(m.last_activity)) : ui.time(m.last_activity)},
    ], data.members, ui.empty({icon: 'users', title: 'No members yet', text: 'Ask a superadmin to add people to this department.'}));
    return [
      ui.h('div', {class: 'kpis'},
        ui.kpi({label: 'Team saves · 30 days', value: number(data.saves_30d), tone: 'accent',
          chart: ui.sparkline(data.trend_30d, {label: data.name + ' saves per day'})}),
        ui.kpi({label: 'Members', value: number(data.members.length)}),
        ui.kpi({label: 'Active records', value: number(data.records)}),
        ui.kpi({label: 'Inactive > ' + data.inactive_days + ' days', value: number(data.inactive.length),
          tone: data.inactive.length ? 'warn' : '', sub: data.inactive.length ? data.members.filter((m) => m.inactive).map((m) => m.name).join(', ') : 'Everyone is active'}),
        learning),
      ui.h('div', {class: 'split'},
        ui.card({title: 'Members', flush: true}, members),
        ui.card({title: 'Recently added to ' + data.name, actions: ui.h('a', {class: 'btn sm', href: '#department'}, 'Browse')},
          feed(ui, data.recent_records, ui.empty({icon: 'layers', title: 'Nothing saved yet',
            text: 'Records your team saves to this department appear here.'})))),
    ];
  }

  async function company(ctx, ui) {
    const [data, learning] = await Promise.all([ctx.api('overview/company'),
      learningKpi(ctx, ui, 'company', (k) => ui.kpi({label: 'Onboarding completion', value: k.completion_percent + '%',
        tone: k.pending_grading ? 'warn' : '', sub: [k.with_curriculum + '/' + k.departments.length + ' departments with a curriculum · ' + k.finished + ' finished',
          k.pending_grading ? onboardingLink(ui, k.pending_grading + ' to grade') : null]}))]);
    return [
      ui.h('div', {class: 'kpis'},
        ui.kpi({label: 'Company saves · 30 days', value: number(data.saves_30d), tone: 'accent',
          chart: ui.sparkline(data.trend_30d, {label: 'Company saves per day'})}),
        ui.kpi({label: 'Departments', value: number(data.departments.length)}),
        ui.kpi({label: 'Active users', value: number(data.active_users)}),
        ui.kpi({label: 'Team records', value: number(data.records)}),
        learning),
      ui.card({title: 'Departments', flush: true}, ui.table([
        {title: 'Department', render: (d) => ui.h('div', {class: 'person'}, ui.h('div', {}, ui.h('strong', {text: d.name}), ui.h('small', {text: d.team_id})))},
        {title: 'Members', numeric: true, render: (d) => ui.h('span', {class: 'num', text: number(d.members)})},
        {title: 'Records', numeric: true, render: (d) => ui.h('span', {class: 'num', text: number(d.records)})},
        {title: '30-day activity', className: 'spark-cell', render: (d) => ui.sparkline(d.trend_30d, {height: 26, tone: 'violet', label: d.name + ' saves'})},
        {title: 'Last activity', render: (d) => ui.time(d.last_activity)},
      ], data.departments, ui.empty({icon: 'building', title: 'No departments yet', text: 'A superadmin creates departments under Administration.'}))),
    ];
  }

  async function system(ctx, ui) {
    const data = await ctx.api('overview/system');
    const providers = data.providers.map((p) => ui.h('div', {class: 'record-meta'},
      ui.h('strong', {text: (p.target === 'llm' ? 'LLM: ' : 'Embeddings: ') + p.label}),
      ui.statusPill(p.check, {configured: p.configured, problem: p.problem})));
    const logins = data.logins;
    return [
      ui.h('div', {class: 'kpis'},
        ui.kpi({label: 'Version', value: data.version, sub: 'released ' + data.release_date}),
        ui.kpi({label: 'Uptime', value: duration(data.uptime_seconds)}),
        ui.kpi({label: 'Workers', value: data.workers.running + ' / ' + data.workers.max,
          sub: data.workers.busy ? ui.chip('Busy', 'violet', {dot: true}) : ui.chip('Idle', 'good', {dot: true})}),
        ui.kpi({label: 'Failed sign-ins · 24 h', value: number(logins.failures), tone: logins.failures ? 'bad' : '',
          sub: logins.failures ? logins.targeted_users + ' user IDs targeted' : number(logins.successes) + ' successful sign-ins',
          chart: ui.bars(logins.failures_by_hour, {height: 32, tone: 'bad', label: 'Failed sign-ins per hour'})})),
      ui.h('div', {class: 'split'},
        ui.card({title: 'Recent administration', actions: ui.h('a', {class: 'btn sm', href: '#audit'}, 'Audit log')},
          ui.h('ul', {class: 'feed'}, data.recent_audit.map((e) => ui.h('li', {},
            ui.avatar(e.actor || '?', e.actor || '?', 'sm'),
            ui.h('div', {class: 'what'}, ui.h('small', {text: e.actor || 'system'}), ui.h('p', {text: e.action + ' · ' + e.subject})),
            ui.time(e.at))))),
        ui.h('div', {class: 'grid'},
          ui.card({title: 'Providers', actions: ui.h('a', {class: 'btn sm', href: '#settings'}, 'Configure')}, ui.h('div', {class: 'grid'}, providers)),
          ui.card({title: 'Pending invites', subtitle: data.users.without_password + ' active users have no password yet'},
            data.pending_invites.length ? ui.h('ul', {class: 'feed'}, data.pending_invites.map((i) => ui.h('li', {},
              ui.avatar(i.name, i.user_id, 'sm'), ui.h('div', {class: 'what'}, ui.h('strong', {text: i.name}), ui.h('small', {text: 'issued by ' + i.created_by})),
              ui.h('span', {class: 'muted'}, 'expires ', ui.time(i.expires_at)))))
              : ui.empty({icon: 'key', text: 'No open invites.', action: {label: 'Invite a user', onClick: () => ctx.navigate('admin-users')}})))),
    ];
  }

  function block(ui, title, build) {
    const body = ui.h('div', {class: 'grid'});
    ui.load(body, build);
    return ui.h('section', {class: 'grid', 'aria-label': title}, title ? ui.h('h2', {text: title}) : null, body);
  }

  window.TamSections.overview = async (container, ctx) => {
    const ui = ctx.ui;
    const role = ctx.user.org_role;
    const managed = ctx.teams.filter((t) => t.role === 'manager');
    const first = ctx.user.display_name.split(/\s+/)[0];
    container.append(ui.h('div', {class: 'hero'}, ui.h('h2', {text: 'Hello, ' + first})));
    if (role === 'superadmin') container.append(block(ui, 'System', () => system(ctx, ui)));
    if (role === 'superadmin' || role === 'company_viewer') container.append(block(ui, 'Company', () => company(ctx, ui)));
    if (managed.length) {
      const picker = managed.length > 1 ? ui.h('select', {'aria-label': 'Department'}, managed.map((t) => ui.h('option', {value: t.team_id}, t.name))) : null;
      const body = ui.h('div', {class: 'grid'});
      const show = () => ui.load(body, () => team(ctx, ui, picker ? picker.value : managed[0].team_id));
      if (picker) picker.addEventListener('change', show);
      container.append(ui.h('section', {class: 'grid', 'aria-label': 'My department'},
        ui.h('div', {class: 'hero'}, ui.h('h2', {text: picker ? 'My departments' : managed[0].name}), picker), body));
      show();
    }
    container.append(block(ui, role === 'member' && !managed.length ? '' : 'Me', () => personal(ctx, ui)));
  };
})();
