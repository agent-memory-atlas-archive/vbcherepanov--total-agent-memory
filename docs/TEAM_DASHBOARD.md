# Team dashboard

The team server (`tam-team serve`) serves a web dashboard at `/dashboard/`. Company staff use it to sign in with a password, and superadmins use it to run the organisation (users, departments, tokens, audit log, LLM/embedding provider keys) from the browser. MCP clients do not change: they still use personal Bearer tokens on `/mcp/` and `/api/call`. The older token-only page at `/` is still served and links to the dashboard.

## Roles

A user has one **organisation role** (`users.org_role`) and one **department role** per team (`membership.role`).

| Organisation role | What it adds |
|---|---|
| `member` (default) | Own memory (search, save, edit, history), own tokens, own password. |
| `company_viewer` | Read-only access to every department: member list, activity, and team memory. |
| `superadmin` | Everything above, plus users, departments, membership, all tokens, audit log, provider settings. |

| Department role | Team memory | People & activity | Curriculum (learning module) |
|---|---|---|---|
| `reader` | read | – | – |
| `editor` | read / write | – | – |
| `manager` (department head) | read / write | yes | yes |

Visibility rules:

* **Personal memory is private to its owner.** Nobody else can read it through the dashboard or MCP, superadmins included. A personal scope has no owner field, so it always resolves to the caller's own workspace.
* `company_viewer` and `superadmin` can read any existing team scope. `Registry.authorize(actor, team_scope, write=False)` grants this "oversight" read. They cannot write unless they are an editor or manager of that team. Oversight teams are not added to `memory_scopes` or to unscoped `memory_recall`, so MCP fan-out stays membership-only.
* A manager sees people and activity for their own departments only.
* The server refuses to disable or demote the last active superadmin.

## First start

Easiest path: start the server with no administrator. It prints a one-time setup code, and `/dashboard/` opens the setup wizard (company name, first admin, departments, providers, connection snippet). Alternatively, run `tam setup` and choose *Company server* on the server machine. See [SETUP_WIZARD.md](SETUP_WIZARD.md). The manual CLI path still works:

```sh
tam-team --root /srv/tam bootstrap-admin alice 'Alice Admin'
# Invite code for alice: 7KQ2-M9XD-...   Valid until ...; single use.
tam-team --root /srv/tam serve --host 127.0.0.1 --port 3737
```

Open `http://127.0.0.1:3737/dashboard/`, choose **Use invite code**, and enter the user ID, the code, and a new password. `bootstrap-admin` refuses to run if an active superadmin already exists. After that, use:

```sh
tam-team --root /srv/tam user-role bob company_viewer   # member | company_viewer | superadmin
tam-team --root /srv/tam invite bob                     # new one-time code = password reset
tam-team --root /srv/tam member bob sales manager       # reader | editor | manager | remove
tam-team --root /srv/tam user-disable bob               # offboard: revoke tokens, end sessions, block sign-in
tam-team --root /srv/tam user-enable bob                # lift the block; then issue a new invite or token
tam-team --root /srv/tam user-export bob --out bob.jsonl  # disabled user: personal records + history (0600)
tam-team --root /srv/tam user-purge bob --confirm bob   # disabled user, server stopped: delete personal area
```

All earlier commands (`user-add`, `team-add`, `member`, `token-create`, `token-revoke`, `backup`, `restore`, `serve`) are unchanged.

## Login flow

1. A superadmin creates a user in the dashboard (or with the CLI) and gets a **one-time invite code**. The code is 20 characters from a 32-symbol alphabet (100 bits), is shown once, and is stored as a SHA-256 digest. It expires after `TAM_TEAM_INVITE_TTL_HOURS` (default 72), works once, and issuing a new code voids earlier unused ones.
2. The employee redeems the code with their user ID and a password of 12–256 characters. The password is hashed with `hashlib.scrypt` (N=2^15, r=8, p=1, 16-byte per-user salt, 32-byte key) and compared in constant time. Redeeming a code signs out all of that user's other sessions.
3. After that the user signs in with user ID + password. **Personal token** login is also available. A session opened with a token ends as soon as that token is revoked.
4. **Password reset** means a superadmin issues a new invite code. Users who know their password change it under *Tokens & password*, which signs out their other sessions.

