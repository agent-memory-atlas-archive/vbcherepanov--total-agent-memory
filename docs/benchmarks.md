# Benchmarks

Retrieval and end-to-end results for total-agent-memory, with the commands
that reproduce them. The retrieval tables below were measured for v13.0.0
(2026-08-27) with the default `fast` profile. Later measurements are in the
per-study reports listed under [Studies since v14](#studies-since-v14).

- [Reproducing the benchmarks](#reproducing-the-benchmarks)
- [Retrieval: LoCoMo, BEAM, LongMemEval](#retrieval-benchmarks)
- [End-to-end accuracy](#on-end-to-end-accuracy-numbers)
- [Negative controls](#do-the-retrieval-numbers-mean-anything--negative-controls)
- [Latency profile](#latency-profile)
- [Comparison with published numbers of other systems](#comparison-with-published-numbers-of-other-systems)
- [Studies since v14](#studies-since-v14)

## Reproducing the benchmarks

Use a source checkout with the development environment from the
[README](../README.md#running-the-tests). The public datasets are not in the
repository (`benchmarks/data/` is gitignored because each corpus carries its
own licence). Put them where the runners expect them:

| Benchmark | Source | Expected path |
|---|---|---|
| LoCoMo | [snap-research/locomo](https://github.com/snap-research/locomo) | `benchmarks/data/locomo/data/locomo10.json` |
| LongMemEval | [xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) | `benchmarks/data/longmemeval_s.json` |
| BEAM | [mohammadtavakoli78/BEAM](https://github.com/mohammadtavakoli78/BEAM) | parquet shards in `benchmarks/data/beam/` |

Then run the retrieval benchmarks. None of them needs an API key:

```bash
.venv/bin/python benchmarks/locomo_bench.py --wipe
.venv/bin/python benchmarks/longmemeval_bench.py --modes store
.venv/bin/python benchmarks/beam_bench.py --scale 100K --wipe
```

The end-to-end (LLM-judged) runners, `benchmarks/locomo_bench_llm.py`,
`benchmarks/locomo_qa.py`, `benchmarks/longmemeval_qa.py` and
`benchmarks/crossgrade_mem0.py`, need an API key for the answering and judging
models. Each study report below has a "Reproduce" or "How to reproduce"
section with its exact commands.

## Retrieval benchmarks

Everything below is **retrieval**: does the memory surface the passage that
contains the answer, in the top-K? That is the part this project owns —
answer quality is bounded above by it, and it can be graded with no LLM in
the loop, which makes the numbers deterministic, free, and reproducible on
your machine.

Two things to read them honestly:

- These are the **default `fast` profile** — FastEmbed, no reranker, no LLM
  anywhere in the path. That is what you get after `install.sh`, not a tuned
  configuration.
- Every runner passes `record_usage=False`. `Recall.search` normally bumps
  `recall_count`, and the scorer adds `recall_boost = min(0.3, recall_count ×
  0.05)` — so before v13, each re-run against the same database scored higher
  than the last, partly measuring its own history. A clean run and a re-run
  are now byte-identical.

### LoCoMo — [snap-research/locomo](https://github.com/snap-research/locomo)

1,536 gradable questions across 10 long-running conversations (5,882 turns
ingested), plus 446 adversarial questions scored separately.

| Category | N | R@1 | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|---:|
| single-hop | 282 | 0.202 | 0.500 | 0.638 | 0.332 |
| temporal | 321 | 0.411 | **0.689** | 0.735 | 0.524 |
| multi-hop | 92 | 0.163 | 0.413 | 0.435 | 0.256 |
| open-domain | 841 | 0.363 | 0.633 | 0.712 | 0.479 |
| **overall** | **1,536** | **0.331** | **0.607** | **0.687** | **0.448** |

Latency p50 **18.2 ms**, p95 55.4 ms. Temporal is the strongest category —
the bi-temporal knowledge graph earns its keep. Multi-hop is the weakest and
is the v13.1 target.

Reproduce: `python benchmarks/locomo_bench.py --wipe` →
[`benchmarks/results/v13-locomo-retrieval.json`](../benchmarks/results/v13-locomo-retrieval.json)

### BEAM — [Beyond a Million Tokens](https://github.com/mohammadtavakoli78/BEAM), ICLR 2026

BEAM is the benchmark that starts where context windows stop: conversations of
100K / 500K / 1M tokens (a separate 10M set goes further), probed across ten
distinct memory abilities. Scored here against each probe's `source_chat_ids`.

**Scale 100K** — 20 conversations, 5,732 messages, 355 gradable probes:

| Ability | N | R@1 | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|---:|
| contradiction_resolution | 40 | 0.700 | **1.000** | 1.000 | 0.824 |
| temporal_reasoning | 40 | 0.475 | **0.975** | 1.000 | 0.689 |
| knowledge_update | 40 | 0.550 | **0.925** | 0.950 | 0.719 |
| multi_session_reasoning | 40 | 0.375 | 0.675 | 0.850 | 0.486 |
| information_extraction | 40 | 0.400 | 0.625 | 0.725 | 0.503 |
| summarization | 36 | 0.167 | 0.444 | 0.556 | 0.267 |
| preference_following | 39 | 0.077 | 0.282 | 0.410 | 0.169 |
| event_ordering | 40 | 0.025 | 0.150 | 0.200 | 0.074 |
| instruction_following | 40 | 0.025 | 0.075 | 0.150 | 0.054 |
| **overall** | **355** | **0.313** | **0.575** | **0.651** | **0.423** |

Latency p50 **17.7 ms**. The shape is the useful part: contradiction
resolution, temporal reasoning and knowledge update are effectively solved,
while `instruction_following` and `event_ordering` are near-zero — those probes
ask *whether a stated instruction was followed* or *in what order things
happened*, and semantic similarity to the question does not find the message
where the instruction was given. Retrieval is the wrong primitive there, and
that is the roadmap item.

**Scale 500K** — 35 conversations, 38,058 messages, 629 gradable probes:

| Ability | N | R@1 | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|---:|
| contradiction_resolution | 70 | 0.714 | **0.943** | 0.971 | 0.828 |
| knowledge_update | 69 | 0.464 | **0.855** | 0.899 | 0.617 |
| temporal_reasoning | 70 | 0.500 | **0.786** | 0.871 | 0.625 |
| multi_session_reasoning | 70 | 0.357 | 0.614 | 0.729 | 0.470 |
| information_extraction | 70 | 0.271 | 0.443 | 0.571 | 0.354 |
| preference_following | 70 | 0.071 | 0.300 | 0.471 | 0.168 |
| summarization | 70 | 0.100 | 0.286 | 0.414 | 0.174 |
| instruction_following | 70 | 0.029 | 0.157 | 0.257 | 0.086 |
| event_ordering | 70 | 0.014 | 0.029 | 0.186 | 0.042 |
| **overall** | **629** | **0.280** | **0.490** | **0.596** | **0.373** |

**Scale 1M** — 35 conversations, 74,630 messages, 625 gradable probes:

| Ability | N | R@1 | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|---:|
| knowledge_update | 70 | 0.529 | **0.886** | 0.929 | 0.677 |
| contradiction_resolution | 70 | 0.686 | **0.871** | 0.914 | 0.772 |
| temporal_reasoning | 70 | 0.371 | 0.686 | 0.800 | 0.508 |
| multi_session_reasoning | 70 | 0.214 | 0.429 | 0.600 | 0.315 |
| information_extraction | 70 | 0.157 | 0.371 | 0.500 | 0.250 |
| summarization | 66 | 0.015 | 0.288 | 0.515 | 0.147 |
| preference_following | 69 | 0.029 | 0.246 | 0.406 | 0.134 |
| event_ordering | 70 | 0.000 | 0.157 | 0.329 | 0.069 |
| instruction_following | 70 | 0.029 | 0.086 | 0.200 | 0.061 |
| **overall** | **625** | **0.227** | **0.448** | **0.578** | **0.327** |

### How it scales, and what that exposed

| Scale | Messages | R@5 | search p50 | ingest |
|---|---:|---:|---:|---:|
| 100K | 5,732 | 0.575 | 17.7 ms | 25.6 msg/s |
| 500K | 38,058 | 0.490 | 58.5 ms | 10.8 msg/s |
| 1M | 74,630 | **0.448** | **411.5 ms** | **5.0 msg/s** |

Recall decays gracefully — 13× the haystack costs 12.7 points of R@5, and the
abilities that hold up (knowledge update, contradiction resolution) hold up at
every scale. The two curves that do *not* decay gracefully are the interesting
part, and they have separate causes.

**Ingest — found and fixed.** Throughput fell 5× across the three scales on
identical code. The cause was ours: `graph/auto_link.py` runs on every save and
constructed a fresh `ConceptExtractor` each time. The node-name cache lives on
the instance, so it was thrown away immediately and the whole `graph_nodes`
table was re-read per write — 1,000 saves triggered 1,000 full table reads
(~139 million rows at the 139k nodes this ingest reaches). Fixed in v13.0.1;
counting reads rather than timing makes the check load-independent, and it is
now **1** read per 1,000 saves. **The ingest column above was measured before
that fix** and is kept as the record of the problem.

**Search — open.** p50 grew 7× between 500K and 1M for 2× the data.
`Store._binary_search` loads the binary vectors of every active record into
numpy on each query, so search is linear in store size. That is a different
problem from the ingest one and is not fixed; an ANN index over the binary
vectors is the obvious answer and has not been built yet. Stated rather than
buried, because 411 ms is a real number a user would feel.

Reproduce: `python benchmarks/beam_bench.py --scale 100K --wipe` →
[`v13-beam-100K.json`](../benchmarks/results/v13-beam-100K.json) ·
[`v13-beam-500K.json`](../benchmarks/results/v13-beam-500K.json) ·
[`v13-beam-1M.json`](../benchmarks/results/v13-beam-1M.json)

### LongMemEval — [xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)

470 questions across six question types, re-measured for v13 **through the
product**: each question's haystack is ingested into a real `Store` and queried
with `Recall.search`, the same path an agent takes.

| Question type | Count | R@5 (recall_any) |
|---|---:|---:|
| knowledge-update | 72 | **100.0%** |
| multi-session | 121 | **98.3%** |
| single-session-user | 64 | 95.3% |
| single-session-assistant | 56 | 94.6% |
| temporal-reasoning | 127 | 92.9% |
| single-session-preference | 30 | 80.0% |
| **total** | **470** | **95.1%** |

Also `recall_all@5` 85.7% (every required fragment, not just one), NDCG@5
88.9%, **27.6 ms** per query.

> **This replaces the 96.2% we published before**, and the difference matters
> more than the 1.1 points. Until v13 this runner used its own self-contained
> BM25 / RRF / MMR / CrossEncoder stack, so the number described *an
> algorithm*, not this software. `--modes store` drives the shipping path and
> is now the default. The old modes remain for ablations.
>
> For reference on the same set, Mastra "Observational" reports 95.0% and
> Supermemory 85.4% — both cloud services.

Reproduce: `python benchmarks/longmemeval_bench.py --modes store` →
[`evals/longmemeval-2026-08-27-v13-store.json`](../evals/longmemeval-2026-08-27-v13-store.json)

### On end-to-end accuracy numbers

Systems in this space usually publish LoCoMo **accuracy** — a generator answers
from the retrieved context and an LLM judges it. We publish it too, with the
two caveats that make it meaningful.

**One LLM-judged run is a sample, not a measurement.** Temperature 0 does not
make the API deterministic and OpenAI documents `seed` as best-effort, so the
runner takes `--seed` and we report three runs:

| Category | N | mean | min | max | spread |
|---|---:|---:|---:|---:|---:|
| single-hop | 282 | 0.366 | 0.358 | 0.372 | 0.014 |
| temporal | 321 | 0.426 | 0.424 | 0.427 | 0.003 |
| multi-hop | 96 | 0.292 | 0.281 | 0.302 | **0.021** |
| open-domain | 841 | 0.570 | 0.567 | 0.573 | 0.006 |
| adversarial | 446 | **0.904** | 0.899 | 0.908 | 0.009 |
| **overall (no adversarial)** | 1,540 | **0.486 ± 0.002** | 0.484 | 0.488 | 0.005 |
| **overall (all)** | 1,986 | **0.579 ± 0.002** | 0.578 | 0.582 | 0.004 |

gpt-4o generator, gpt-4o-mini judge, seeds 1/2/3. Retrieval was **byte-identical
across all three** — only generation and judging vary.

**The judge needed two guards, and they point opposite ways.**

*Refusals scored as correct answers.* On ~100 of the 1,540 non-adversarial
questions per run, the judge answered YES to *"Not mentioned in the
conversation."* against golds like `Sweden`, `June 2023`, `Single` — F1 exactly
0.00. Almost certainly the adversarial rule bleeding across, since the judge is
told to accept a refusal when the gold also indicates no information. Per
category the inflation runs **3.2 pp (open-domain) to 14.3 pp (temporal)**.

*Hallucinations scored as correct abstentions.* **99.6% of LoCoMo's adversarial
golds are the empty string.** The judge accepts almost any fluent answer against
an empty reference, so 27–30 invented answers per run scored correct —
inflating the one category we used to lead on.

Both are rules rather than judgements — on categories 1–4 the gold *is* a fact,
so a refusal cannot be right; with an empty gold, only a refusal can be — so
both now run deterministically at judging time. **Effect: no-adv 0.551 → 0.486,
adversarial 0.966 → 0.904, all 0.645 → 0.579. The table above is corrected.**

How noisy is the rest? Aligning all 1,986 questions across the three seeds:

| | share |
|---|---:|
| generator's answer differed between seeds | 12.5% |
| judge's verdict differed | 5.1% |
| **judge flipped on an identical answer** | **2.7%** |

The aggregate holds within ±0.005 because those flips roughly cancel, not
because the instrument is precise. Quoting one run to three decimals — as we
did before — is not supported by the data.

Not comparable to the 90%+ figures some competitors publish: different
generators, judges, prompts and question subsets. And on this evidence, an
unguarded LLM judge can be worth six points on its own. The retrieval numbers
above remain our primary metric because they are checkable without an API key.

[`benchmarks/results/v13-locomo-llm-3seeds.json`](../benchmarks/results/v13-locomo-llm-3seeds.json) ·
Runner: [`benchmarks/locomo_bench_llm.py`](../benchmarks/locomo_bench_llm.py)

### Do the retrieval numbers mean anything? — negative controls

A retrieval score with no floor under it is not a claim. Every LoCoMo run now
scores three degenerate baselines on the same questions:

| Baseline | R@1 | R@5 | R@10 |
|---|---:|---:|---:|
| random — ten turns from the same conversation | 0.001 | 0.012 | 0.023 |
| first — the ten earliest turns | 0.000 | 0.023 | 0.039 |
| recency — the ten most recent turns | 0.001 | 0.003 | 0.011 |
| **the pipeline** | **0.331** | **0.607** | **0.687** |

**27× the best degenerate baseline.** The controls run in the same pass as the
metric, so the floor ships with the number rather than living in a script
somebody stops running.

### Latency profile

```
  p50 (warm)   ▌ 0.065 ms
  p95 (warm)   ▌▌ 2.97 ms
  LoCoMo       ▌▌▌ 18.2 ms/query    ← full hybrid retrieval over 5,882 records
  BEAM 100K    ▌▌▌ 17.7 ms/query    ← over 5,732 messages
  LongMemEval  ▌▌▌▌▌ 38.8 ms/query  ← includes embedding + CrossEncoder rerank
  p50 (cold)   ▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌ 1333 ms  ← first query after process start
```

Warm / cold reproducible from [`evals/results-2026-04-17.json`](../evals/results-2026-04-17.json).

## Comparison with published numbers of other systems

Numbers from different systems are comparable only when the answering model,
judge, prompts and question set match. Mem0 publishes 92.5 on LoCoMo and 94.4
on LongMemEval from its managed platform, with gpt-5 answering and judging, and
it also publishes the per-question answers behind them. Those answers were
graded next to TAM's answers to the same held-out questions under the same
judge. At the same answering model, no difference between the two systems was
statistically significant on either benchmark. That is not a demonstrated
equivalence; the protocol, the tuning history and the differences from the
earlier public protocol are in the
[head-to-head report](benchmarks/head-to-head-v14/RESULTS.md). Where a project
has not published per-question answers, we do not compare against its number.

A broader feature comparison, written in April 2026, is in
[vs-competitors.md](vs-competitors.md).

## Studies since v14

| Study | What it measures | Report |
|---|---|---|
| Head-to-head with Mem0 Platform (14.5.0) | LoCoMo and LongMemEval-S accuracy on held-out questions, two judges each | [head-to-head-v14](benchmarks/head-to-head-v14/RESULTS.md) |
| Scale (14.3.1, 14.4.0) | recall and save latency, HTTP throughput at 10k / 100k / 1M records, 200 tenants | [scale-v14](benchmarks/scale-v14/RESULTS.md) |
| MemoryAgentBench FactConsolidation (14.3.1, 14.4.0) | whether updated facts replace old ones | [memoryagentbench](benchmarks/memoryagentbench/FINDINGS.md) |
| Knowledge update (14.2.0, 14.3.0) | `memory_answer` on facts that change over time; LLM vs Jev contradiction scorer | [knowledge-update-v14](benchmarks/knowledge-update-v14/RESULTS.md) |
| Organisational memory (14.6.0) | access isolation between departments, concurrent updates, SQLite vs PostgreSQL | [org-memory-v14-20260925](benchmarks/org-memory-v14-20260925/RESULTS.md) |
| Release verification (14.0.0) | test counts, platform checks, artifact hashes | [release-final-v14-20260915](benchmarks/release-final-v14-20260915/RESULTS.md) |
| CPU use of the optional BGE reranker (14.0.0) | one-thread latency on Linux ARM64 | [grounded-v14](benchmarks/grounded-v14/CPU_RESULTS.md) |

The headline figures of each study are summarised in [whats-new.md](whats-new.md).
