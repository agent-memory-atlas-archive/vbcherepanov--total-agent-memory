'use strict';
window.TamSections.company = async (container, ctx) => {
  const {h, ui} = ctx;
  const data = await ctx.api('overview/company');
  const detail = h('div');
  const number = (value) => new Intl.NumberFormat('en').format(value);
  container.append(
    h('p', {class: 'muted', text: 'Read-only view of every department. Personal memory is never included.'}),
    ui.card({title: 'Departments', subtitle: data.departments.length + ' departments · ' + number(data.records) + ' records', flush: true}, ui.table([
      {title: 'Department', render: (d) => h('div', {class: 'person'}, h('div', {}, h('strong', {text: d.name}), h('small', {text: d.team_id})))},
      {title: 'Members', numeric: true, render: (d) => h('span', {class: 'num', text: number(d.members)})},
      {title: 'Records', numeric: true, render: (d) => h('span', {class: 'num', text: number(d.records)})},
      {title: 'Saves · 30 d', numeric: true, render: (d) => h('span', {class: 'num', text: number(d.saves_30d)})},
      {title: 'Trend', className: 'spark-cell', render: (d) => ui.sparkline(d.trend_30d, {height: 26, tone: 'violet', label: d.name + ' saves per day'})},
      {title: 'Last activity', render: (d) => ui.time(d.last_activity)},
      {title: '', render: (d) => h('div', {class: 'row-actions'}, h('button', {type: 'button', class: 'btn sm', onclick: () => {
        detail.scrollIntoView({behavior: 'smooth', block: 'start'});
        ui.load(detail, async () => {
          const people = await ctx.api('teams/' + encodeURIComponent(d.team_id) + '/people');
          return ui.card({title: people.name, subtitle: 'People and activity', flush: true}, ui.table([
            {title: 'Member', render: (m) => ui.person(m.name, m.user_id)},
            {title: 'Role', render: (m) => ui.chip(m.role)},
            {title: 'Saved', numeric: true, render: (m) => h('span', {class: 'num', text: m.saves})},
            {title: 'Edits', numeric: true, render: (m) => h('span', {class: 'num', text: m.changes})},
            {title: 'Last activity', render: (m) => ui.time(m.last_activity)},
          ], people.members, 'No members.'));
        });
      }}, 'People'))},
    ], data.departments, ui.empty({icon: 'building', title: 'No departments yet', text: 'A superadmin creates departments under Administration.'}))),
    detail);
};
