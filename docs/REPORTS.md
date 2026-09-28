# Activity reports

`memory_report` answers "what happened in this project today / this week / this month / ever?" from the records already in memory. The same report is available as an MCP tool, a `tam report` command, a `/report` skill for Claude Code and Codex, and a **Reports** page on the team server dashboard.

The report is deterministic: it is built with SQL from stored records, with no LLM call. The same data, clock and arguments give the same JSON and Markdown byte for byte. An LLM-written summary paragraph is optional and off by default.

## Periods

| `period` | Window | Previous period used for deltas |
|---|---|---|
| `day` | the local calendar day, 00:00 to 00:00 | the day before |
| `week` | the ISO week, Monday 00:00 to the next Monday | the week before |
| `month` | the calendar month | the month before |
| `all` | local midnight of the day of the first record, to the end of today | none (no deltas) |
| `custom` | `since` to `until` | the equal-length span right before `since` |

* `offset` (`0`, `-1`, `-2`, …) shifts `day` / `week` / `month` into the past: `period=week, offset=-1` is last week.
* `since` / `until` accept `YYYY-MM-DD` (whole days, `until` inclusive) or an ISO-8601 date-time (exact instant, `until` exclusive). Without `until` the window ends tonight at midnight.
* Boundaries are computed in a timezone: the `tz` argument (IANA name, e.g. `Europe/Berlin`), else `MEMORY_REPORT_TZ`, else `TZ`, else the system zone from `/etc/localtime`, else UTC. Days are real local days, so a day that crosses a DST change lasts 23 or 25 hours, and a record saved at 23:30 UTC belongs to the next day in Berlin.
* A window that has not ended yet is marked `in_progress`. Deltas compare it with the whole previous period.

## Sections

| Section | Source | Notes |
|---|---|---|
| Summary | `knowledge`, `errors`, `session_summaries`, `observations`, `sessions` | records by type; **new** (written in the window), **updated** (a new record that replaced an older one), **superseded** (records replaced by one written in the window), **re-confirmed** (an older record saved again), sessions, active days |
| Changes | the same numbers for the previous period | `delta` and `change_pct` (`null` when the previous value is 0) |
| Key decisions | `knowledge.type='decision'` | ordered by importance, then time; `why` is the stored context, or `Selected X. <rationale>` for `save_decision` records |
| Solutions and fixes | `knowledge.type='solution'` | chronological |
| Errors | `errors` (`self_error_log`, `learn_error`) | severity, status, root cause and fix |
| Error patterns | `pattern:` tag, else the category | count in the window, all-time count up to the window end, `recurring` when seen more than once |
| Lessons and rules | `knowledge.type='lesson'`, rules learned from repeated errors | |
| Open tasks and next steps | `session_summaries` (`session_end`) | `next_steps`, `open_questions` and `pitfalls`, deduplicated case- and space-insensitively; `picked_up` means a later session loaded the summary |
| Most touched files | `observations.files_affected`, `file:` tags on records and errors | |
| Entities and technologies | the knowledge graph (`knowledge_nodes` → `graph_nodes`) | graph mirrors of record types, events and `file:` nodes are left out; so is the report's own project node |
| Tags | record tags | system tags (`file:`, `pattern:`, `structured`, …) are left out |
| Timeline | everything above, grouped by local day | up to 5 entries per day plus a count of the rest |
| Contributors | team server only: `tam_history` | saves, updates, deletes and confirmations per person |

Every item carries `sources`: `{"kind": "knowledge", "id": 12}` (fetch it with `memory_get(ids=[12])`), `error`, `rule`, `session_summary` or `observation`. In Markdown they appear as `#12`, `err#5`, `rule#3`, `summary:<id>` and `obs#7`. Lists are capped by `limit` (default 20, at most 200); each list reports its `total`, and Markdown says "showing 20 of 45" when it is cut.

Ordering is total (time, workspace, id as tie-breakers), so two runs over the same data never reorder items. An empty window gives `"empty": true`, zero counts, and a Markdown line saying nothing was recorded.

## MCP tool

```json
{"name": "memory_report", "arguments": {"project": "billing-api", "period": "week"}}
```

| Argument | Default | Meaning |
|---|---|---|
| `project` | all projects | project name |
| `period` | `week` | `day`, `week`, `month`, `all`, `custom` |
| `since`, `until` | – | `custom` only |
| `offset` | `0` | `day` / `week` / `month` only, `≤ 0` |
| `tz` | system zone | IANA timezone |
| `limit` | `20` | items per section |
| `format` | `markdown` | `markdown` returns the report text; `json` returns `{"report": {...}, "saved_to": ...}` |
| `include_llm_summary` | `false` | add a paragraph from the configured LLM |
| `save` | `false` | also write the Markdown to `<memory dir>/reports/<project>/<period>-<date>.md` |

