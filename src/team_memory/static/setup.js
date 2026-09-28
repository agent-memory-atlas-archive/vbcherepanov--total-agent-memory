'use strict';
(() => {
  const STEPS = [
    {id: 'code', title: 'Setup code'},
    {id: 'database', title: 'Database'},
    {id: 'company', title: 'Company'},
    {id: 'admin', title: 'First admin'},
    {id: 'departments', title: 'Departments'},
    {id: 'providers', title: 'Providers'},
    {id: 'connect', title: 'Connect your team'},
    {id: 'done', title: 'Done'},
  ];
  const at = (id) => STEPS.findIndex((step) => step.id === id);
  // Steps before this one run on the setup code; from here on the new superadmin is signed in.
  const SIGNED_IN_FROM = at('departments');
  const IDENTIFIER = /^[a-zA-Z0-9_-]{1,64}$/;
  const MIN_PASSWORD = 12;
  const TOKEN_IN_HASH = /(?:^#|&)setup=([A-Za-z0-9-]{1,64})/;
  let dash, root, index, draft, status;

  const h = (...args) => dash.h(...args);

  function tokenFromHash() {
    const match = TOKEN_IN_HASH.exec(location.hash);
    if (!match) return '';
    history.replaceState(null, '', location.pathname + location.search);
    return match[1];
  }

  function slug(text) {
    return text.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '').slice(0, 64);
  }

  function baseUrl() {
    return (draft.publicUrl || location.origin).replace(/\/+$/, '');
  }

  function validUrl(value) {
    try {
      const url = new URL(value);
      return (url.protocol === 'http:' || url.protocol === 'https:') && !url.search && !url.hash && !url.username;
    } catch (error) {
      return false;
    }
  }

  function go(next) {
    index = next;
    paint();
    const heading = root.querySelector('.setup-head h2');
    if (heading) heading.focus();
  }

  function rail() {
    const {ui} = dash;
    return h('aside', {class: 'setup-rail', 'aria-label': 'Setup progress'},
      h('div', {class: 'brand'}, h('span', {class: 'logo'}, ui.icon('logo')), 'total-agent-memory'),
      h('div', {class: 'setup-intro'}, h('h1', {text: 'Set up your team memory'}),
        h('p', {text: 'Create the first administrator, add departments, pick model providers and connect people. Everything here can be changed later.'})),
      h('ol', {class: 'setup-steps'}, STEPS.map((step, i) => h('li', {class: i < index ? 'done' : null, 'aria-current': i === index ? 'step' : null},
        h('span', {class: 'setup-dot', 'aria-hidden': 'true'}, i < index ? ui.icon('check', 'sm') : String(i + 1)),
        h('span', {text: step.title}), i < index ? h('span', {class: 'sr-only', text: '(done)'}) : null))),
      h('p', {class: 'setup-foot', text: 'Only someone with the setup code from the server console can create the first administrator.'}));
  }

  function panel({lead, body = [], back = null, next = null, wide = false}) {
    const step = STEPS[index];
    return h('main', {class: 'setup-main'}, h('div', {class: 'setup-panel' + (wide ? ' wide' : '')},
      h('div', {class: 'setup-progress'}, h('span', {text: 'Step ' + (index + 1) + ' of ' + STEPS.length}),
        h('progress', {max: STEPS.length, value: index + 1, 'aria-label': 'Setup progress'})),
      h('div', {class: 'setup-head'}, h('h2', {tabindex: '-1', text: step.title}), lead ? h('p', {text: lead}) : null),
      body,
      back || next ? h('div', {class: 'setup-actions'}, back || h('span'), next) : null));
  }

  function backButton(target) {
    return h('button', {type: 'button', class: 'btn', onclick: () => go(target)}, 'Back');
  }

  function form(fields, label, onSubmit, back = null) {
    const submit = h('button', {class: 'btn primary', type: 'submit'}, label);
    const node = h('form', {class: 'setup-form', novalidate: true}, fields, h('div', {class: 'setup-actions'}, back || h('span'), submit));
    node.addEventListener('submit', (event) => {
      event.preventDefault();
      dash.busy(submit, () => onSubmit(node));
    });
    return node;
  }

  function notice(text, tone = 'warn') {
    return h('div', {class: 'setup-note ' + tone, role: 'note'}, dash.ui.icon(tone === 'warn' ? 'alert' : 'spark', 'sm'), h('p', {text}));
  }

  const steps = {
    code() {
      const {ui} = dash;
      const input = h('input', {name: 'token', class: 'mono', autocomplete: 'one-time-code', spellcheck: 'false',
        placeholder: 'XXXX-XXXX-XXXX-XXXX-XXXX-XXXX', value: draft.token, required: true});
      const body = [
        status.token_active ? null : notice('There is no valid setup code right now. Restart the server, or run `tam-team --root <data dir> setup-token` on the server, then enter the new code.'),
        form([ui.field('Setup code', input, 'The server printed it in its console or log when it started. It works once and expires.')],
          'Continue', async () => {
            const token = input.value.trim();
            if (!token) throw new Error('Enter the setup code');
            await dash.api('setup/verify', {method: 'POST', body: {token}});
            draft.token = token;
            go(at('database'));
          }),
      ];
      return panel({lead: 'Prove you have access to the server: enter the one-time code it printed at startup.', body});
    },

    database() {
      const {ui} = dash;
      const db = window.TamDatabase;
      const current = status.database;
      const lead = 'Where the server keeps accounts, settings and memory. SQLite needs nothing else; PostgreSQL must already exist and be reachable from this server.';
      const next = h('button', {type: 'button', class: 'btn primary'}, 'Continue');
      const forward = () => go(at('company'));
      if (!current || current.backend === 'postgres') {
        next.addEventListener('click', forward);
        const text = !current ? 'This server stores everything in SQLite files in its data directory.'
          : 'This server already uses PostgreSQL' + (current.source === 'env' ? ' (set by TAM_TEAM_DATABASE_URL on the server).' : '.') +
            ' You can change the connection later under Administration → Database.';
        return panel({lead, body: notice(text, 'info'), back: backButton(at('code')), next});
      }
      const host = h('div', {class: 'grid'});
      const progress = h('p', {class: 'muted', role: 'status', 'aria-live': 'polite'});
      let dsn = null;
      function paintChoice() {
        const result = h('div', {class: 'grid'});
        if (draft.backend === 'sqlite') {
          dsn = null;
          next.textContent = 'Continue';
          host.replaceChildren(notice('Good for most teams: one server, files in the data directory, backups with one command. You can move to PostgreSQL later under Administration → Database.', 'info'));
          return;
        }
        dsn = db.dsnInput(dash, {});
        const test = h('button', {type: 'button', class: 'btn'}, ui.icon('pulse', 'sm'), 'Test');
        test.addEventListener('click', () => dash.busy(test, async () => {
          if (!dsn.value()) throw new Error('Paste the PostgreSQL connection string first');
          const report = await dash.api('setup/database/test', {method: 'POST', body: {token: draft.token, dsn: dsn.value()}});
          result.replaceChildren(db.checkList(dash, report));
        }));
        next.textContent = 'Use PostgreSQL and continue';
        host.replaceChildren(notice('The database must be created by your administrator with UTF-8 and the C.UTF-8 builtin locale; Test lists anything missing with the exact SQL to run.', 'info'),
          dsn.node, h('div', {class: 'actions'}, test), result);
      }
      next.addEventListener('click', () => dash.busy(next, async () => {
        if (draft.backend === 'sqlite') { forward(); return; }
        if (!dsn.value()) throw new Error('Paste the PostgreSQL connection string first');
        progress.textContent = 'Moving the setup data to PostgreSQL…';
        try {
          const view = await dash.api('setup/database', {method: 'POST', body: {token: draft.token, backend: 'postgres', dsn: dsn.value()}});
          status.database = {backend: view.backend, source: view.source};
        } finally {
          progress.textContent = '';
        }
        dsn.clear();
        dash.ui.toast('The server now uses PostgreSQL', 'good');
        forward();
      }));
      paintChoice();
      const choice = db.backendSwitch(dash, draft.backend, (id) => { draft.backend = id; paintChoice(); });
      return panel({lead, body: [h('div', {class: 'field'}, h('span', {text: 'Backend', 'aria-hidden': 'true'}), choice), host, progress],
        back: backButton(at('code')), next});
    },

    company() {
      const {ui} = dash;
      const name = h('input', {name: 'company', autocomplete: 'organization', value: draft.company, required: true, maxlength: '128'});
      const url = h('input', {name: 'public_url', type: 'url', inputmode: 'url', value: draft.publicUrl, spellcheck: 'false'});
      return panel({lead: 'Your company name appears in the dashboard header. The public address goes into the connection instructions for your team.',
        body: form([ui.field('Company name', name), ui.field('Public address', url, 'Where people reach this server, for example https://memory.example.com. Use HTTPS for access from other machines.')],
          'Continue', async () => {
            if (!name.value.trim()) throw new Error('Enter the company name');
            const address = url.value.trim().replace(/\/+$/, '');
            if (address && !validUrl(address)) throw new Error('Enter an http(s) address without query or credentials');
            draft.company = name.value.trim();
            draft.publicUrl = address;
            go(at('admin'));
          }, backButton(at('database')))});
    },

    admin() {
      const {ui} = dash;
      const userId = h('input', {name: 'user_id', autocomplete: 'username', value: draft.userId, required: true, spellcheck: 'false'});
      const name = h('input', {name: 'name', autocomplete: 'name', value: draft.name, required: true});
      const password = h('input', {name: 'password', type: 'password', autocomplete: 'new-password', minlength: String(MIN_PASSWORD), required: true});
      const repeat = h('input', {name: 'repeat', type: 'password', autocomplete: 'new-password', minlength: String(MIN_PASSWORD), required: true});
      const fields = h('div', {class: 'form-grid'}, ui.field('User ID', userId, 'Letters, digits, - and _. You sign in with it.'),
        ui.field('Full name', name));
      const secrets = h('div', {class: 'form-grid'}, ui.field('Password', password, 'At least ' + MIN_PASSWORD + ' characters.'),
        ui.field('Repeat password', repeat));
      return panel({lead: 'This person becomes the superadmin: they manage users, departments, tokens and providers.',
        body: form([fields, secrets], 'Create admin and sign in', async () => {
          draft.userId = userId.value.trim();
          draft.name = name.value.trim();
          if (!IDENTIFIER.test(draft.userId)) throw new Error('User ID may contain only letters, digits, - and _');
          if (!draft.name) throw new Error('Enter the full name');
          if (password.value.length < MIN_PASSWORD) throw new Error('The password needs at least ' + MIN_PASSWORD + ' characters');
          if (password.value !== repeat.value) throw new Error('Passwords do not match');
          const overview = await dash.api('setup/complete', {method: 'POST', body: {token: draft.token, company_name: draft.company,
            public_url: draft.publicUrl || null, user_id: draft.userId, name: draft.name, password: password.value}});
          password.value = '';
          repeat.value = '';
          draft.token = '';
          draft.savedUrl = draft.publicUrl;
          dash.setup.adopt(overview);
          dash.ui.toast('Signed in as ' + overview.user.display_name, 'good');
          go(at('departments'));
        }, backButton(at('company')))});
    },

    departments() {
      const {ui} = dash;
      const list = h('div', {class: 'setup-list'});
      const name = h('input', {name: 'team_name', placeholder: 'e.g. Customer Support', maxlength: '128'});
      const id = h('input', {name: 'team_id', class: 'mono', placeholder: 'customer-support', spellcheck: 'false', maxlength: '64'});
      let idEdited = false;
      name.addEventListener('input', () => { if (!idEdited) id.value = slug(name.value); });
      id.addEventListener('input', () => { idEdited = id.value.trim() !== ''; });
      async function refresh() {
        const teams = await dash.api('admin/teams');
        draft.departments = teams.length;
        list.replaceChildren(teams.length
          ? h('ul', {class: 'setup-chips'}, teams.map((team) => h('li', {}, ui.icon('layers', 'sm'), h('strong', {text: team.name}), h('code', {text: team.team_id}))))
          : ui.empty({icon: 'layers', text: 'No departments yet. Add one below, or continue and add them later.'}));
      }
      const add = form([h('div', {class: 'form-grid'}, ui.field('Department name', name), ui.field('ID', id, 'Used in team scopes and the CLI.'))],
        'Add department', async () => {
          const team = {id: id.value.trim(), name: name.value.trim()};
          if (!team.name) throw new Error('Enter the department name');
          if (!IDENTIFIER.test(team.id)) throw new Error('The ID may contain only letters, digits, - and _');
          await dash.api('admin/teams', {method: 'POST', body: team});
          ui.toast('Added ' + team.name, 'good');
          name.value = '';
          id.value = '';
          idEdited = false;
          await refresh();
          name.focus();
        });
      add.querySelector('.setup-actions').classList.add('inline');
      const node = panel({lead: 'Departments share memory among their members. Add as many as you need; people are assigned later under Administration.',
        body: [ui.card({title: 'Departments'}, list, add)], next: h('button', {type: 'button', class: 'btn primary', onclick: () => go(at('providers'))}, 'Continue')});
      ui.load(list, async () => { await refresh(); return [...list.childNodes]; });
      return node;
    },

    providers() {
      const {ui} = dash;
      const host = h('div', {class: 'grid'});
      const node = panel({wide: true,
        lead: 'The local defaults (Ollama, FastEmbed) need no keys. Switch providers or add keys now, or later under Administration → Providers.',
        body: host, back: backButton(at('departments')), next: h('button', {type: 'button', class: 'btn primary', onclick: () => go(at('connect'))}, 'Continue')});
      ui.load(host, async () => {
        const mount = h('div', {class: 'grid'});
        await dash.setup.mountSection('settings', mount);
        return mount;
      });
      return node;
    },

    connect() {
      const {ui} = dash;
      const address = h('input', {type: 'url', inputmode: 'url', value: baseUrl(), spellcheck: 'false', 'aria-label': 'Public address'});
      const mcp = h('code', {class: 'setup-url'});
      const snippet = h('pre', {class: 'cmd'});
      const copy = h('div', {class: 'actions'});
      let flavour = 'tam-remote';
      const tabs = h('div', {class: 'segmented two', role: 'tablist', 'aria-label': 'Client bridge'});
      function paintSnippet() {
        const url = (address.value.trim().replace(/\/+$/, '') || location.origin) + '/mcp/';
        const server = flavour === 'tam-remote' ? {command: 'tam-remote'} : {command: 'python3', args: ['/absolute/path/remote.py']};
        const text = JSON.stringify({mcpServers: {'total-agent-memory': {...server,
          env: {TAM_REMOTE_URL: url, TAM_REMOTE_TOKEN_FILE: '/absolute/path/to/personal.token'}}}}, null, 2);
        mcp.textContent = url;
        snippet.textContent = text;
        copy.replaceChildren(ui.copyButton(text));
        tabs.replaceChildren(...[['tam-remote', 'tam-remote'], ['remote-py', 'remote.py (no install)']].map(([id, label]) =>
          h('button', {type: 'button', role: 'tab', 'aria-selected': String(id === flavour), onclick: () => { flavour = id; paintSnippet(); }}, label)));
      }
      address.addEventListener('input', paintSnippet);
      paintSnippet();
      async function saveAddress() {
        const value = address.value.trim().replace(/\/+$/, '');
        if (!value || value === draft.savedUrl) return;
        if (!validUrl(value)) throw new Error('Enter an http(s) address without query or credentials');
        await dash.api('admin/organization', {method: 'POST', body: {public_url: value}});
        draft.publicUrl = value;
        draft.savedUrl = value;
      }
      const next = h('button', {type: 'button', class: 'btn primary', onclick: () => dash.busy(next, async () => { await saveAddress(); go(at('done')); })}, 'Continue');
      const how = h('ol', {class: 'setup-howto'},
        h('li', {}, h('strong', {text: 'Invite people. '}), 'Under Administration → Users, add a person. They get a one-time invite code, open this dashboard, choose ', h('em', {text: 'Invite code'}), ' and set a password. From the server shell: ', h('code', {text: 'tam-team --root <data dir> invite <user-id>'}), '.'),
        h('li', {}, h('strong', {text: 'Create a token. '}), 'Each person opens Tokens & password, creates a token for their AI client and saves it to a private file.'),
        h('li', {}, h('strong', {text: 'Add the server to the client. '}), 'Paste the snippet below into the MCP configuration of Claude Code, Codex, Cursor or another client and point it at the token file.'));
      return panel({lead: 'AI clients connect to one MCP endpoint with a personal token per person.',
        body: [ui.card({title: 'MCP endpoint', subtitle: 'Clients connect here. Use HTTPS for other machines.'},
          ui.field('Public address', address), h('div', {class: 'record-meta'}, h('span', {text: 'MCP URL'}), mcp)),
        ui.card({title: 'How people connect'}, how, tabs, snippet, copy)],
        back: backButton(at('providers')), next});
    },

    done() {
      const {ui} = dash;
      const session = dash.setup.session();
      const row = (icon, label, value, note) => h('li', {}, ui.icon(icon), h('div', {}, h('span', {text: label}),
        h('strong', {text: value}), note ? h('small', {text: note}) : null));
      const summary = h('ul', {class: 'setup-summary'},
        row('building', 'Company', (session.organization && session.organization.name) || '—'),
        row('user-cog', 'Administrator', session.user.display_name, session.user.user_id),
        row('layers', 'Departments', draft.departments === null ? '—' : String(draft.departments),
          draft.departments ? null : 'Add them any time under Administration → Departments'));
      const next = h('ol', {class: 'setup-howto'},
        h('li', {}, h('strong', {text: 'Invite people '}), 'under Administration → Users.'),
        h('li', {}, h('strong', {text: 'Create your own token '}), 'under Tokens & password to connect your AI client.'),
        h('li', {}, h('strong', {text: 'Adjust anything later: '}), 'company name and address are under Administration → System.'));
      const open = h('button', {type: 'button', class: 'btn primary', onclick: () => dash.busy(open, async () => {
        await dash.api('admin/setup/finish', {method: 'POST'});
        await dash.setup.finish();
      })}, 'Open the dashboard');
      const help = session.support_url ? h('p', {class: 'setup-help muted'}, session.support_line.replace(session.support_url, ''),
        h('a', {href: session.support_url, target: '_blank', rel: 'noopener noreferrer', text: session.support_url})) : null;
      return panel({lead: 'Your server is ready.', body: [ui.card({}, summary), ui.card({title: 'What next'}, next), help],
        back: backButton(at('connect')), next: open});
    },
  };

  function paint() {
    root.replaceChildren(rail(), steps[STEPS[index].id]());
  }

  async function autoVerify() {
    try {
      await dash.api('setup/verify', {method: 'POST', body: {token: draft.token}});
      go(at('database'));
    } catch (error) {
      dash.ui.toast(error.message, 'bad');
    }
  }

  window.TamSetup = {
    start(element, options) {
      dash = window.TamDashboard;
      root = element;
      status = options.status || {token_active: true, organization: {}};
      const organization = status.organization || {};
      draft = {token: options.resume ? '' : tokenFromHash(), company: organization.name || '',
        publicUrl: organization.public_url || location.origin, savedUrl: organization.public_url || '',
        userId: '', name: '', departments: null, backend: 'sqlite'};
      index = options.resume ? SIGNED_IN_FROM : 0;
      paint();
      if (!options.resume && draft.token) autoVerify();
      if (options.resume) {
        dash.api('admin/organization').then((org) => {
          draft.publicUrl = org.public_url || draft.publicUrl;
          draft.savedUrl = org.public_url || '';
        }).catch((error) => dash.ui.toast(error.message, 'bad'));
      }
    },
  };
})();
