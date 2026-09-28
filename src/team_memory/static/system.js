'use strict';
window.TamSections.system = async (container, ctx) => {
  const {h, ui} = ctx;
  const [backup, metrics, organization] = await Promise.all([ctx.api('admin/backup'), ctx.api('admin/metrics'),
    ctx.api('admin/organization')]);
  const companyName = h('input', {value: organization.name || '', maxlength: '128', autocomplete: 'organization'});
  const publicUrl = h('input', {type: 'url', inputmode: 'url', value: organization.public_url || '', spellcheck: 'false', placeholder: 'https://memory.example.com'});
  const saveOrganization = h('button', {type: 'button', class: 'btn primary', onclick: () => ctx.busy(saveOrganization, async () => {
    const body = {};
    if (companyName.value.trim() && companyName.value.trim() !== (organization.name || '')) body.name = companyName.value.trim();
    if (publicUrl.value.trim() && publicUrl.value.trim() !== (organization.public_url || '')) body.public_url = publicUrl.value.trim();
    if (!Object.keys(body).length) { ui.toast('Nothing to save', 'info'); return; }
    await ctx.api('admin/organization', {method: 'POST', body});
    ui.toast('Organization saved', 'good');
    await ctx.refresh();
  })}, 'Save');
  const counters = metrics.gateway.counters.filter((c) => c.name !== 'http_requests_total');
  const requests = metrics.gateway.counters.filter((c) => c.name === 'http_requests_total');
  container.append(
    ui.card({title: 'Organization', subtitle: 'The name appears in the dashboard header; the public address goes into client connection instructions.'},
      h('div', {class: 'form-grid top'}, ui.field('Company name', companyName), ui.field('Public address', publicUrl, 'Where people reach this server.')),
      h('div', {class: 'actions'}, saveOrganization)),
    ui.card({title: 'Backup', subtitle: backup.reason},
      h('pre', {class: 'cmd', text: backup.command}), h('div', {class: 'actions'}, ui.copyButton(backup.command)),
      h('p', {class: 'muted', text: 'Keep the master key (master.key or TAM_TEAM_MASTER_KEY) somewhere separate: backups hold provider keys only in encrypted form.'})),
    h('div', {class: 'split'},
      ui.card({title: 'Events since start', flush: true}, ui.table([
        {title: 'Event', render: (c) => h('span', {class: 'mono', text: c.name})},
        {title: 'Labels', render: (c) => h('div', {class: 'record-meta'}, Object.entries(c.labels).map(([k, v]) => ui.chip(k + '=' + v)))},
        {title: 'Count', numeric: true, render: (c) => h('span', {class: 'num', text: c.value})},
      ], counters, ui.empty({icon: 'pulse', text: 'No sign-ins or admin actions since the server started.'}))),
      ui.card({title: 'Memory tool calls', flush: true}, ui.table([
        {title: 'Tool', render: (c) => h('span', {class: 'mono', text: c.tool})},
        {title: 'Status', render: (c) => ui.chip(c.status, c.status === 'ok' ? 'good' : c.status === 'error' ? 'bad' : 'warn')},
        {title: 'Count', numeric: true, render: (c) => h('span', {class: 'num', text: c.value})},
      ], metrics.memory_calls, ui.empty({icon: 'brain', text: 'No memory calls since the server started.'})))),
    ui.card({title: 'HTTP requests', flush: true}, ui.table([
      {title: 'Route', render: (c) => h('span', {class: 'mono', text: c.labels.route})},
      {title: 'Method', render: (c) => c.labels.method},
      {title: 'Status', render: (c) => ui.chip(c.labels.status, c.labels.status.startsWith('2') ? 'good' : c.labels.status.startsWith('5') ? 'bad' : 'warn')},
      {title: 'Count', numeric: true, render: (c) => h('span', {class: 'num', text: c.value})},
    ], requests, 'No requests yet.')));
};
