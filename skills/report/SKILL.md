---
name: report
description: >
  Activity report from total-agent-memory for a project and period. Use when the user
  types /report or asks "report for today", "what did we do this week", "weekly /
  monthly summary", "status report", "retro for project X", "all-time report",
  "report for last week", or a report for a department or the whole company on the
  team server. Calls the memory_report MCP tool and presents the result.
argument-hint: "[today|week|month|all|YYYY-MM-DD..YYYY-MM-DD] [project]"
---

# /report — activity report from memory

The report is built by the `memory_report` tool (your client may prefix it, e.g.
`mcp__memory__memory_report`). It is deterministic: numbers, decisions, fixes,
errors, open next steps, files and a daily timeline come straight from stored
records, each with source IDs. If no `memory_report` tool is available, say the
memory server is not connected (or is older than this skill) and stop.

## 1. Read the request

Take the period and project from `$ARGUMENTS` or the user's words:

| The user says | Arguments |
|---|---|
| today, for the day, daily | `period: "day"` |
| yesterday | `period: "day", offset: -1` |
| this week, weekly (default when nothing is said) | `period: "week"` |
| last week | `period: "week", offset: -1` |
| this month, monthly | `period: "month"` |
| last month | `period: "month", offset: -1` |
| all time, ever, since the start | `period: "all"` |
| from 2026-09-01 to 2026-09-15, since Monday, last 10 days | `period: "custom", since: "YYYY-MM-DD", until: "YYYY-MM-DD"` (until is inclusive; omit it for "until today") |

- Project: "for project X", "in X", or the current repository/folder name when the
  user says "this project". Omit `project` for "all projects".
- Timezone: pass `tz` (IANA name, e.g. `Europe/Berlin`) only when the user names a
  zone or city; otherwise the server uses the machine's zone.
- Team server only: "department X report" → `scope: "team", team_id: "X"`;
  "company report" → `scope: "company"`; otherwise `scope: "personal"`. A
  `Forbidden` error means the user lacks the department head / company viewer
  role: say so plainly.

## 2. Call the tool

Call `memory_report` with those arguments and `format: "markdown"`. Add
`include_llm_summary: true` only when the user asks for a written summary or
narrative; it needs a configured LLM and the report is complete without it.

## 3. Present it concisely

Do not paste the whole report unless asked. Give:

1. One line with the period (as the report labels it) and the headline numbers
   with their change against the previous period (records, decisions, errors,
   active days).
2. The 3–5 most important decisions with their WHY, and the main fixes.
3. Recurring error patterns (count this period vs all time) and lessons learned.
4. Open next steps and pitfalls that were not picked up yet.
5. Anything notable in files, technologies or the timeline (e.g. one very busy day).

Keep the `#id` references so the user can ask for details; fetch full records
with `memory_get(ids=[...])` when they do (on the team server: `memory_get` with
the scope and `id`). If the report says there was no activity, say so and offer
a longer period.

## 4. Offer to save

End by offering to save it. On yes, call `memory_report` again with the same
arguments and `save: true`, then show the returned file path
(`<memory dir>/reports/<project>/<period>-<date>.md`). The same report is
available from a terminal: `tam report --project X --period week [--format json] [--out FILE]`.
