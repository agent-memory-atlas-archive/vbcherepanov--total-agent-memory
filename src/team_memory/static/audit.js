'use strict';
window.TamSections.audit = async (container, ctx) => {
  const {h, ui} = ctx;
  const PAGE = 50;
  const actor = h('input', {placeholder: 'e.g. admin or cli', 'aria-label': 'Actor', autocomplete: 'off'});
  const action = h('select', {'aria-label': 'Action'}, h('option', {value: ''}, 'All actions'));
  const subject = h('input', {type: 'search', placeholder: 'User, team or setting', 'aria-label': 'Subject'});
  const body = h('tbody');
  const wrap = h('div');
  const older = h('button', {type: 'button', class: 'btn', hidden: true}, 'Load older events');
  let next = null;
  let actionsLoaded = false;

  function tone(name) {
    if (/disabled|revoked|deleted|locked|cleared/.test(name)) return 'bad';
    if (/created|enabled|redeemed/.test(name)) return 'good';
    if (/role|membership|setting/.test(name)) return 'violet';
    return '';
  }

  async function load(first) {
    if (first) { next = null; body.replaceChildren(); wrap.replaceChildren(ui.skeleton(6)); }
    const params = new URLSearchParams({limit: String(PAGE)});
    if (next) params.set('before', String(next));
    if (actor.value.trim()) params.set('actor', actor.value.trim());
    if (action.value) params.set('action', action.value);
    if (subject.value.trim()) params.set('subject', subject.value.trim());
    const page = await ctx.api('admin/audit?' + params.toString());
    if (!actionsLoaded) {
      action.append(...page.actions.map((a) => h('option', {value: a}, a)));
      actionsLoaded = true;
    }
    for (const event of page.events) {
      body.append(h('tr', {}, h('td', {class: 'lead'}, ui.chip(event.action, tone(event.action))),
        h('td', {'data-label': 'When'}, ui.time(event.at)),
        h('td', {'data-label': 'Actor'}, event.actor ? ui.person(event.actor, event.actor) : h('span', {class: 'muted', text: '—'})),
        h('td', {class: 'mono', 'data-label': 'Subject', text: event.subject}), h('td', {class: 'muted', 'data-label': 'Detail', text: event.detail || ''})));
    }
    next = page.next;
    older.hidden = !next;
    if (first) {
      wrap.replaceChildren(body.children.length ? h('div', {class: 'table-wrap stack'}, h('table', {},
        h('thead', {}, h('tr', {}, ['Action', 'When', 'Actor', 'Subject', 'Detail'].map((t) => h('th', {scope: 'col', text: t})))), body))
        : ui.empty({icon: 'list', title: 'No events match', text: 'Change or clear the filters.'}));
    }
  }

  let timer = null;
  const refilter = () => { clearTimeout(timer); timer = setTimeout(() => ctx.busy(null, () => load(true)), 250); };
  actor.addEventListener('input', refilter);
  subject.addEventListener('input', refilter);
  action.addEventListener('change', refilter);
  older.addEventListener('click', () => ctx.busy(older, () => load(false)));
  container.append(ui.card({title: 'Audit log', subtitle: 'Every administrative change, newest first. Secret values are never logged.', flush: true},
    h('div', {class: 'filters card-head'}, ui.field('Actor', actor), ui.field('Action', action), h('label', {class: 'field'}, h('span', {text: 'Subject'}), subject)),
    wrap, h('div', {class: 'card-head'}, older)));
  await load(true);
};
