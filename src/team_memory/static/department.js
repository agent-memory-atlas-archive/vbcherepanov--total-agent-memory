'use strict';
window.TamSections.department = async (container, ctx) => {
  const {h, ui} = ctx;
  const teams = ctx.viewableTeams;
  if (!teams.length) {
    container.append(ui.empty({icon: 'users', title: 'No departments available', text: 'You manage no department yet.'}));
    return;
  }
  const ROLE_TONE = {manager: 'accent', editor: 'violet', reader: ''};
  const picker = h('select', {'aria-label': 'Department'}, teams.map((t) => h('option', {value: t.team_id}, t.name)));
  const people = h('div');
  const memory = h('div', {class: 'grid'});

  async function show() {
    const teamId = picker.value;
    const own = ctx.teams.find((t) => t.team_id === teamId);
    await ui.load(people, async () => {
      const data = await ctx.api('teams/' + encodeURIComponent(teamId) + '/people');
      return ui.card({title: 'People', subtitle: data.members.length + ' members', flush: true}, ui.table([
        {title: 'Member', render: (m) => ui.person(m.name, m.user_id)},
        {title: 'Role', render: (m) => ui.chip(m.role, ROLE_TONE[m.role])},
        {title: 'Saved', numeric: true, render: (m) => h('span', {class: 'num', text: m.saves})},
        {title: 'Edits', numeric: true, render: (m) => h('span', {class: 'num', text: m.changes})},
        {title: 'Last activity', render: (m) => ui.time(m.last_activity)},
      ], data.members, ui.empty({icon: 'users', title: 'No members yet', text: 'A superadmin adds people under Administration → Departments.'})));
    });
    memory.replaceChildren();
    window.TamDashboard.recordBrowser(memory, ctx, {
      title: 'Department memory',
      scopes: [{scope: {kind: 'team', team_id: teamId}, writable: Boolean(own && own.role !== 'reader')}],
      teamNames: {[teamId]: picker.selectedOptions[0].textContent}});
  }

  picker.addEventListener('change', show);
  container.append(teams.length > 1 ? h('div', {class: 'toolbar'}, ui.field('Department', picker)) : null, people, memory);
  await show();
};
