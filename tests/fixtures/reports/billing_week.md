# Activity report: billing-api

- **Period:** ISO week 2026-W39 (2026-09-21 – 2026-09-27) (7 days, Europe/Berlin) · in progress
- **Compared with:** ISO week 2026-W38 (2026-09-14 – 2026-09-20)
- **Generated:** 2026-09-25 12:00 · built from stored records without an LLM

## Summary

| Metric | This period | Previous | Change |
|---|--:|--:|--:|
| Records written | 7 | 1 | +6 (+600.0%) |
| New records | 6 | 1 | +5 (+500.0%) |
| Updated (replaced a record) | 1 | 0 | +1 (new) |
| Superseded | 1 | 0 | +1 (new) |
| Re-confirmed | 1 | 0 | +1 (new) |
| Decisions | 2 | 1 | +1 (+100.0%) |
| Solutions | 1 | 0 | +1 (new) |
| Lessons and rules | 2 | 0 | +2 (new) |
| Errors | 3 | 1 | +2 (+200.0%) |
| Sessions | 3 | 1 | +2 (+200.0%) |
| Active days | 3 | 2 | +1 (+50.0%) |
| Open items | 5 | 0 | +5 (new) |

Records by type: convention 1 · decision 2 · fact 2 · lesson 1 · solution 1

## Key decisions (2)

- **Queue invoice e-mails in Redis streams** — why: Selected Redis streams. Already deployed; consumer groups give at-least-once delivery. · critical · 2026-09-24 18:45 · `#9`
- **Move invoice numbering to a PostgreSQL sequence** — why: Gapless numbering is a legal requirement; app-side counters raced under load. · high · 2026-09-21 11:30 · `#3`

## Solutions and fixes (1)

- **Fix duplicate invoice numbers with nextval in the same transaction** — Two workers read MAX(number)+1. · 2026-09-21 13:05 · `#4`

## Errors and lessons

### Error patterns

| Pattern | This period | All time | Recurring | First seen | Last seen | Errors |
|---|--:|--:|---|---|---|---|
| `duplicate-number` | 2 | 3 | yes | 2026-09-18 12:00 | 2026-09-24 11:10 | `err#2` `err#3` |
| `pdf-render` | 1 | 1 | no | 2026-09-24 11:40 | 2026-09-24 11:40 | `err#4` |

### Errors (3)

- **[high · open] IntegrityError: duplicate invoice number** — root cause: concurrent writers · fix: Use a database sequence · 2026-09-21 12:40 · `err#2`
- **[high · open] IntegrityError: duplicate invoice number** — root cause: concurrent writers · fix: Use a database sequence · 2026-09-24 11:10 · `err#3`
- **[medium · resolved] WeasyPrint crashed on CSS grid** — root cause: concurrent writers · fix: Use a database sequence · 2026-09-24 11:40 · `err#4`

### Lessons and rules (2)

- **Run the numbering migration with lock_timeout set** — The first attempt blocked writes. · 2026-09-23 16:00 · `#5`
- **Never compute invoice numbers in the application** — from duplicate-number · 2026-09-24 11:11 · `rule#1`

## Open tasks and next steps (5)

From session summaries (`session_end`). "picked up" means a later session loaded it.

- [ ] add a numbering load test · 2026-09-24 20:30 · mentioned 2× · picked up · `summary:sum-mon-1` `summary:sum-thu-2`
- [ ] Wire the Redis consumer into the mailer · 2026-09-24 20:30 · picked up · `summary:sum-thu-2`
- [ ] Backfill September numbers · 2026-09-21 20:00 · `summary:sum-mon-1`
- **Question:** Do we need per-country numbering? · 2026-09-24 20:30 · picked up · `summary:sum-thu-2`
- **Pitfall:** Do not backfill during business hours · 2026-09-21 20:00 · `summary:sum-mon-1`

## Most touched files (4)

| File | Touches | Sources |
|---|--:|---|
| `src/invoices/numbering.py` | 3 | `obs#1` `#4` `err#2` |
| `src/pdf/render.py` | 2 | `#2` `err#4` |
| `migrations/0042_invoice_seq.sql` | 1 | `obs#1` |
| `src/invoices/export.py` | 1 | `err#3` |

## Entities and technologies (2)

| Entity | Type | Records | Sources |
|---|---|--:|---|
| `PostgreSQL` | technology | 2 | `#3` `#5` |
| `Redis` | technology | 1 | `#9` |

Tags: `invoices` (2) · `pdf` (2) · `postgres` (2) · `redis` (1)

## Timeline

### 2026-09-21 Mon — 3 records · 1 error · 1 session summary · 1 session

- 10:15 fact: Invoice PDFs are rendered by WeasyPrint 61 `#2`
- 11:30 decision: Move invoice numbering to a PostgreSQL sequence `#3`
- 12:40 error: IntegrityError: duplicate invoice number `err#2`
- 13:05 solution: Fix duplicate invoice numbers with nextval in the same transaction `#4`

### 2026-09-23 Wed — 1 record · 1 session

- 16:00 lesson: Run the numbering migration with lock_timeout set `#5`

### 2026-09-24 Thu — 3 records · 2 errors · 1 session summary · 2 sessions

- 00:30 convention: Money amounts are stored as integer cents `#6`
- 11:10 error: IntegrityError: duplicate invoice number `err#3`
- 11:40 error: WeasyPrint crashed on CSS grid `err#4`
- 12:00 fact: Invoice PDFs are rendered by WeasyPrint 62 `#8`
- 18:45 decision: Queue invoice e-mails in Redis streams `#9`

---

Drill down: `memory_get(ids=[9, 3, 4, 5])` returns the full records.
`err#` = error log, `rule#` = learned rule, `summary:` = session summary, `obs#` = observation.
