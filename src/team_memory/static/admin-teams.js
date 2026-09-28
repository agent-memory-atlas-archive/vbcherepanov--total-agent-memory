'use strict';
window.TamSections.admin_teams = async (container, ctx) => {
  const {h, ui} = ctx;
  const ROLES = {reader: 'Reader', editor: 'Editor', manager: 'Manager'};
  const ROLE_TONE = {manager: 'accent', editor: 'violet', reader: ''};
  const post = (path, body) => ctx.api(path, {method: 'POST', body});
  const enc = encodeURIComponent;
  const list = h('div', {class: 'grid'});
  let people = [];

  const options = (selected) => Object.entries(ROLES).map(([value, label]) => h('option', {value, selected: value === selected}, label));
  const membership = (userId, teamId, role) => post('admin/membership', {user_id: userId, team_id: teamId, role});

  async function addMember(team) {
    const candidates = people.filter((u) => u.active && !team.members.some((m) => m.user_id === u.id));
    if (!candidates.length) { ui.toast('Every active user is already in ' + team.name, 'info'); return; }
    const user = h('select', {}, candidates.map((u) => h('option', {value: u.id}, u.name + ' (' + u.id + ')')));
    const role = h('select', {}, options('editor'));
    const ok = await ui.modal({title: 'Add member to ' + team.name, confirm: 'Add member', body: [ui.field('User', user),
      ui.field('Role', role, 'Managers read and write department memory and see member activity and learning results.')]});
    if (ok) await ctx.busy(null, async () => { await membership(user.value, team.team_id, role.value); await reload(); }, 'Member added');
  }

  async function rename(team) {
    const name = h('input', {value: team.name, maxlength: '128', required: true});
    if (!await ui.modal({title: 'Rename ' + team.team_id, confirm: 'Rename', body: [ui.field('Name', name, 'The ID stays the same.')]})) return;
    await ctx.busy(null, async () => { await post('admin/teams/' + enc(team.team_id) + '/rename', {name: name.value.trim()}); await reload(); }, 'Department renamed');
  }

  async function remove(team) {
    if (!await ui.confirm('Delete ' + team.name + '?', 'Only departments with no members and no stored memory can be deleted. This cannot be undone.', 'Delete department')) return;
    await ctx.busy(null, async () => { await post('admin/teams/' + enc(team.team_id) + '/delete', {}); await reload(); }, 'Department deleted');
  }

  function teamCard(team) {
    const members = ui.table([
      {title: 'Member', render: (m) => ui.person(m.name, m.user_id)},
      {title: 'Role', render: (m) => {
        const select = h('select', {'aria-label': 'Role of ' + m.user_id}, options(m.role));
        select.addEventListener('change', () => ctx.busy(select, async () => { await membership(m.user_id, team.team_id, select.value); await reload(); }, 'Role updated'));
        return select;
      }},
      {title: '', render: (m) => h('div', {class: 'row-actions'}, h('button', {type: 'button', class: 'btn ghost sm', 'aria-label': 'Remove ' + m.user_id,
        onclick: async () => {
          if (!await ui.confirm('Remove ' + m.name + '?', 'They lose access to ' + team.name + ' memory immediately.', 'Remove member')) return;
          await ctx.busy(null, async () => { await membership(m.user_id, team.team_id, null); await reload(); }, 'Member removed');
        }}, ui.icon('x', 'sm'), 'Remove'))},
    ], team.members, ui.empty({icon: 'users', title: 'No members yet', text: 'Add the first person to this department.',
      action: {label: 'Add member', onClick: () => addMember(team)}}));
    return ui.card({title: team.name, subtitle: team.team_id + ' · ' + team.members.length + ' members', flush: true, actions: [
      h('button', {type: 'button', class: 'btn sm', onclick: () => addMember(team)}, ui.icon('plus', 'sm'), 'Add member'),
      ui.menu([{label: 'Rename…', icon: 'edit', onClick: () => rename(team)}, 'separator',
        {label: 'Delete department…', icon: 'x', danger: true, onClick: () => remove(team)}], 'Actions for ' + team.team_id)]}, members);
  }

  async function reload() {
    await ui.load(list, async () => {
      const [teams, users] = await Promise.all([ctx.api('admin/teams'), ctx.api('admin/users')]);
      people = users;
      return teams.length ? teams.map(teamCard) : ui.empty({icon: 'layers', title: 'No departments yet',
        text: 'Create a department, then add people with a role.', action: {label: 'New department', onClick: create}});
    });
  }

  async function create() {
    const id = h('input', {required: true, pattern: '[A-Za-z0-9_-]{1,64}', placeholder: 'sales', autocomplete: 'off'});
    const name = h('input', {required: true, maxlength: '128', placeholder: 'Sales', autocomplete: 'off'});
    if (!await ui.modal({title: 'New department', confirm: 'Create department', body: [ui.field('Department ID', id, 'Used by MCP clients as team_id; cannot change later.'), ui.field('Name', name)]})) return;
    await ctx.busy(null, async () => { await post('admin/teams', {id: id.value.trim(), name: name.value.trim()}); await reload(); }, 'Department created');
  }

  container.append(h('div', {class: 'hero'}, h('p', {class: 'muted', text: 'Departments own shared team memory. Roles: reader, editor, manager.'}),
    h('button', {type: 'button', class: 'btn primary sm', onclick: create}, ui.icon('plus', 'sm'), 'New department')), list);
  await reload();
};