Unknown users and wrong passwords get the same error and a similar response time: a dummy scrypt runs for unknown users.

## Sessions and browser security

* Sessions are stored server-side (`sessions` table, digest of a random 256-bit ID). The cookie is `tam_session`, or `__Host-tam_session` over HTTPS, and is `HttpOnly`, `SameSite=Strict`, `Path=/`.
* Sessions expire after 30 idle minutes (`TAM_TEAM_SESSION_IDLE_MINUTES`) and 12 hours in total (`TAM_TEAM_SESSION_MAX_HOURS`). Sign-out deletes the server row. Disabling a user deletes all of that user's sessions, revokes all of their tokens and voids unused invite codes (`Registry.set_active`, the same path as `tam-team user-disable`). Enabling the user again does not bring those back: issue a new invite or token.
* **CSRF**: every POST under `/dashboard/api/` except the three login endpoints must send `X-CSRF-Token` (a per-session random value from `/dashboard/api/session`). All POST bodies must be `application/json`. A request whose `Origin` header does not match `Host` is rejected with 403.
* **Secure cookie**: `TAM_TEAM_COOKIE_SECURE=auto|always|never` (default `auto` = when the request arrived over HTTPS). `X-Forwarded-Proto` and `X-Forwarded-For` (last hop) are honoured only with `TAM_TEAM_TRUST_PROXY=true`. Enable that only when a reverse proxy you control overwrites those headers.
* **Lockout**: 5 failures (`TAM_TEAM_LOGIN_MAX_FAILURES`) for one user from one IP within 15 minutes lock that pair for `TAM_TEAM_LOGIN_LOCK_MINUTES` (default 15). Separately, 30 failures from one IP across all users lock that IP. Invite redemption, token login and password change use the same limiter. Lockouts are written to the audit log.
* **CSP**: `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`. There are no inline scripts or styles. The pages are plain static files in `src/team_memory/static/` (shipped as package data, no build step). Pages build the DOM with `textContent` only.
* Every admin action goes to `admin_events` with the acting user (`cli` for the CLI) and never includes secret values.

## Provider settings

*Provider settings* (superadmin only) edits the variables TAM actually reads:

| Group | Variables |
|---|---|
| LLM | `MEMORY_LLM_ENABLED`, `MEMORY_LLM_PROVIDER` (`ollama`, `openai`, `openai-compatible`, `anthropic`, `auto`), `MEMORY_LLM_MODEL`, `MEMORY_LLM_API_BASE`, `OLLAMA_URL`, `MEMORY_LLM_TIMEOUT_SEC`, `MEMORY_LLM_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` |
| Embeddings | `MEMORY_EMBED_PROVIDER` (`fastembed`, `openai`, `cohere`, `dashscope`), `MEMORY_EMBED_MODEL`, `MEMORY_EMBED_API_BASE`, `MEMORY_EMBED_API_KEY`, `MEMORY_EMBED_DIMENSIONS`, `COHERE_API_KEY`, `DASHSCOPE_API_KEY` |
| Search answers | `MEMORY_RECALL_MAX_RESULT_CHARS` (characters per record in `memory_recall`, default 6000; `memory_get` returns the whole record), `MEMORY_FLAG_INSTRUCTIONS` (`true`/`false`, default `true`: records that address the agent are marked `untrusted_instructions` and the answer carries a `notice`) |

**Precedence: a value set in the dashboard > the server's environment / `.env` > the built-in default.** Dashboard values are exported under the same variable names, so TAM's own lookup rules still apply on top of them. For example, `MEMORY_LLM_API_KEY` beats `OPENAI_API_KEY` wherever it is set. Clearing a dashboard value makes the environment value apply again.

