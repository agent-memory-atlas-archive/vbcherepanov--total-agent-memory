'use strict';
window.TamSections.access = async (container, ctx) => {
  const {h, ui} = ctx;
  const list = h('div');

  async function reload() {
    await ui.load(list, async () => {
      const tokens = await ctx.api('tokens');
      return ui.table([
        {title: 'Client', render: (t) => h('strong', {text: t.client})},
        {title: 'Created', render: (t) => ui.time(t.created_at, 'unknown')},
        {title: 'Status', render: (t) => ui.chip(t.revoked ? 'Revoked' : 'Active', t.revoked ? '' : 'good', {dot: true})},
        {title: '', render: (t) => t.revoked ? '' : h('div', {class: 'row-actions'}, h('button', {type: 'button', class: 'btn danger sm',
          onclick: async (event) => {
            if (!await ui.confirm('Revoke token?', 'Clients using the "' + t.client + '" token stop working immediately. This cannot be undone.', 'Revoke token')) return;
            await ctx.busy(event.target, async () => {
              await ctx.api('tokens/revoke', {method: 'POST', body: {id: t.id}});
              await reload();
            }, 'Token revoked');
          }}, 'Revoke'))},
      ], tokens, ui.empty({icon: 'key', title: 'No tokens yet',
        text: 'Create a token for each MCP client (Claude Code, Codex, Cursor…) so it can use your memory.'}));
    });
  }

  const client = h('input', {required: true, placeholder: 'claude-code-laptop', maxlength: '128'});
  const create = h('button', {type: 'submit', class: 'btn primary'}, ui.icon('plus', 'sm'), 'Create token');
  container.append(ui.card({title: 'Personal access tokens', subtitle: 'Tokens let MCP clients act as you. Name them after the device or tool.'},
    h('form', {class: 'toolbar', onsubmit: (event) => {
      event.preventDefault();
      ctx.busy(create, async () => {
        const reply = await ctx.api('tokens', {method: 'POST', body: {client: client.value.trim()}});
        client.value = '';
        await reload();
        await ui.showSecret('Token for ' + reply.client, reply.token,
          'Shown once. Paste it into your MCP client configuration now; the server keeps only its hash.');
      });
    }}, ui.field('Client name', client), h('div', {class: 'actions'}, create)), list));

  const current = h('input', {type: 'password', autocomplete: 'current-password', required: true});
  const next = h('input', {type: 'password', autocomplete: 'new-password', minlength: '12', required: true});
  const repeat = h('input', {type: 'password', autocomplete: 'new-password', minlength: '12', required: true});
  const change = h('button', {type: 'submit', class: 'btn primary'}, 'Change password');
  const passwordBody = ctx.user.password_set
    ? h('form', {class: 'grid', onsubmit: (event) => {
      event.preventDefault();
      ctx.busy(change, async () => {
        if (next.value !== repeat.value) throw new Error('New passwords do not match');
        await ctx.api('password', {method: 'POST', body: {current: current.value, new: next.value}});
        event.target.reset();
      }, 'Password changed; your other sessions were signed out');
    }}, h('div', {class: 'form-grid'}, ui.field('Current password', current), ui.field('New password', next, 'At least 12 characters'),
      ui.field('Repeat new password', repeat)), h('div', {class: 'actions'}, change))
    : ui.empty({icon: 'key', title: 'No password set', text: 'You signed in with a token. Ask a superadmin for an invite code to set a password.'});
  container.append(ui.card({title: 'Password', subtitle: 'Forgot it? A superadmin can issue a new invite code.'}, passwordBody));
  await reload();
};
