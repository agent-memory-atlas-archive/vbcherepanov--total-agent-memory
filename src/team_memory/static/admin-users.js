'use strict';
window.TamSections.admin_users = async (container, ctx) => {
  const {h, ui} = ctx;
  const ORG = {member: 'Member', company_viewer: 'Company viewer', superadmin: 'Superadmin'};
  const ORG_TONE = {member: '', company_viewer: 'violet', superadmin: 'accent'};
  const post = (path, body) => ctx.api(path, {method: 'POST', body});
  const enc = encodeURIComponent;
  let users = [];
  const search = h('input', {type: 'search', placeholder: 'Search name or ID', 'aria-label': 'Search users'});
  const role = h('select', {'aria-label': 'Filter by role'}, h('option', {value: ''}, 'All roles'),
    Object.entries(ORG).map(([value, label]) => h('option', {value}, label)));
  const team = h('select', {'aria-label': 'Filter by department'}, h('option', {value: ''}, 'All departments'));
  const status = h('select', {'aria-label': 'Filter by status'}, h('option', {value: ''}, 'Any status'),
    h('option', {value: 'active'}, 'Active'), h('option', {value: 'disabled'}, 'Disabled'), h('option', {value: 'pending'}, 'No password yet'));
  const body = h('div');
  const count = h('span', {class: 'muted'});

  function showInvite(invite, verb) {
    return ui.showSecret(verb + ' for ' + invite.user_id, invite.code,
      'Single use. Share it privately; the person opens the dashboard, picks "Invite code" and sets a password.',
      h('p', {class: 'record-meta'}, ui.icon('clock', 'sm'), 'Expires ', ui.time(invite.expires_at), ' · ' + new Date(invite.expires_at).toLocaleString()));
  }

  async function changeRole(user) {
    const select = h('select', {}, Object.entries(ORG).map(([value, label]) => h('option', {value, selected: value === user.org_role}, label)));
    const ok = await ui.modal({title: 'Organisation role for ' + user.name, confirm: 'Save role', body: [ui.field('Role', select,
      'Company viewers read every department; superadmins manage everything.')]});
    if (!ok || select.value === user.org_role) return;
    await ctx.busy(null, async () => { await post('admin/users/' + enc(user.id) + '/role', {org_role: select.value}); await reload(); }, 'Role updated');
  }

  async function invite(user) {
    const text = user.password_set ? 'Their current password keeps working until they redeem the new code. Earlier unused codes stop working.'
      : 'Earlier unused codes for this user stop working.';
    if (!await ui.confirm(user.password_set ? 'Reset password?' : 'Issue invite code?', text, 'Issue code', 'primary')) return;
    const code = await ctx.busy(null, () => post('admin/users/' + enc(user.id) + '/invite', {}));
    if (code) await showInvite(code, user.password_set ? 'Reset code' : 'Invite code');
  }

  async function toggle(user) {
    const disabling = user.active;
    if (disabling && !await ui.confirm('Disable ' + user.name + '?', 'Their tokens and dashboard sessions stop working immediately. You can enable the account again later.', 'Disable user')) return;
    await ctx.busy(null, async () => { await post('admin/users/' + enc(user.id) + '/active', {active: !user.active}); await reload(); },
      disabling ? 'User disabled' : 'User enabled');
  }

  function render() {
    const q = search.value.trim().toLowerCase();
    const rows = users.filter((u) => (!q || u.name.toLowerCase().includes(q) || u.id.toLowerCase().includes(q))
      && (!role.value || u.org_role === role.value) && (!team.value || u.teams.some((t) => t.team_id === team.value))
      && (!status.value || (status.value === 'active' ? u.active : status.value === 'disabled' ? !u.active : u.active && !u.password_set)));
    count.textContent = rows.length + ' of ' + users.length;
    body.replaceChildren(ui.table([
      {title: 'User', render: (u) => ui.person(u.name, u.id)},
      {title: 'Role', render: (u) => ui.chip(ORG[u.org_role], ORG_TONE[u.org_role])},
      {title: 'Departments', render: (u) => u.teams.length ? h('div', {class: 'record-meta'}, u.teams.map((t) => ui.chip(t.team_id + ' · ' + t.role))) : h('span', {class: 'muted', text: '—'})},
      {title: 'Last sign-in', render: (u) => ui.time(u.last_login_at)},
      {title: 'Status', render: (u) => !u.active ? ui.chip('Disabled', 'bad', {dot: true})
        : u.password_set ? ui.chip('Active', 'good', {dot: true}) : ui.chip('Invite pending', 'warn', {dot: true})},
      {title: '', render: (u) => h('div', {class: 'row-actions'}, ui.menu([
        {label: 'Change role…', icon: 'user-cog', onClick: () => changeRole(u)},
        {label: u.password_set ? 'Reset password…' : 'Issue invite code…', icon: 'key', onClick: () => invite(u)},
        'separator',
        {label: u.active ? 'Disable user…' : 'Enable user', icon: u.active ? 'x' : 'check', danger: u.active, onClick: () => toggle(u)},
      ], 'Actions for ' + u.id))},
    ], rows, ui.empty({icon: 'search', title: 'No users match', text: 'Clear the filters or search for another name.',
      action: {label: 'Clear filters', onClick: () => { search.value = ''; role.value = ''; team.value = ''; status.value = ''; render(); }}})));
  }

  async function reload() {
    users = await ctx.api("admin/users");
    const teams = [...new Set(users.flatMap((u) => u.teams.map((t) => t.team_id)))].sort();
    const selected = team.value;
    team.replaceChildren(h('option', {value: ''}, 'All departments'), teams.map((t) => h('option', {value: t, selected: t === selected}, t)));
    render();
  }

  async function create() {
    const id = h('input', {required: true, pattern: '[A-Za-z0-9_-]{1,64}', placeholder: 'ivan', autocomplete: 'off'});
    const name = h('input', {required: true, maxlength: '128', placeholder: 'Ivan Petrov', autocomplete: 'off'});
    const orgRole = h('select', {}, Object.entries(ORG).map(([value, label]) => h('option', {value}, label)));
    const ok = await ui.modal({title: 'New user', confirm: 'Create and get invite code', body: [
      ui.field('User ID', id, 'Letters, digits, - and _. Used to sign in.'), ui.field('Full name', name), ui.field('Organisation role', orgRole)]});
    if (!ok) return;
    const code = await ctx.busy(null, () => post('admin/users', {id: id.value.trim(), name: name.value.trim(), org_role: orgRole.value}));
    if (code) { await reload(); await showInvite(code, 'Invite code'); }
  }

  for (const control of [role, team, status]) control.addEventListener('change', render);
  search.addEventListener('input', render);
  container.append(ui.card({title: 'People', flush: true, actions: [count,
    h('button', {type: 'button', class: 'btn primary sm', onclick: create}, ui.icon('plus', 'sm'), 'New user')]},
  h('div', {class: 'filters card-head'}, h('div', {class: 'field search'}, ui.icon('search', 'sm'), search),
    h('div', {class: 'field'}, role), h('div', {class: 'field'}, team), h('div', {class: 'field'}, status)), body));
  body.append(ui.skeleton(5));
  await reload();
};
