'use strict';
(() => {
  const API = '/reports/api/';
  const PERIODS = [['day', 'Today'], ['week', 'This week'], ['month', 'This month'], ['all', 'All time'], ['custom', 'Custom']];
  const PREVIOUS = {day: 'Yesterday', week: 'Last week', month: 'Last month'};
  const STEPPED = new Set(['day', 'week', 'month']);
  const KPI_KEYS = [['records', 'Records written', 'accent'], ['decisions', 'Decisions', ''], ['errors', 'Errors', 'warn'],
    ['active_days', 'Active days', ''], ['open_items', 'Open items', '']];
  const SEVERITY_TONE = {critical: 'bad', high: 'bad', medium: 'warn', low: ''};
  const OPEN_LABEL = {next_step: ['Next step', 'accent'], open_question: ['Question', 'violet'], pitfall: ['Pitfall', 'warn']};
  const MAX_BARS = 92;
  const DAY_MS = 86400000;
  const SUMMARY_CHARS = 8;
  const SUMMARY_LIMIT = 12;
  const PREFIX = {knowledge: '#', error: 'err#', rule: 'rule#', observation: 'obs#', session_summary: 'summary:'};

  const number = (value) => new Intl.NumberFormat('en').format(value);
  const zone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; } catch (error) { return null; } };
  const plural = (n, word) => number(n) + ' ' + word + (n === 1 ? '' : 's');
  const stamp = (value) => String(value || '').slice(0, 16).replace('T', ' ');

  function refLabel(ref) {
    const id = ref.kind === 'session_summary' && String(ref.id).length > SUMMARY_LIMIT ? String(ref.id).slice(0, SUMMARY_CHARS) : String(ref.id);
    return (ref.workspace ? ref.workspace + ' ' : '') + PREFIX[ref.kind] + id;
  }

  function refs(ui, sources) {
    return ui.h('span', {class: 'rep-refs'}, sources.map((ref) => ui.h('code', {text: refLabel(ref),
      title: ref.kind === 'knowledge' ? 'memory_get id ' + ref.id : ref.kind.replace('_', ' ')})));
  }

  function change(ui, metric, compact = false) {
    if (metric.previous === null) return ui.h('span', {class: 'muted', text: compact ? '—' : 'no comparison'});
    if (metric.delta === 0) return ui.h('span', {class: 'muted', text: compact ? 'same' : 'same as before (' + number(metric.previous) + ')'});
    const up = metric.delta > 0;
    const pct = metric.change_pct === null ? 'new' : (up ? '+' : '−') + Math.abs(metric.change_pct) + '%';
    return ui.h('span', {class: 'rep-delta ' + (up ? 'up' : 'down')}, ui.icon(up ? 'arrow-up' : 'arrow-down', 'sm'),
      (up ? '+' : '−') + number(Math.abs(metric.delta)) + ' (' + pct + ')' + (compact ? '' : ' vs ' + number(metric.previous)));
  }

  function series(report) {
    const start = new Date(report.window.first_day + 'T00:00:00Z').getTime();
    const days = Math.max(1, report.window.days);
    const counts = new Map(report.timeline.map((d) => [d.date, d.records + d.errors]));
    const values = Array.from({length: days}, (_, i) => counts.get(new Date(start + i * DAY_MS).toISOString().slice(0, 10)) || 0);
    if (values.length <= MAX_BARS) return {values, unit: 'day'};
    const weeks = [];
    for (let i = 0; i < values.length; i += 7) weeks.push(values.slice(i, i + 7).reduce((a, b) => a + b, 0));
    return {values: weeks.slice(-MAX_BARS), unit: 'week'};
  }

  function listing(ui, title, list, render, emptyText) {
    const subtitle = list.total > list.items.length ? 'Showing ' + list.items.length + ' of ' + list.total : number(list.total) + ' in this period';
    return ui.card({title, subtitle}, list.items.length ? ui.h('ul', {class: 'rep-list'}, list.items.map(render))
      : ui.empty({icon: 'spark', text: emptyText}));
  }

  function recordRow(ui, item, lead) {
    const meta = [item.importance && item.importance !== 'medium' ? ui.chip(item.importance, item.importance === 'low' ? '' : 'accent') : null,
      item.status !== 'active' ? ui.chip(item.status, 'violet') : null, item.author ? ui.h('span', {class: 'muted', text: item.author}) : null,
      ui.h('span', {class: 'muted num', text: stamp(item.at)}), refs(ui, item.sources)];
    return ui.h('li', {}, ui.h('strong', {text: item.title}),
      lead ? ui.h('p', {}, ui.h('span', {class: 'rep-label', text: lead.label}), lead.text || ui.h('em', {class: 'muted', text: 'not recorded'})) : null,
      ui.h('div', {class: 'record-meta'}, meta));
  }

  function errorsCard(ui, report) {
    const patterns = report.error_patterns.length ? ui.table([
      {title: 'Pattern', render: (p) => ui.h('code', {text: p.pattern})},
      {title: 'This period', numeric: true, render: (p) => ui.h('span', {class: 'num', text: number(p.count)})},
      {title: 'All time', numeric: true, render: (p) => ui.h('span', {class: 'num', text: number(p.total)})},
      {title: 'Recurring', render: (p) => p.recurring ? ui.chip('recurring', 'warn', {dot: true}) : ui.chip('once')},
      {title: 'Last seen', render: (p) => ui.h('span', {class: 'num', text: stamp(p.last_seen)})},
      {title: 'Errors', render: (p) => refs(ui, p.sources)},
    ], report.error_patterns) : null;
    const rows = report.errors.items.map((item) => ui.h('li', {},
      ui.h('div', {class: 'record-meta'}, ui.chip(item.severity || 'medium', SEVERITY_TONE[item.severity] || ''),
        ui.chip(item.status, item.status === 'resolved' ? 'good' : 'warn', {dot: true})),
      ui.h('strong', {text: item.title}),
      item.why ? ui.h('p', {}, ui.h('span', {class: 'rep-label', text: 'Root cause'}), item.why) : null,
      item.detail ? ui.h('p', {}, ui.h('span', {class: 'rep-label', text: 'Fix'}), item.detail) : null,
      ui.h('div', {class: 'record-meta'}, ui.h('span', {class: 'muted num', text: stamp(item.at)}), refs(ui, item.sources))));
    const subtitle = report.errors.total ? number(report.errors.total) + ' errors · ' + report.error_patterns.filter((p) => p.recurring).length + ' recurring patterns' : 'No errors logged';
    return ui.card({title: 'Errors', subtitle}, patterns, rows.length ? ui.h('ul', {class: 'rep-list'}, rows)
      : ui.empty({icon: 'check', text: 'No errors were logged in this period.'}));
  }

  function openCard(ui, report) {
    return listing(ui, 'Open tasks and next steps', report.open_items, (item) => {
      const [label, tone] = OPEN_LABEL[item.kind];
      return ui.h('li', {}, ui.h('div', {class: 'record-meta'}, ui.chip(label, tone),
        item.picked_up ? ui.chip('picked up', 'good', {dot: true}) : null,
        item.occurrences > 1 ? ui.chip('mentioned ' + item.occurrences + '×') : null),
      ui.h('p', {text: item.text}),
      ui.h('div', {class: 'record-meta'}, ui.h('span', {class: 'muted num', text: stamp(item.at)}), refs(ui, item.sources)));
    }, 'No next steps or pitfalls were recorded in session summaries.');
  }

  function countedCard(ui, title, list, kindColumn, emptyText) {
    const columns = [{title: kindColumn ? 'Entity' : 'File', render: (c) => ui.h('code', {class: 'rep-name' + (kindColumn ? ' keep' : ''), text: c.name})}];
    if (kindColumn) columns.push({title: 'Type', render: (c) => ui.chip(c.kind, 'violet')});
    columns.push({title: kindColumn ? 'Records' : 'Touches', numeric: true, render: (c) => ui.h('span', {class: 'num', text: number(c.count)})},
      {title: 'Sources', render: (c) => refs(ui, c.sources)});
    return ui.card({title, subtitle: list.total > list.items.length ? 'Top ' + list.items.length + ' of ' + list.total : null, flush: true},
      ui.table(columns, list.items, emptyText));
  }

  function timelineCard(ui, report) {
    if (!report.timeline.length) return null;
    return ui.card({title: 'Day by day', subtitle: report.timeline.length + ' active days'}, ui.h('div', {class: 'rep-days'},
      report.timeline.slice().reverse().map((day, index) => ui.h('details', {class: 'rep-day', open: index < 3 || null},
        ui.h('summary', {}, ui.h('strong', {text: day.weekday + ' ' + day.date}),
          ui.h('span', {class: 'muted', text: [plural(day.records, 'record'), day.errors ? plural(day.errors, 'error') : null,
            plural(day.sessions, 'session')].filter(Boolean).join(' · ')})),
        ui.h('ul', {class: 'feed'}, day.entries.map((entry) => ui.h('li', {},
          ui.h('span', {class: 'op ' + (entry.type === 'error' ? 'delete' : 'insert'), 'aria-hidden': 'true'},
            ui.icon(entry.type === 'error' ? 'alert' : 'plus', 'sm')),
          ui.h('div', {class: 'what'}, ui.h('small', {text: entry.type + ' · ' + entry.at.slice(11, 16)}), ui.h('p', {text: entry.title})),
          refs(ui, entry.sources)))),
        day.more ? ui.h('p', {class: 'muted', text: '… and ' + day.more + ' more'}) : null))));
  }

  function contributorsCard(ui, report) {
    if (!report.contributors.length) return null;
    return ui.card({title: 'Contributors', subtitle: 'Who wrote and changed records in this period', flush: true}, ui.table([
      {title: 'Person', render: (c) => ui.person(c.display_name, c.user_id)},
      {title: 'Saves', numeric: true, render: (c) => ui.h('span', {class: 'num', text: number(c.saves)})},
      {title: 'Updates', numeric: true, render: (c) => ui.h('span', {class: 'num', text: number(c.updates)})},
      {title: 'Deletes', numeric: true, render: (c) => ui.h('span', {class: 'num', text: number(c.deletes)})},
      {title: 'Confirms', numeric: true, render: (c) => ui.h('span', {class: 'num', text: number(c.confirms)})},
    ], report.contributors));
  }

  function render(ui, report) {
    const metrics = Object.fromEntries(report.changes.map((m) => [m.key, m]));
    const {values, unit} = series(report);
    const nodes = [ui.h('div', {class: 'kpis'}, KPI_KEYS.map(([key, label, tone]) => ui.kpi({label, value: number(metrics[key].current),
      tone: metrics[key].current ? tone : '', sub: change(ui, metrics[key])})))];
    if (report.llm_summary) nodes.push(ui.card({title: 'Summary', subtitle: 'Written by the server LLM from the sections below'}, ui.h('p', {text: report.llm_summary})));
    if (report.empty) {
      nodes.push(ui.empty({icon: 'history', title: 'No activity in this period',
        text: 'Nothing was saved, changed or logged between ' + report.window.first_day + ' and ' + report.window.last_day + '. Try a longer period.'}));
      return nodes;
    }
    nodes.push(ui.h('div', {class: 'split'},
      ui.card({title: 'Activity', subtitle: 'Records and errors per ' + unit}, ui.bars(values, {height: 72, label: 'Records and errors per ' + unit}),
        ui.h('p', {class: 'chart-caption', text: report.window.first_day + ' → ' + report.window.last_day + ' · ' + report.window.timezone})),
      ui.card({title: 'Compared with the previous period', subtitle: report.previous ? report.previous.label : 'All-time reports have no previous period', flush: true},
        ui.table([
          {title: 'Metric', key: 'label'},
          {title: 'Now', numeric: true, render: (m) => ui.h('span', {class: 'num', text: number(m.current)})},
          report.previous ? {title: 'Before', numeric: true, render: (m) => ui.h('span', {class: 'num', text: number(m.previous)})} : null,
          report.previous ? {title: 'Change', render: (m) => change(ui, m, true)} : null,
        ].filter(Boolean), report.changes))));
    nodes.push(ui.h('div', {class: 'split'},
      listing(ui, 'Key decisions', report.decisions, (item) => recordRow(ui, item, {label: 'Why', text: item.why}), 'No decisions were recorded.'),
      openCard(ui, report)));
    nodes.push(ui.h('div', {class: 'split'},
      listing(ui, 'Solutions and fixes', report.solutions, (item) => recordRow(ui, item, item.detail ? {label: 'Context', text: item.detail} : null), 'No solutions were recorded.'),
      listing(ui, 'Lessons and rules', report.lessons, (item) => recordRow(ui, item, null), 'No lessons were recorded.')));
    nodes.push(errorsCard(ui, report));
    nodes.push(ui.h('div', {class: 'split'},
      countedCard(ui, 'Most touched files', report.files, false, 'No files were referenced.'),
      countedCard(ui, 'Entities and technologies', report.entities, true, 'No entities were linked.')));
    if (report.tags.items.length) {
      nodes.push(ui.card({title: 'Tags'}, ui.h('div', {class: 'record-meta'}, report.tags.items.map((t) => ui.chip(t.name + ' · ' + t.count)))));
    }
    nodes.push(contributorsCard(ui, report), timelineCard(ui, report));
    return nodes.filter(Boolean);
  }

  async function mount(container, ctx) {
    const ui = ctx.ui;
    const options = await ctx.api(API + 'options');
    const state = {scope: 'personal', team_id: null, period: 'week', offset: 0};
    const scopeSelect = ui.h('select', {'aria-label': 'Report scope'},
      ui.h('option', {value: 'personal', text: 'My memory'}),
      options.teams.map((t) => ui.h('option', {value: 'team:' + t.team_id, text: 'Department · ' + t.name})),
      options.company ? ui.h('option', {value: 'company', text: 'Company · all departments'}) : null);
    const periodBar = ui.h('div', {class: 'segmented rep-periods', role: 'tablist', 'aria-label': 'Period'});
    const back = ui.h('button', {type: 'button', class: 'btn sm icon-only', 'aria-label': 'Previous period'}, ui.icon('arrow-down', 'sm'));
    const forward = ui.h('button', {type: 'button', class: 'btn sm icon-only', 'aria-label': 'Next period'}, ui.icon('arrow-up', 'sm'));
    const stepLabel = ui.h('span', {class: 'rep-step muted', 'aria-live': 'polite'});
    const since = ui.h('input', {type: 'date', 'aria-label': 'From'});
    const until = ui.h('input', {type: 'date', 'aria-label': 'To'});
    const project = ui.h('input', {type: 'search', placeholder: 'All projects', 'aria-label': 'Project', maxlength: 128});
    const download = ui.h('a', {class: 'btn sm', href: '#', download: ''}, ui.icon('arrow-down', 'sm'), 'Download .md');
    const generate = ui.h('button', {type: 'submit', class: 'btn primary sm'}, 'Build report');
    const custom = ui.h('div', {class: 'rep-custom', hidden: true}, ui.field('From', since), ui.field('To', until));
    const stepper = ui.h('div', {class: 'rep-stepper'}, back, stepLabel, forward);
    const body = ui.h('div', {class: 'grid'});
    const today = new Date();
    until.value = today.toISOString().slice(0, 10);
    since.value = new Date(today.getTime() - 13 * DAY_MS).toISOString().slice(0, 10);

    const [firstTeam] = options.teams;
    if (!options.teams.length && !options.company) scopeSelect.disabled = true;
    else if (firstTeam && (ctx.user.org_role === 'member')) scopeSelect.value = 'team:' + firstTeam.team_id;

    function params() {
      const scope = scopeSelect.value;
      const query = new URLSearchParams({period: state.period});
      if (scope.startsWith('team:')) { query.set('scope', 'team'); query.set('team_id', scope.slice(5)); }
      else query.set('scope', scope);
      if (STEPPED.has(state.period) && state.offset) query.set('offset', String(state.offset));
      if (state.period === 'custom') { query.set('since', since.value); if (until.value) query.set('until', until.value); }
      if (project.value.trim()) query.set('project', project.value.trim());
      const tz = zone();
      if (tz) query.set('tz', tz);
      return query;
    }

    function syncControls() {
      for (const button of periodBar.querySelectorAll('button')) {
        const on = button.dataset.period === state.period;
        button.setAttribute('aria-selected', String(on));
        button.tabIndex = on ? 0 : -1;
      }
      custom.hidden = state.period !== 'custom';
      stepper.hidden = !STEPPED.has(state.period);
      forward.disabled = state.offset >= 0;
      const current = PERIODS.find(([key]) => key === state.period)[1];
      stepLabel.textContent = state.offset === 0 ? current : state.offset === -1 ? PREVIOUS[state.period] : Math.abs(state.offset) + ' ' + state.period + 's ago';
      download.href = API + 'report.md?' + params().toString();
    }

    function build() {
      syncControls();
      if (state.period === 'custom' && !since.value) { ui.toast('Choose a start date', 'bad'); return; }
      ui.load(body, async () => render(ui, (await ctx.api(API + 'report?' + params().toString())).report));
    }

    for (const [key, label] of PERIODS) {
      periodBar.append(ui.h('button', {type: 'button', role: 'tab', 'data-period': key, onclick: () => {
        state.period = key; state.offset = 0; build();
      }}, label));
    }
    back.addEventListener('click', () => { state.offset -= 1; build(); });
    forward.addEventListener('click', () => { state.offset = Math.min(0, state.offset + 1); build(); });
    scopeSelect.addEventListener('change', build);
    for (const input of [since, until, project]) input.addEventListener('change', syncControls);
    const form = ui.h('form', {class: 'rep-controls', onsubmit: (event) => { event.preventDefault(); build(); }},
      ui.field('Scope', scopeSelect), ui.field('Project', project), ui.h('div', {class: 'rep-period'}, periodBar, stepper), custom,
      ui.h('div', {class: 'actions'}, generate, download));
    container.append(ui.h('div', {class: 'hero'}, ui.h('p', {text: options.company || options.teams.length
      ? 'What happened in your memory, your department or the company: decisions, fixes, errors, open work.'
      : 'What happened in your memory: decisions, fixes, errors and open work. Only you can see your personal report.'})),
    ui.card({className: 'rep-toolbar'}, form), body);
    build();
  }

  window.TamReports = {mount};
})();
