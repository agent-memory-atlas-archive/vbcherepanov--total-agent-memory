'use strict';
window.TamSections.admin_tokens = async (container, ctx) => {
  const {h, ui} = ctx;
  const users = await ctx.api('admin/users');
  const names = Object.fromEntries(users.map((u) => [u.id, u.name]));
  const filter = h('select', {'aria-label': 'Filter by user'}, h('option', {value: ''}, 'All users'),
    users.map((u) => h('option', {value: u.id}, u.name + ' (' + u.id + ')')));
  const state = h('select', {'aria-label': 'Filter by status'}, h('option', {value: 'active'}, 'Active only'), h('option', {value: ''}, 'Active and revoked'));
  const list = h('div');

  async function load() {
    await ui.load(list, async () => {
      const rows = (await ctx.api('admin/tokens' + (filter.value ? '?user_id=' + encodeURIComponent(filter.value) : '')))
        .filter((t) => !state.value || !t.revoked);
      return ui.table([
        {title: 'Owner', render: (t) => ui.person(names[t.user_id] || t.user_id, t.user_id)},
        {title: 'Client', render: (t) => h('strong', {text: t.client})},
        {title: 'Created', render: (t) => ui.time(t.created_at, 'unknown')},
        {title: 'Status', render: (t) => ui.chip(t.revoked ? 'Revoked' : 'Active', t.revoked ? '' : 'good', {dot: true})},
        {title: '', render: (t) => t.revoked ? '' : h('div', {class: 'row-actions'}, h('button', {type: 'button', class: 'btn danger sm', onclick: async () => {
          if (!await ui.confirm('Revoke this token?', (names[t.user_id] || t.user_id) + "'s " + t.client + ' client stops working immediately.', 'Revoke token')) return;
          await ctx.busy(null, async () => { await ctx.api('admin/tokens/revoke', {method: 'POST', body: {id: t.id}}); await load(); }, 'Token revoked');
        }}, 'Revoke'))},
      ], rows, ui.empty({icon: 'shield', title: 'No tokens match', text: 'People create tokens themselves under Tokens & password.'}));
    });
  }

  filter.addEventListener('change', load);
  state.addEventListener('change', load);
  container.append(ui.card({title: 'Access tokens', subtitle: 'Personal tokens used by MCP clients. Only hashes are stored.', flush: true},
    h('div', {class: 'filters card-head'}, h('div', {class: 'field'}, filter), h('div', {class: 'field'}, state)), list));
  await load();
};
