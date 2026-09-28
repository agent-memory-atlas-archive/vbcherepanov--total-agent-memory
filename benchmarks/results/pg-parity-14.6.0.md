# PostgreSQL vs SQLite parity

Seed 20260925, 600 questions, 320 facts; sizes [1000, 10000, 50000].

| metric | SQLite | PostgreSQL |
|---|---|---|
| recall_at_5 | 99.67 | 99.67 |
| recall_at_10 | 100.0 | 100.0 |
| mrr | 0.9887 | 0.9887 |
| ndcg_at_10 | 0.9915 | 0.9915 |

| tier | top-10 Jaccard | Kendall tau |
|---|---|---|
| fused | 1.0 | 1.0 |
| lexical | 1.0 | 1.0 |
| semantic | 1.0 | 1.0 |

| size | save p50/p95 SQLite | save p50/p95 PG | recall p50/p95 SQLite | recall p50/p95 PG |
|---|---|---|---|---|
| 1000 | 16.222/19.399 | 23.357/26.359 | 467.688/570.211 | 462.779/560.053 |
| 10000 | 19.842/25.745 | 23.805/26.281 | 491.793/746.244 | 484.799/602.014 |
| 50000 | 33.009/38.81 | 25.594/28.499 | 485.66/621.859 | 552.464/670.572 |

Acceptance: {"latency_ok": true, "lexical_jaccard": 1.0, "lexical_ok": true, "p95_recall_ratio_at_10000": 0.81, "recall_at_10_drop_pp": 0.0, "recall_ok": true}