Saved files are named `day-2026-09-25.md`, `week-2026-09-21.md` (the Monday), `month-2026-09-01.md`, `all-2026-09-25.md` (the last day) or `custom-2026-09-01_2026-09-15.md`, under `all-projects/` when no project is given. They are written atomically with mode 0600. Invalid arguments (for example `period=custom` without `since`, an unknown timezone or `until` before `since`) return an MCP error result.

The tool is annotated as idempotent and not read-only, because `save=true` writes a file.

## Optional LLM summary

`include_llm_summary=true` sends a compact digest of the finished report (titles, metrics, patterns, open items; no full record bodies) to the provider configured with `MEMORY_LLM_*` (`llm_provider.make_provider("auto")`). The prompt treats record text as untrusted data. The result goes to `llm_summary` and appears as a quote at the top of the Markdown. When no LLM is configured, the call fails or times out (60 s), or it returns nothing, the report is still complete and `llm_summary_error` explains why. Empty periods never call the LLM.

## CLI

```bash
tam report --project billing-api --period week
tam report --period day --offset -1 --tz Europe/Berlin
tam report --period custom --since 2026-09-01 --until 2026-09-15 --format json --out sept.json
tam report --project billing-api --period month --save
```

`tam report` opens `<memory dir>/memory.db` read-only (the memory dir is `--memory-dir`, `TAM_MEMORY_DIR` or `~/.tam`) and does not start the MCP server. Flags: `--project`, `--period`, `--since`, `--until`, `--offset`, `--tz`, `--limit`, `--format md|json`, `--out FILE`, `--save`, `--llm-summary`. Exit codes: `0` success, `1` missing or unreadable database, `2` invalid arguments.

## `/report` skill

`skills/report/SKILL.md` is installed with the other skills (`tam setup register`, `install.sh`, `install.ps1`, `install-codex.*`) into `~/.claude/skills/report` for Claude Code and `~/.codex/skills/report` plus `~/.agents/skills/report` for Codex. It maps phrasing such as "report for today", "last week", "this month", "all time for project X" or "from 2026-09-01 to 2026-09-15" to tool arguments, calls `memory_report`, presents the headline numbers, main decisions, recurring errors and open next steps briefly, and offers to save the report.

## Team server

On the team server the same tool name takes a scope:

| `scope` | Who may request it | Workspaces read |
|---|---|---|
| `personal` (default) | everyone | the caller's own personal workspace |
| `team` + `team_id` | the department's managers, company viewers, superadmins (`Registry.can_view_team_people`) | that department's workspace |
| `company` | company viewers and superadmins | every department plus shared memory |

Personal memory is private: it is read only for its owner and never enters a department or company report, superadmins included. Other users get `Forbidden`. Workspace databases are opened read-only, as for the overview statistics; no workspace process starts. Department and company reports add a **Contributors** table. In company reports each item names its workspace (`team:eng #12`), so it can be opened with `memory_get(scope={"kind": "team", "team_id": "eng"}, id=12)`.

The dashboard's **Reports** page (Personal group, visible to everyone) has a scope selector (own memory, the departments the user may view, the company for oversight roles), period tabs (Today, This week, This month, All time, Custom) with back and forward buttons to step through previous periods, a project filter and a **Download .md** button. The page sends the browser's timezone. Its API:

| Endpoint (GET, dashboard session) | Returns |
|---|---|
| `/reports/api/options` | which scopes the user may choose |
| `/reports/api/report?scope=…&period=…` | `{"report": …}` (the JSON report) |
| `/reports/api/report.md?scope=…&period=…` | the Markdown as an attachment |

The optional LLM summary on the team server uses the LLM provider settings from the dashboard (*Administration → Provider settings*), with the same precedence as the workspace workers: dashboard value > the server's environment / `.env` > built-in default. The gateway resolves them on every summary request and builds the provider from that explicit configuration (`team_memory.gateway_llm.GatewayLLM`). It never writes them into `os.environ`, and a saved change applies to the next report without a restart.

## Observability

Each build logs one JSON line (`event: report_built` with period, project, scope, record count, emptiness, skipped timestamps and duration) and updates the in-process counters `report_built`, `report_failed`, `report_build_ms` (with latency buckets), `report_llm_summary_ok` and `report_llm_summary_failed`. Timestamps that cannot be parsed are skipped and counted in the log line.