* Values live in the `settings` table of `identity.db`. API keys are encrypted with Fernet (`cryptography`). The master key comes from `TAM_TEAM_MASTER_KEY` or, if that is unset, from `<root>/master.key`, created with mode 0600 on first start. The server refuses to start if that file is readable by group or others.
* Secrets never go back to the browser. The page shows only *set / not set*, the source (web / environment / default), and the last 4 characters (only for keys of 12+ characters).
* **Test connection** makes a single `GET` with an 8 s timeout and no redirects: `/models` for OpenAI-compatible, Anthropic and DashScope, `/api/tags` for Ollama, `/v1/models` for Cohere. It reports OK, a status class, "timed out" or "connection failed", and never includes the key or the response body. `fastembed` is local and needs no test.
* **Apply**: saving restarts the workspace worker pool (`WorkerPool.recycle()`). The pool holds its lock while an operation runs, so no running operation is interrupted. New workers start with the settings in their environment. Changing the embedding provider or model makes existing vectors incompatible until each workspace is re-embedded.
* **Gateway features** (onboarding grading and drafts, the report LLM summary) run in the gateway process, not in a worker. They read the same settings with the same precedence on every call and build the provider from that explicit configuration, so a saved LLM key or provider applies to the next request without a restart. The gateway never writes the settings into its own `os.environ`.
* **Backups** copy `identity.db` with the encrypted values but not the master key. Store `master.key` (or `TAM_TEAM_MASTER_KEY`) separately. After a restore with a different key, affected entries show "set, but unreadable". Re-enter them.

## Backups from the dashboard

Backups need exclusive access to every SQLite database (`ServerLease`), and the running server holds that lease. The dashboard therefore shows the offline CLI command instead of running a backup. Stop the server first, then run `tam-team --root <root> backup --out <dir>`.

## Metrics and logs

The codebase has no Prometheus exporter, so there is no `/metrics` endpoint. The gateway keeps in-process counters and writes structured JSON log lines:

* `login` (method, outcome), `admin_action` (action, outcome), `csrf_rejected`;
* `http_request` (route class, method, status, duration) with a latency histogram per route class;
* the existing `memory_call` counters and histogram.

Superadmins see the counters under *Administration → System*.

## Layout, overview pages and look

* **Identity.** The colors, radii and fonts follow totalmemory.dev: a navy base, mint as the main accent and violet for data. Text is Inter and data is JetBrains Mono. Both fonts are SIL OFL 1.1, self-hosted under `static/fonts/` with their licence files, and loaded with CSP `font-src 'self'`.
* **Themes.** There is a dark and a light theme. The default follows `prefers-color-scheme`. The sidebar toggle stores the choice in `localStorage` (`tam-theme`); any storage error is caught, and the page still works without storage. `static/theme.js` applies the theme before first paint. Text contrast meets WCAG AA in both themes, and every control shows a 2px focus ring.
* **Sidebar.** Sections are grouped into Personal / Department / Company / Administration, each with an icon from `static/icons.svg`. The sprite is fetched same-origin and placed inline, so `<use href="#i-…">` works under the CSP. Below 900px the sidebar becomes a drawer. Below 640px tables turn into labelled stacked rows, so a 375px screen never scrolls sideways.
* **Header scope.** The header shows the user's real scope: `Superadmin`, `Company viewer`, `Manager · Engineering`, `Editor · Engineering`, or `Member` when the user has no department.
* **Overview** is the landing page for every role:

| Endpoint (GET) | Who | Content |
|---|---|---|
| `/dashboard/api/overview/me` | everyone | own records per workspace (the personal count is shown only to its owner), own saves over 30 days, last activity, active tokens, recent own changes |
| `/dashboard/api/overview/team/{team_id}` | manager of that team, company viewer, superadmin | members with 30-day save sparklines, members inactive for more than `INACTIVE_DAYS` (14), active record count, recent team records |
| `/dashboard/api/overview/company` | company viewer, superadmin | departments (members, records, 30-day activity, last activity), company-wide 30-day trend |
| `/dashboard/api/overview/system` | superadmin | version, uptime, workers (running / max / busy), active providers with their last test, pending invites, recent audit events, failed sign-ins in the last 24 h by hour |

  The statistics are read from each workspace's `memory.db` in read-only mode (`insights.WorkspaceReader`); no workspace process is started to serve them. Sign-in attempts are kept for 30 days in `login_log`. Provider test results are stored per provider in `provider_checks`.
* **Providers** are shown as cards, one per provider TAM supports (`settings.PROVIDERS`):
    * The *Active provider* select shows the editable fields only for the chosen provider. Every card has a status pill (not configured / configured / tested OK / error, with the time) and its own **Test** button, which posts `{target, provider}` to `/admin/settings/test`.
    * Keys stay masked. Changing a key is an explicit **Replace key** action, and **Remove** returns the setting to its environment value.
    * Pending changes collect in a sticky bar. Saving asks for confirmation, because the workers restart.
