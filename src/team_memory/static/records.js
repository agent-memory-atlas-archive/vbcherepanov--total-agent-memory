'use strict';
(() => {
  const PAGE = 50;
  const {h, busy, ui} = window.TamDashboard;
  const OPS = {insert: 'Saved', update: 'Edited', delete: 'Deleted', confirm: 'Confirmed'};

  function scopeLabel(scope, teamNames = {}) {
    if (scope.kind === 'personal') return 'Personal';
    if (scope.kind === 'shared') return 'Shared';
    return teamNames[scope.team_id] || scope.team_id;
  }

  function scopeChip(scope, teamNames) {
    return ui.chip(scopeLabel(scope, teamNames), scope.kind === 'personal' ? 'accent' : scope.kind === 'team' ? 'violet' : '');
  }

  function recordBrowser(container, ctx, {scopes, allowAll = false, teamNames = {}, title = 'Search & browse'}) {
    const call = (name, args) => ctx.api('memory', {method: 'POST', body: {name, arguments: args}});
    const key = (scope) => JSON.stringify(scope);
    const writable = new Set(scopes.filter((s) => s.writable).map((s) => key(s.scope)));
    const results = h('div', {class: 'results'});
    const searchScope = h('select', {'aria-label': 'Workspace'},
      allowAll ? h('option', {value: ''}, 'All my workspaces') : null,
      scopes.map((s) => h('option', {value: key(s.scope)}, scopeLabel(s.scope, teamNames))));
    const query = h('input', {type: 'search', placeholder: 'Search by meaning, e.g. "deploy schedule"', 'aria-label': 'Search query'});
    const more = h('button', {type: 'button', class: 'btn', hidden: true}, 'Load more');
    let cursor = 0;

    function card(scope, record) {
      const article = h('article', {class: 'record'},
        h('div', {class: 'record-meta'}, scopeChip(scope, teamNames), h('span', {class: 'mono', text: '#' + record.id}),
          h('span', {text: 'rev ' + record.revision}),
          record.status && record.status !== 'active' ? ui.chip(record.status, 'warn') : null,
          record.project && record.project !== 'general' ? h('span', {text: '· ' + record.project}) : null),
        h('pre', {text: record.content}),
        h('div', {class: 'record-meta'}, ui.avatar(record.created_by.display_name, record.created_by.user_id, 'sm'),
          h('span', {text: record.created_by.display_name}),
          record.updated_by.user_id !== record.created_by.user_id ? h('span', {text: '· edited by ' + record.updated_by.display_name}) : null));
      const actions = h('div', {class: 'actions'});
      let after = 0;
      const history = h('button', {type: 'button', class: 'btn sm', onclick: () => busy(history, async () => {
        const reply = await call('memory_history', {scope, id: record.id, after, limit: PAGE});
        for (const event of reply.data) {
          article.insertBefore(h('div', {class: 'event'},
            h('div', {class: 'record-meta'}, h('strong', {text: OPS[event.operation] || event.operation}),
              h('span', {text: event.actor.display_name}), ui.time(event.at)),
            event.reason ? h('span', {class: 'dim', text: '“' + event.reason + '”'}) : null,
            event.after_state ? h('pre', {text: event.after_state.content}) : h('span', {class: 'muted', text: 'Record removed'})), actions);
        }
        after = reply.data.length ? reply.data[reply.data.length - 1].sequence : after;
        history.replaceChildren(ui.icon('history', 'sm'), 'More history');
        history.hidden = reply.data.length < PAGE;
      })}, ui.icon('history', 'sm'), 'History');
      actions.append(history);
      if (writable.has(key(scope)) && record.status === 'active') {
        const edit = h('button', {type: 'button', class: 'btn sm', onclick: () => {
          edit.hidden = true;
          const text = h('textarea', {'aria-label': 'New text', value: record.content});
          const reason = h('input', {placeholder: 'Why is it changing?', 'aria-label': 'Reason for the change', required: true});
          const submit = h('button', {type: 'button', class: 'btn primary sm', onclick: () => busy(submit, async () => {
            if (!reason.value.trim()) throw new Error('Add a short reason for the change');
            const reply = await call('memory_update', {scope, id: record.id, expected_revision: record.revision,
              content: text.value, reason: reason.value});
            article.replaceWith(card(scope, reply.data));
          }, 'Saved as a new revision')}, 'Save revision');
          const cancel = h('button', {type: 'button', class: 'btn ghost sm', onclick: () => { form.remove(); edit.hidden = false; }}, 'Cancel');
          const form = h('div', {class: 'grid'}, text, reason, h('div', {class: 'actions'}, submit, cancel));
          article.insertBefore(form, actions);
          text.focus();
        }}, ui.icon('edit', 'sm'), 'Edit');
        actions.append(edit);
      }
      article.append(actions);
      return article;
    }

    function show(items, append = false) {
      if (!append) results.replaceChildren();
      if (!items.length && !append) {
        results.append(ui.empty({icon: 'search', title: 'No records found',
          text: 'Try different words, pick another workspace, or save the first record below.'}));
      }
      for (const item of items) results.append(card(item.scope, item.record));
    }

    async function browse(first) {
      if (!searchScope.value) throw new Error('Choose one workspace to browse');
      const scope = JSON.parse(searchScope.value);
      if (first) { cursor = 0; results.replaceChildren(ui.skeleton(4)); }
      const reply = await call('memory_export', {scope, after: cursor, limit: PAGE});
      show(reply.data.map((record) => ({scope, record})), !first);
      cursor = reply.data.length ? reply.data[reply.data.length - 1].id : cursor;
      more.hidden = reply.data.length < PAGE;
    }

    const searchButton = h('button', {type: 'submit', class: 'btn primary'}, ui.icon('search', 'sm'), 'Search');
    const browseButton = h('button', {type: 'button', class: 'btn', onclick: () => busy(browseButton, () => browse(true))}, 'Browse all');
    more.addEventListener('click', () => busy(more, () => browse(false)));
    const form = h('form', {class: 'grid', onsubmit: (event) => {
      event.preventDefault();
      if (!query.value.trim()) { ui.toast('Type what you are looking for', 'info'); return; }
      busy(searchButton, async () => {
        results.replaceChildren(ui.skeleton(4));
        const args = {query: query.value, limit: 20};
        if (searchScope.value) args.scope = JSON.parse(searchScope.value);
        const reply = await call('memory_recall', args);
        more.hidden = true;
        show(reply.results);
      });
    }}, h('div', {class: 'toolbar'}, h('div', {class: 'field search'}, ui.icon('search', 'sm'), query),
      h('div', {class: 'field'}, searchScope)), h('div', {class: 'actions'}, searchButton, browseButton));
    results.append(ui.empty({icon: 'brain', title: 'Search or browse',
      text: 'Search finds records by meaning across the workspaces you can read. Browse lists one workspace page by page.'}));
    container.append(ui.card({title}, form, results, more));

    const targets = scopes.filter((s) => s.writable);
    if (targets.length) {
      const writeScope = h('select', {}, targets.map((s) => h('option', {value: key(s.scope)}, scopeLabel(s.scope, teamNames))));
      const content = h('textarea', {required: true, placeholder: 'A decision, a fix, a convention… one idea per record works best.'});
      const project = h('input', {value: 'general'});
      const tags = h('input', {placeholder: 'deploy, backend'});
      const saveButton = h('button', {type: 'submit', class: 'btn primary'}, ui.icon('plus', 'sm'), 'Save record');
      container.append(ui.card({title: 'Save a record', subtitle: 'The author is recorded from your account.'},
        h('form', {class: 'grid', onsubmit: (event) => {
          event.preventDefault();
          busy(saveButton, async () => {
            const scope = JSON.parse(writeScope.value);
            const reply = await call('memory_save', {scope, content: content.value, project: project.value,
              tags: tags.value.split(',').map((t) => t.trim()).filter(Boolean)});
            if (reply.data.saved === false) throw new Error('The quality gate rejected this record');
            show([{scope, record: reply.data}]);
            content.value = '';
          }, 'Record saved');
        }}, h('div', {class: 'form-grid'}, ui.field('Workspace', writeScope), ui.field('Project', project),
          ui.field('Topic tags', tags, 'Comma separated')), ui.field('Text', content), h('div', {class: 'actions'}, saveButton))));
    }
  }

  window.TamDashboard.recordBrowser = recordBrowser;
  window.TamDashboard.scopeLabel = scopeLabel;
})();
