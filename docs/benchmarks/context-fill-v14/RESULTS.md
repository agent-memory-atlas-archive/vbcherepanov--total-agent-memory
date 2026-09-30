# Context budget fill (14.7.0)

`memory_recall(mode="context", fill_budget=true)` searches up to 100 hits deep and keeps whole
records in rank order while they fit into `context_max_chars`, skipping a record that does not fit
so a shorter later one can use the room. The plain context mode takes the top `limit` hits. This
page measures how often the context a reader would get holds the evidence for the question.

No LLM is involved: the metric is retrieval coverage of the assembled context, not answer accuracy.

## Setup

- Script: [`ctx_eval.py`](ctx_eval.py), run through the benchmark TAM worker
  (`docs/benchmarks/tam_bench_common/tam_worker.py`), which calls the same `recall.search` and the
  same `memory_core.context_budget.fill_budget` as the server. One fresh store per conversation
  (LoCoMo) or per question (LongMemEval), one record per turn.
- **LoCoMo**, conversations 0-2: 383 questions of categories 1-4 with `evidence` turns
  (category 5, adversarial, has none). Budgets 4,000 and 8,000 characters.
- **LongMemEval-S**, the first 100 questions: 94 have turns flagged `has_answer` (the rest are
  skipped). Turns longer than 2,000 characters are split on line boundaries. Budgets 16,000 and
  40,000 characters.
- LoCoMo conversations 0-2 are the development split of [qa-v14](../qa-v14/RESULTS.md), on which
  earlier retrieval settings were chosen. The LongMemEval-S questions are the first 100 in file
  order, not that page's development split (the 100 smallest SHA-256 ids), so some of them belong to
  its held-out set. The only setting chosen from these runs is the neighbours default (from the
  LoCoMo rows below).
- Variants, per budget:
  - `base`: the top 10 hits, whole, in rank order, stopping at the first one that overflows.
  - `fill`: budget fill over a 100-hit ranked list.
  - `fill_r1`: as `fill`, each hit together with one session neighbour on each side.
- Metrics: `any` = at least one gold turn in the context; `all` = every gold turn; mean records and
  characters used.
- TAM at `de53c19` plus the 14.7.0 changes, default multilingual embedding
  (`paraphrase-multilingual-MiniLM-L12-v2`), Python 3.13.5, Apple M2 Max.

Reproduce (the LoCoMo and LongMemEval-S files go to `benchmarks/data/` as for the other QA runs):

```bash
.venv/bin/python docs/benchmarks/context-fill-v14/ctx_eval.py --kind locomo --variants base,fill,fill_r1 \
  --cross-rerank on --work-root "$(mktemp -d)" --output /tmp/locomo_ce_on.json
.venv/bin/python docs/benchmarks/context-fill-v14/ctx_eval.py --kind lme --variants base,fill \
  --cross-rerank on --work-root "$(mktemp -d)" --output /tmp/lme_ce_on.json
```

Raw per-question rows: [`raw/`](raw/).

## LoCoMo (383 questions)

| Variant | Cross-encoder | any @4,000 | all @4,000 | any @8,000 | all @8,000 | records @8,000 |
|---|---|---:|---:|---:|---:|---:|
| base | on | 0.815 | 0.710 | 0.815 | 0.710 | 10.0 |
| **fill** | on | **0.867** | **0.752** | **0.890** | 0.776 | 50.5 |
| fill_r1 | on | 0.849 | 0.742 | 0.885 | **0.783** | 44.9 |
| base | off | 0.674 | 0.567 | 0.674 | 0.567 | 10.0 |
| fill | off | 0.807 | 0.687 | 0.883 | 0.770 | 50.9 |
| fill_r1 | off | 0.713 | 0.611 | 0.807 | 0.692 | 46.0 |

The top 10 LoCoMo turns take about 1,950 characters, so `base` leaves most of either budget empty.
Neighbours cost room that further ranked hits would have used: `fill_r1` is below `fill` in all
but one cell, and far below it without the cross-encoder. That is why neighbours default to 0 when
`fill_budget` is on.

## LongMemEval-S (94 questions, cross-encoder on)

| Variant | any @16,000 | all @16,000 | any @40,000 | all @40,000 | records @40,000 |
|---|---:|---:|---:|---:|---:|
| base | 0.957 | 0.766 | 0.957 | 0.766 | 10.0 |
| **fill** | **1.000** | **0.830** | **1.000** | **0.904** | 49.8 |

| Question type | n | base all | fill all @16,000 | fill all @40,000 |
|---|---:|---:|---:|---:|
| multi-session | 30 | 0.333 | 0.500 | 0.733 |
| single-session-user | 64 | 0.969 | 0.984 | 0.984 |

`fill` lost no question that `base` covered, on either metric. The gain is on multi-session
questions, whose evidence sits in several sessions and does not fit into ten hits.