* **Administration.**
    * **Users:** search, filters by role, department and status, initials avatars, role chips, and a per-row action menu (change role, invite or reset, disable or enable) with confirmation dialogs. An invite code appears in a dialog with a copy button and its expiry.
    * **Audit log:** filters by actor, action prefix and subject.
* **Common behaviour:** toasts replace the old status line, loading areas show skeletons, empty states offer the next action, times are relative with the exact time on hover, and destructive actions ask for confirmation.

## Extending the dashboard (section registry)

Sections come from `team_memory.sections.SECTIONS`. The built-in **Onboarding** section (`team_memory.learning`, see [TEAM_ONBOARDING.md](TEAM_ONBOARDING.md)) and **Reports** section (`team_memory.reports`, see [REPORTS.md](REPORTS.md)) are registered this way. To add one, append a descriptor:

```python
from team_memory.sections import Capability, Section, register

register(Section(id="forecasts", title="Forecasts", capability=Capability.team_people,
                 script="/forecasts/static/forecasts.js", stylesheet="/forecasts/static/forecasts.css",
                 mount="TamForecasts.mount", order=60, group="department", icon="list"))
```

* `capability` is `authenticated`, `team_people` (manager of at least one team, company viewer, superadmin), `company`, or `superadmin`. Navigation shows only the sections the user qualifies for. Every API still checks permissions itself.
* `group` (`personal`, `department`, `company`, `administration`) defaults from the capability. `icon` names a symbol in `static/icons.svg` (for example `book`, `users`, `spark`) and defaults to `spark`.
* The shell loads `script` and `stylesheet` (same-origin paths only, so the CSP allows them). It then calls the global function named by `mount` as `mount(element, ctx)`. That contract has not changed.
* `ctx` contains `user` (`user_id`, `display_name`, `org_role`, `role_label`, `password_set`), `teams`, `viewableTeams`, `csrf`, `section` and `navigate(id)`. It also has `api(path, {method, body})`, which sends the cookie, JSON and the CSRF header and throws `Error(message)` when the response is not OK. The older helpers `h`, `status`, `busy`, `table`, `when` and `showSecret` still work.
* `ctx.ui` gives a section the dashboard's look without copying CSS. The helpers return DOM nodes built without `innerHTML`:

| Helper | Returns |
|---|---|
| `ui.card({title, subtitle, actions, flush}, ...children)` | a surface card with a header |
| `ui.kpi({label, value, sub, chart, tone})` | a metric tile (`tone`: `accent`, `warn`, `bad`) |
| `ui.table(columns, rows, emptyState)` | a responsive table. Each column is `{title, key \| render(row), numeric, className}` |
| `ui.chip(text, tone, {dot})` | a role or status chip (`accent`, `violet`, `good`, `warn`, `bad`) |
| `ui.toast(message, tone)` | a transient notice (`good`, `bad`, `info`) |
| `ui.modal({title, body, confirm, cancel, tone})` | a native `<dialog>`; resolves `true` on confirm |
| `ui.confirm(title, text, label, tone)` | a destructive-action confirmation |
| `ui.sparkline(values, {height, tone, label})`, `ui.bars(values, {...})` | small inline-SVG charts with an accessible summary |
| `ui.empty({icon, title, text, action})`, `ui.skeleton(lines)`, `ui.load(target, async build)` | empty and loading states |
| `ui.person(name, id)`, `ui.avatar(name, id)`, `ui.time(value)`, `ui.relative(value)`, `ui.icon(name)`, `ui.field(label, control, hint)`, `ui.menu(items)`, `ui.showSecret(title, secret, note)`, `ui.copyButton(value)` | smaller building blocks |

* Server routes for a section use `team_memory.dashboard.session_endpoint(capability)`. It wraps `async def handler(request, actor)`, validates the session and CSRF, maps domain errors to JSON, and adds the security headers. For per-team decisions, call `registry.can_view_team_people(actor, team_id)`. Use `session_credential(request)` when a handler has to call workspace workers. Paths under `/dashboard` and `/learning` skip the Bearer check (`SESSION_PREFIXES` in `app.py`).
