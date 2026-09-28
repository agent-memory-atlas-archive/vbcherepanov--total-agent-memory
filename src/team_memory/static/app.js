'use strict';
(() => {
  const SVG = 'http://www.w3.org/2000/svg';
  const GROUPS = {personal: 'Personal', department: 'Department', company: 'Company', administration: 'Administration'};
  const TOAST_MS = 4200;
  const loaded = new Map();
  let state = null;
  const $ = (id) => document.getElementById(id);

  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'text') node.textContent = value;
      else if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (key === 'value') node.value = value;
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function svg(tag, attrs = {}, ...children) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) if (value !== null && value !== undefined) node.setAttribute(key, value);
    node.append(...children);
    return node;
  }

  function icon(name, size = '') {
    return svg('svg', {class: 'icon' + (size ? ' ' + size : ''), 'aria-hidden': 'true', focusable: 'false'},
      svg('use', {href: '#i-' + name}));
  }

  function hydrateIcons(root = document) {
    for (const slot of root.querySelectorAll('[data-icon]')) {
      if (!slot.firstChild) slot.append(icon(slot.dataset.icon));
    }
  }

  async function loadSprite() {
    try {
      const response = await fetch('/dashboard/static/icons.svg', {credentials: 'same-origin'});
      const doc = new DOMParser().parseFromString(await response.text(), 'image/svg+xml');
      $('sprite').append(document.importNode(doc.documentElement, true));
    } catch (error) {
      toast('Icons could not be loaded', 'bad');
    }
  }

  const rtf = new Intl.RelativeTimeFormat('en', {numeric: 'auto'});
  const UNITS = [['year', 31536000], ['month', 2592000], ['week', 604800], ['day', 86400], ['hour', 3600], ['minute', 60]];
  function toDate(value) {
    if (value === null || value === undefined || value === '') return null;
    const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  function relative(value) {
    const date = toDate(value);
    if (!date) return '—';
    const seconds = (date.getTime() - Date.now()) / 1000;
    for (const [unit, size] of UNITS) if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
    return 'just now';
  }
  function time(value, fallback = 'never') {
    const date = toDate(value);
    if (!date) return h('span', {class: 'muted', text: fallback});
    return h('time', {datetime: date.toISOString(), title: date.toLocaleString()}, relative(value));
  }

  function toast(message, tone = 'info') {
    const names = {good: 'check', bad: 'alert', info: 'spark'};
    const item = h('div', {class: 'toast ' + tone, role: tone === 'bad' ? 'alert' : 'status'}, icon(names[tone] || 'spark'),
      h('p', {text: message}),
      h('button', {type: 'button', class: 'btn ghost sm icon-only', 'aria-label': 'Dismiss', onclick: () => item.remove()}, icon('x', 'sm')));
    $('toasts').append(item);
    setTimeout(() => item.remove(), tone === 'bad' ? TOAST_MS * 2 : TOAST_MS);
  }

  function modal({title, body = [], confirm = null, cancel = 'Cancel', tone = 'primary'}) {
    return new Promise((resolve) => {
      const dialog = h('dialog', {'aria-labelledby': 'dialog-title'});
      const close = (value) => { dialog.close(); dialog.remove(); resolve(value); };
      const buttons = [h('button', {type: 'button', class: 'btn', onclick: () => close(false)}, confirm ? cancel : 'Done')];
      if (confirm) buttons.push(h('button', {type: 'button', class: 'btn ' + (tone === 'danger' ? 'danger solid' : 'primary'), onclick: () => close(true)}, confirm));
      dialog.append(h('div', {class: 'dialog-body'}, h('h2', {id: 'dialog-title', text: title}), body),
        h('div', {class: 'dialog-foot'}, buttons));
      dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(false); });
      document.body.append(dialog);
      dialog.showModal();
      buttons[buttons.length - 1].focus();
    });
  }

  function confirmAction(title, text, confirmLabel, tone = 'danger') {
    return modal({title, body: [h('p', {text})], confirm: confirmLabel, tone});
  }

  function copyButton(value) {
    return h('button', {type: 'button', class: 'btn sm', onclick: async () => {
      try { await navigator.clipboard.writeText(value); toast('Copied to clipboard', 'good'); }
      catch (error) { toast('Copy failed; select the text and copy it manually', 'bad'); }
    }}, icon('copy', 'sm'), 'Copy');
  }

  function showSecret(title, secret, note, extra = []) {
    return modal({title, body: [h('div', {class: 'secret'}, h('code', {text: secret}), copyButton(secret)),
      h('p', {class: 'muted', text: note}), extra]});
  }

  function card({title, subtitle, actions, flush = false, className = ''} = {}, ...body) {
    const head = title || actions ? h('div', {class: 'card-head'},
      h('div', {}, title ? h('h2', {text: title}) : null, subtitle ? h('p', {text: subtitle}) : null),
      actions ? h('div', {class: 'actions'}, actions) : null) : null;
    return h('section', {class: ('card ' + (flush ? 'flush ' : '') + className).trim()}, head, body);
  }

  function kpi({label, value, sub, chart, tone}) {
    const text = String(value);
    return h('div', {class: 'kpi' + (tone ? ' ' + tone : '')}, h('span', {class: 'label', text: label}),
      h('span', {class: 'value' + (text.length > 9 ? ' text' : ''), text}), sub ? h('span', {class: 'sub'}, sub) : null, chart || null);
  }

  function chip(text, tone = '', {dot = false} = {}) {
    return h('span', {class: 'chip' + (tone ? ' ' + tone : '')}, dot ? h('span', {class: 'dot'}) : null, text);
  }

  function hash(text) {
    let value = 0;
    for (const ch of String(text)) value = (value * 31 + ch.charCodeAt(0)) >>> 0;
    return value;
  }
  function initials(name) {
    const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
    return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
  }
  function avatar(name, id, size = '') {
    return h('span', {class: 'avatar c' + (hash(id || name) % 7) + (size ? ' ' + size : ''), 'aria-hidden': 'true', text: initials(name)});
  }
  function person(name, id, size = 'sm') {
    return h('div', {class: 'person'}, avatar(name, id, size), h('div', {}, h('strong', {text: name}), h('small', {text: id})));
  }

  function table(columns, rows, emptyState = 'Nothing to show yet.') {
    if (!rows.length) return typeof emptyState === 'string' ? empty({text: emptyState}) : emptyState;
    const cellClass = (c, index) => [c.numeric ? 'num' : '', index === 0 ? 'lead' : '', c.title ? '' : 'bare', c.className || '']
      .filter(Boolean).join(' ') || null;
    return h('div', {class: 'table-wrap stack'}, h('table', {},
      h('thead', {}, h('tr', {}, columns.map((c) => h('th', {scope: 'col', class: c.numeric ? 'num' : null, text: c.title || ''})))),
      h('tbody', {}, rows.map((row) => h('tr', {}, columns.map((c, index) => h('td', {class: cellClass(c, index), 'data-label': c.title || null},
        c.render ? c.render(row) : (row[c.key] ?? '—'))))))));
  }

  function empty({icon: name = 'spark', title, text, action} = {}) {
    return h('div', {class: 'empty'}, icon(name), title ? h('strong', {text: title}) : null, text ? h('p', {text}) : null,
      action ? h('button', {type: 'button', class: 'btn sm', onclick: action.onClick}, action.label) : null);
  }

  function skeleton(lines = 4, block = false) {
    return h('div', {class: 'skeleton' + (block ? ' block' : ''), 'aria-busy': 'true', 'aria-label': 'Loading'},
      Array.from({length: lines}, () => h('i')));
  }

  function summary(values) {
    const total = values.reduce((a, b) => a + b, 0);
    return total + ' total, peak ' + Math.max(0, ...values) + ' per day';
  }

  function sparkline(values, {height = 44, tone = '', label = 'Activity'} = {}) {
    const width = Math.max(values.length - 1, 1) * 10;
    const peak = Math.max(1, ...values);
    const y = (v) => (height - 3 - (v / peak) * (height - 8)).toFixed(1);
    const points = values.map((v, i) => (i * 10) + ',' + y(v)).join(' ');
    const last = values.length - 1;
    return svg('svg', {class: 'chart' + (tone ? ' ' + tone : ''), viewBox: '0 0 ' + width + ' ' + height, height,
      preserveAspectRatio: 'none', role: 'img', 'aria-label': label + ': ' + summary(values)},
    svg('polygon', {class: 'area', points: '0,' + height + ' ' + points + ' ' + width + ',' + height}),
    svg('polyline', {class: 'line', points}),
    svg('circle', {class: 'dot', cx: last * 10, cy: y(values[last] || 0), r: 2.2}));
  }

  function bars(values, {height = 64, label = 'Distribution', tone = ''} = {}) {
    const width = values.length * 10;
    const peak = Math.max(1, ...values);
    return svg('svg', {class: 'chart', viewBox: '0 0 ' + width + ' ' + height, height, preserveAspectRatio: 'none',
      role: 'img', 'aria-label': label + ': ' + summary(values)},
    ...values.map((v, i) => {
      const barHeight = v ? Math.max(2, (v / peak) * (height - 2)) : 2;
      return svg('rect', {class: 'bar' + (v ? (tone ? ' ' + tone : '') : ' zero'), x: i * 10 + 1.5, width: 7, rx: 1.5,
        y: (height - barHeight).toFixed(1), height: barHeight.toFixed(1)});
    }));
  }

  function menu(items, label = 'Actions') {
    const wrap = h('div', {class: 'menu-wrap'});
    const list = h('div', {class: 'menu', role: 'menu', hidden: true});
    const trigger = h('button', {type: 'button', class: 'btn ghost sm icon-only', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-label': label}, icon('more'));
    const closeMenu = () => { list.hidden = true; trigger.setAttribute('aria-expanded', 'false'); document.removeEventListener('click', outside); };
    const outside = (event) => { if (!wrap.contains(event.target)) closeMenu(); };
    for (const item of items.filter(Boolean)) {
      if (item === 'separator') { list.append(h('hr')); continue; }
      list.append(h('button', {type: 'button', role: 'menuitem', class: item.danger ? 'danger' : null,
        onclick: () => { closeMenu(); item.onClick(); }}, item.icon ? icon(item.icon, 'sm') : null, item.label));
    }
    trigger.addEventListener('click', () => {
      const opening = list.hidden;
      list.hidden = !opening;
      trigger.setAttribute('aria-expanded', String(opening));
      if (opening) { setTimeout(() => document.addEventListener('click', outside)); list.querySelector('button')?.focus(); }
      else closeMenu();
    });
    list.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { closeMenu(); trigger.focus(); }
      const buttons = [...list.querySelectorAll('button')];
      const index = buttons.indexOf(document.activeElement);
      if (event.key === 'ArrowDown') { event.preventDefault(); buttons[(index + 1) % buttons.length].focus(); }
      if (event.key === 'ArrowUp') { event.preventDefault(); buttons[(index - 1 + buttons.length) % buttons.length].focus(); }
    });
    wrap.append(trigger, list);
    return wrap;
  }

  function field(label, control, hint) {
    return h('label', {class: 'field'}, h('span', {text: label}), control, hint ? h('small', {text: hint}) : null);
  }

  function statusPill(check, {configured = true, problem = null} = {}) {
    if (check) return chip((check.ok ? 'Tested OK · ' : 'Error · ') + relative(check.at), check.ok ? 'good' : 'bad', {dot: true});
    if (!configured) return chip(problem || 'Not configured', 'warn', {dot: true});
    return chip('Configured', '', {dot: true});
  }

  async function load(target, build) {
    target.replaceChildren(skeleton());
    try {
      const nodes = await build();
      target.replaceChildren(...[nodes].flat().filter(Boolean));
    } catch (error) {
      target.replaceChildren(empty({icon: 'alert', title: 'Could not load this part', text: error.message,
        action: {label: 'Try again', onClick: () => load(target, build)}}));
    }
  }

  const ui = {h, svg, icon, card, kpi, chip, avatar, person, table, empty, skeleton, sparkline, bars, menu, field, toast,
    modal, confirm: confirmAction, showSecret, copyButton, time, relative, statusPill, load};

  class ApiError extends Error {
    constructor(message, code, httpStatus) { super(message); this.code = code; this.status = httpStatus; }
  }

  async function request(path, {method = 'GET', body} = {}) {
    const headers = {'Accept': 'application/json'};
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      if (state) headers['X-CSRF-Token'] = state.csrf;
    }
    const response = await fetch(path, {method, headers, credentials: 'same-origin', cache: 'no-store',
      body: method === 'GET' ? undefined : JSON.stringify(body || {})});
    let data;
    try { data = await response.json(); } catch (error) { data = {error: 'Unexpected server response (' + response.status + ')'}; }
    if (!response.ok) {
      if (response.status === 401 && state) showLogin('Your session ended. Please sign in again.');
      throw new ApiError(data.error || 'Request failed', data.code, response.status);
    }
    return data;
  }

  const api = (path, options) => request(path.startsWith('/') ? path : '/dashboard/api/' + path, options);

  async function busy(button, action, doneMessage = '') {
    const buttons = button ? [button] : [];
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const result = await action();
      if (doneMessage) toast(doneMessage, 'good');
      return result;
    } catch (error) {
      toast(error.message, 'bad');
      return undefined;
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
  }

  function legacyStatus(message, isError = false) { if (message) toast(message, isError ? 'bad' : 'info'); }
  function legacyShowSecret(_container, title, secret, note) { return showSecret(title, secret, note); }
  function legacyTable(columns, rows, emptyText) { return table(columns, rows, emptyText); }

  function loadAsset(section) {
    if (loaded.has(section.id)) return loaded.get(section.id);
    if (section.stylesheet) document.head.append(h('link', {rel: 'stylesheet', href: section.stylesheet}));
    const promise = new Promise((resolveLoad, reject) => {
      const script = h('script', {src: section.script});
      script.addEventListener('load', resolveLoad);
      script.addEventListener('error', () => reject(new Error('Could not load section ' + section.title)));
      document.head.append(script);
    });
    loaded.set(section.id, promise);
    return promise;
  }

  function context(section) {
    return {user: state.user, teams: state.teams, viewableTeams: state.viewableTeams, csrf: state.csrf,
      sections: state.sections, section: section.id, api, h, status: legacyStatus, busy, table: legacyTable,
      when: (value) => (toDate(value) ? toDate(value).toLocaleString() : '—'), showSecret: legacyShowSecret,
      refresh: boot, navigate: (id) => { location.hash = id; }, ui};
  }

  function resolveMount(dotted) {
    return dotted.split('.').reduce((obj, key) => (obj ? obj[key] : undefined), window);
  }

  async function mountSection(sectionId, container, attach = () => {}) {
    const section = state.sections.find((s) => s.id === sectionId);
    if (!section) throw new Error('This page is not available for your role');
    await loadAsset(section);
    const mount = resolveMount(section.mount);
    if (typeof mount !== 'function') throw new Error('Section ' + section.title + ' did not register ' + section.mount);
    attach();
    await mount(container, context(section));
  }

  function companyName() {
    return (state && state.organization && state.organization.name) || 'Team Memory';
  }

  function setDrawer(open) {
    $('app').classList.toggle('drawer-open', open);
    $('scrim').hidden = !open;
    $('menu').setAttribute('aria-expanded', String(open));
  }

  async function open(sectionId) {
    const section = state.sections.find((s) => s.id === sectionId) || state.sections[0];
    if (!section) return;
    setDrawer(false);
    for (const link of $('nav').querySelectorAll('a')) {
      if (link.dataset.section === section.id) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    $('pageTitle').textContent = section.title;
    $('pageGroup').textContent = GROUPS[section.group] || '';
    document.title = section.title + ' · ' + companyName();
    const view = $('view');
    view.replaceChildren(skeleton(3, true));
    try {
      const container = h('div', {class: 'grid'});
      await mountSection(section.id, container, () => view.replaceChildren(container));
    } catch (error) {
      view.replaceChildren(empty({icon: 'alert', title: 'This page failed to load', text: error.message,
        action: {label: 'Reload', onClick: () => open(section.id)}}));
    }
  }

  function renderNav() {
    const groups = new Map();
    for (const section of state.sections) {
      if (!groups.has(section.group)) groups.set(section.group, []);
      groups.get(section.group).push(section);
    }
    $('nav').replaceChildren(...[...groups.entries()].map(([group, sections]) => h('div', {class: 'nav-group'},
      h('span', {text: GROUPS[group] || group}),
      sections.map((section) => h('a', {href: '#' + section.id, 'data-section': section.id}, icon(section.icon || 'spark'), section.title)))));
  }

  function render() {
    if (state.setup_pending) { showSetup({resume: true}); return; }
    $('setup').hidden = true;
    $('login').hidden = true;
    $('app').hidden = false;
    $('brandName').textContent = companyName();
    $('whoName').textContent = state.user.display_name;
    $('whoRole').textContent = state.user.role_label;
    $('topRole').textContent = state.user.role_label;
    $('meAvatar').replaceChildren(avatar(state.user.display_name, state.user.user_id));
    renderNav();
    open(location.hash.slice(1));
  }

  function showSetup(options) {
    $('login').hidden = true;
    $('app').hidden = true;
    $('setup').hidden = false;
    window.TamSetup.start($('setup'), options);
  }

  async function setupStatus() {
    try {
      return await request('/dashboard/api/setup');
    } catch (error) {
      if (error.status !== 404) toast(error.message, 'bad');
      return null;
    }
  }

  function showLogin(message = '') {
    state = null;
    $('setup').hidden = true;
    $('app').hidden = true;
    $('login').hidden = false;
    $('view').replaceChildren();
    if (message) toast(message, 'bad');
  }

  async function boot() {
    const setup = await setupStatus();
    if (setup) { showSetup({status: setup}); return; }
    try {
      state = await request('/dashboard/api/session');
      render();
    } catch (error) {
      showLogin(error.status === 401 ? '' : error.message);
    }
  }

  function selectTab(name) {
    for (const tab of document.querySelectorAll('[data-tab]')) tab.setAttribute('aria-selected', String(tab.dataset.tab === name));
    for (const panel of document.querySelectorAll('[data-panel]')) panel.hidden = panel.dataset.panel !== name;
  }

  function bindLogin(formId, path, build) {
    $(formId).addEventListener('submit', (event) => {
      event.preventDefault();
      const form = event.target;
      const values = Object.fromEntries(new FormData(form).entries());
      busy(form.querySelector('button'), async () => {
        state = await request(path, {method: 'POST', body: build(values)});
        form.reset();
        render();
      });
    });
  }

  function currentTheme() { return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'; }
  function paintThemeButton() {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    $('theme').replaceChildren(icon(next === 'light' ? 'sun' : 'moon', 'sm'), next === 'light' ? 'Light' : 'Dark');
    $('theme').setAttribute('aria-label', 'Switch to ' + next + ' theme');
  }
  function setTheme(theme, remember) {
    document.documentElement.dataset.theme = theme;
    if (remember) {
      try { window.localStorage.setItem('tam-theme', theme); } catch (error) { toast('Theme choice cannot be remembered in this browser', 'info'); }
    }
    paintThemeButton();
  }
  function storedTheme() {
    try { return window.localStorage.getItem('tam-theme'); } catch (error) { return null; }
  }

  document.addEventListener('DOMContentLoaded', async () => {
    await loadSprite();
    hydrateIcons();
    paintThemeButton();
    fetch('/healthz').then((r) => r.json()).then((d) => { $('loginVersion').textContent = 'v' + d.version; })
      .catch(() => { $('loginVersion').hidden = true; });
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (event) => {
      if (!storedTheme()) setTheme(event.matches ? 'light' : 'dark', false);
    });
    $('theme').addEventListener('click', () => setTheme(currentTheme() === 'dark' ? 'light' : 'dark', true));
    for (const tab of document.querySelectorAll('[data-tab]')) tab.addEventListener('click', () => selectTab(tab.dataset.tab));
    bindLogin('form-password', '/dashboard/api/login', (v) => ({user_id: v.user_id.trim(), password: v.password}));
    bindLogin('form-token', '/dashboard/api/login/token', (v) => ({token: v.token.trim()}));
    bindLogin('form-invite', '/dashboard/api/invite/redeem', (v) => {
      if (v.password !== v.repeat) throw new Error('Passwords do not match');
      return {user_id: v.user_id.trim(), code: v.code.trim(), password: v.password};
    });
    $('logout').addEventListener('click', () => busy($('logout'), async () => {
      await request('/dashboard/api/logout', {method: 'POST'});
      showLogin();
      toast('Signed out', 'info');
    }));
    $('menu').addEventListener('click', () => setDrawer(!$('app').classList.contains('drawer-open')));
    $('scrim').addEventListener('click', () => setDrawer(false));
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setDrawer(false); });
    window.addEventListener('hashchange', () => { if (state) open(location.hash.slice(1)); });
    boot();
  });

  window.TamSections = window.TamSections || {};
  const setupBridge = {
    adopt: (overview) => { state = overview; return state; },
    session: () => state,
    mountSection,
    finish: async () => { state = await request('/dashboard/api/session'); render(); },
  };
  window.TamDashboard = {h, api, busy, ui, status: legacyStatus, table: legacyTable, when: relative, showSecret: legacyShowSecret,
    setup: setupBridge};
})();
