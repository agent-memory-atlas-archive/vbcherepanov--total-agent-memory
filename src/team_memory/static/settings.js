'use strict';
window.TamSections.settings = async (container, ctx) => {
  const {h, ui} = ctx;
  const TARGETS = {llm: ['Language model', 'Used for enrichment, summaries and answer checks.'],
    embed: ['Embeddings', 'Turns records into vectors for search. Changing it makes existing vectors incompatible until re-embedded.']};
  const SOURCE = {web: ['Set here', 'accent'], env: ['From environment', 'violet'], default: ['Default', '']};
  const pending = new Map();
  const root = h('div', {class: 'grid'});
  const bar = h('div', {class: 'savebar', hidden: true, role: 'region', 'aria-label': 'Unsaved changes'});
  let data = null;
  let byKey = {};

  function setPending(key, value) {
    const item = byKey[key];
    const original = item.source === 'web' && item.kind !== 'secret' ? item.value : null;
    if (value === undefined || (item.kind !== 'secret' && value === original) || (value === null && item.source !== 'web')) pending.delete(key);
    else pending.set(key, value);
    paintBar();
  }

  function describe(key) {
    const value = pending.get(key);
    const item = byKey[key];
    if (value === null) return item.label + ' → reset';
    return item.label + (item.kind === 'secret' ? ' → new key' : ' → ' + value);
  }

  function paintBar() {
    bar.hidden = pending.size === 0;
    if (!pending.size) return;
    const save = h('button', {type: 'button', class: 'btn primary', onclick: async () => {
      const ok = await ui.modal({title: 'Apply settings?', confirm: 'Save and restart workers', body: [
        h('p', {text: 'Workers will restart so they pick up the change. Running operations finish first; the next request loads a fresh worker.'}),
        h('ul', {class: 'feed'}, [...pending.keys()].map((key) => h('li', {}, ui.icon('edit', 'sm'), h('div', {class: 'what'}, h('p', {text: describe(key)})), h('span')))),
      ]});
      if (!ok) return;
      const values = Object.fromEntries(pending);
      const reply = await ctx.busy(save, () => ctx.api('admin/settings', {method: 'POST', body: {values}}));
      if (!reply) return;
      pending.clear();
      ui.toast('Saved ' + reply.changed.length + ' setting(s); ' + reply.workers_recycled + ' worker(s) restarted', 'good');
      await reload();
    }}, 'Save changes');
    bar.replaceChildren(ui.icon('alert', 'sm'), h('p', {}, h('strong', {text: pending.size + ' unsaved change' + (pending.size > 1 ? 's' : '') + ': '}),
      [...pending.keys()].map(describe).join(', ')),
    h('button', {type: 'button', class: 'btn', onclick: () => { pending.clear(); render(); }}, 'Discard'), save);
  }

  function editor(key) {
    const item = byKey[key];
    const chipFor = () => ui.chip(...SOURCE[item.source]);
    const label = h('div', {class: 'record-meta'}, h('strong', {text: item.label}), chipFor(), h('code', {class: 'muted', text: key}));
    if (item.kind === 'secret') {
      const shown = item.is_set ? (item.readable ? item.hint : 'unreadable with the current master key') : 'not set';
      const slot = h('div', {class: 'masked'});
      const idle = () => slot.replaceChildren(h('code', {text: shown}),
        h('button', {type: 'button', class: 'btn sm', onclick: replace}, ui.icon('key', 'sm'), item.is_set ? 'Replace key' : 'Add key'),
        item.source === 'web' ? h('button', {type: 'button', class: 'btn ghost sm', onclick: () => { setPending(key, null); slot.replaceChildren(h('code', {text: 'will be removed'}), undo()); }}, 'Remove') : null);
      const undo = () => h('button', {type: 'button', class: 'btn ghost sm', onclick: () => { setPending(key, undefined); idle(); }}, 'Undo');
      function replace() {
        const input = h('input', {type: 'password', autocomplete: 'new-password', placeholder: 'Paste the new key', 'aria-label': 'New value for ' + item.label});
        input.addEventListener('input', () => setPending(key, input.value.trim() || undefined));
        slot.replaceChildren(input, undo());
        input.focus();
      }
      if (pending.has(key)) {
        if (pending.get(key) === null) slot.replaceChildren(h('code', {text: 'will be removed'}), undo());
        else replace();
      } else idle();
      return h('div', {class: 'setting-row'}, label, slot, item.help ? h('small', {class: 'muted', text: item.help}) : null);
    }
    const current = pending.has(key) ? pending.get(key) : (item.source === 'web' ? item.value : '');
    const placeholder = item.source === 'env' ? item.value : 'default';
    const input = item.kind === 'choice'
      ? h('select', {'aria-label': item.label}, h('option', {value: ''}, item.source === 'env' ? 'Environment: ' + item.value : 'Default'),
        item.choices.map((c) => h('option', {value: c, selected: c === current}, c)))
      : h('input', {'aria-label': item.label, value: current || '', placeholder, autocomplete: 'off', inputmode: item.kind === 'number' ? 'decimal' : item.kind === 'integer' ? 'numeric' : null});
    input.addEventListener(item.kind === 'choice' ? 'change' : 'input', () => {
      const value = input.value.trim();
      setPending(key, value ? value : (item.source === 'web' ? null : undefined));
    });
    return h('div', {class: 'setting-row'}, label, input, item.help ? h('small', {class: 'muted', text: item.help}) : null);
  }

  function providerCard(provider) {
    const test = h('button', {type: 'button', class: 'btn sm', onclick: () => ctx.busy(test, async () => {
      const result = await ctx.api('admin/settings/test', {method: 'POST', body: {target: provider.target, provider: provider.id}});
      ui.toast(provider.label + ': ' + (result.ok ? 'OK' : 'failed') + ' — ' + result.detail, result.ok ? 'good' : 'bad');
      await reload();
    })}, ui.icon('pulse', 'sm'), 'Test');
    const dirty = provider.fields.some((key) => pending.has(key));
    return h('article', {class: 'provider' + (provider.active ? ' active' : '')},
      h('div', {class: 'provider-head'}, h('strong', {text: provider.label}), provider.active ? ui.chip('Active', 'accent') : null),
      h('div', {class: 'record-meta'}, ui.statusPill(provider.check, {configured: provider.configured, problem: provider.problem}),
        provider.check && !provider.check.ok ? h('span', {class: 'muted', text: provider.check.detail}) : null),
      provider.active ? h('div', {class: 'grid'}, provider.fields.map(editor)) : h('small', {text: provider.local ? 'Runs locally.' : 'Select it as active to edit its fields.'}),
      h('div', {class: 'actions'}, test, dirty ? h('small', {text: 'Tests use saved values — save first.'}) : null));
  }

  function targetCard(target) {
    const [title, subtitle] = TARGETS[target];
    const providerKey = data.provider_keys[target];
    const item = byKey[providerKey];
    const chosen = pending.has(providerKey) ? pending.get(providerKey) : item.value;
    const providers = data.providers.filter((p) => p.target === target).map((p) => {
      const activeNow = chosen ? chosen === p.id : p.active;
      return {...p, active: activeNow};
    });
    const select = h('select', {'aria-label': title + ' provider'},
      providers.map((p) => h('option', {value: p.id, selected: p.active}, p.label)),
      target === 'llm' ? h('option', {value: 'auto', selected: chosen === 'auto'}, 'Auto (first key found)') : null);
    select.addEventListener('change', () => { setPending(providerKey, select.value); render(); });
    const common = data.common[target] || [];
    return ui.card({title, subtitle, actions: [ui.chip(...SOURCE[item.source]), ui.field('Active provider', select)]},
      h('div', {class: 'provider-grid'}, providers.map(providerCard)),
      common.length ? h('details', {class: 'advanced'}, h('summary', {text: 'Advanced options'}),
        h('div', {}, common.map(editor))) : null);
  }

  function recallCard() {
    const keys = data.settings.filter((s) => s.group === 'recall').map((s) => s.key);
    return ui.card({title: 'Search answers', subtitle: 'What agents receive from memory_recall. Applies to the next request.'},
      h('div', {class: 'grid'}, keys.map(editor)));
  }

  function render() {
    root.replaceChildren(h('p', {class: 'muted', text: 'Precedence: values set here override the server environment, which overrides built-in defaults. Keys are encrypted at rest and never sent back to the browser.'}),
      targetCard('llm'), targetCard('embed'), recallCard());
    paintBar();
  }

  async function reload() {
    data = await ctx.api('admin/settings');
    byKey = Object.fromEntries(data.settings.map((s) => [s.key, s]));
    render();
  }

  container.append(root, bar);
  root.append(ui.skeleton(3, true));
  await reload();
};
