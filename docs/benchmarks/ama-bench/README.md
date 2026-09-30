# AMA-Bench with total-agent-memory (TAM)

TAM as a two-stage memory method for [AMA-Bench](https://github.com/AMA-Bench/AMA-Bench)
(open-ended subset, 208 episodes, 2,496 questions, test split only).

Construction: each episode's trajectory is split into `Step N` fragments (steps longer
than 2,000 chars are split on line boundaries) and written into a fresh TAM store, one
store per episode in its own worker process and temp directory. Retrieval: TAM recall
(FTS5 + local vectors + RRF + local cross-encoder, `top_k=10`) on the question; hits are
kept in rank order up to 40,000 chars and shown in trajectory order under the task
description. No LLM runs inside TAM (fast mode, enrichment / query rewriting / LLM hooks
off, no API key in the worker). All settings are general defaults: AMA-Bench has only a
test split, and nothing was tuned on it.

## Files

| File | Purpose |
|---|---|
| `tam_memory.py` | The method (`TAMMethod`), symlinked into the harness as `src/method/tam_memory.py` |
| `setup_harness.sh` | Clone AMA-Bench at commit `ddfd319e`, install the method, link or download the dataset (revision `a5777378`, sha256-checked) |
| `configs/tam_method.yaml` | Method settings |
| `configs/answer_gpt5_mini.yaml` | Answerer: gpt-5-mini, Responses API, `reasoning_effort: minimal`, `max_tokens: 4096` |
| `configs/tam_method_v6.yaml` | Method settings of the full test run (v6): whole-record context fill up to 70,000 characters, cross-encoder rerank, step and quote anchors, action timeline and statistics |
| `configs/answer_gpt5_mini_medium.yaml` | Answerer of the full test run: gpt-5-mini, `reasoning_effort: medium`, `max_tokens: 16384` |
| `configs/judge_gpt52.yaml` | Judge: gpt-5.2, `reasoning_effort: none` (gpt-5.2 does not accept the harness default `minimal`) |
| `select_episodes.py` | Stratified-by-domain, seeded episode subset for pilots |
| `run_pilot.sh` | The pilot: harness behind the budget guard, then leaderboard conversion |
| `to_leaderboard.py` | Harness output -> HF leaderboard JSONL (`episode_id` as string, `question_uuid_list`, `llm_as_judge_score_list`) |
| `requirements.txt` | Python packages of the run environment |

Budget guard, key handling, prices and the stub: `../tam_bench_common/README.md`.

## Environment

One interpreter runs the harness and the TAM worker. The TAM repo's dev virtualenv works
(`.venv`, with `pip install -e .`), plus `requirements.txt` here. The harness's own
`requirements.txt` (vLLM, torch, alfworld, ...) is not needed for API models.

```bash
docs/benchmarks/ama-bench/setup_harness.sh /path/to/scratch/ama-harness
# or reuse a downloaded dataset/test directory:
docs/benchmarks/ama-bench/setup_harness.sh /path/to/scratch/ama-harness /path/to/dataset/test
```

The key goes into `~/.config/tam-bench/openai.env` (mode 600, `OPENAI_API_KEY=...`).

## Pilot (20 episodes, 240 questions, ceiling $25)

```bash
AMA_HARNESS_DIR=/path/to/scratch/ama-harness CEILING_USD=25 EPISODES=20 \
  docs/benchmarks/ama-bench/run_pilot.sh /path/to/scratch/runs/ama-pilot-$(date +%Y%m%d-%H%M)
```

The 20 episodes (seed 20260928, proportional to domain size) are
`8,22,25,47,51,58,61,84,86,102,111,112,129,135,147,154,169,178,193,203`.

## Full test run (208 episodes, v6)

`METHOD_CONFIG`, `ANSWER_CONFIG` and `EPISODE_IDS` select the configs and the episodes. The
full test set ran in eight chunks of 26 episodes (ids 0-25, 26-51, ..., 182-207), so one
failed OpenAI call costs one chunk, not the run:

```bash
AMA_HARNESS_DIR=/path/to/scratch/ama-harness CEILING_USD=4 EPISODE_CONCURRENCY=4 METHOD_CONFIG=docs/benchmarks/ama-bench/configs/tam_method_v6.yaml ANSWER_CONFIG=docs/benchmarks/ama-bench/configs/answer_gpt5_mini_medium.yaml EPISODE_IDS=$(seq -s, 0 25) docs/benchmarks/ama-bench/run_pilot.sh /path/to/scratch/runs/ama-v6/chunk_000_025
```

Each chunk writes its own `leaderboard.jsonl` with one line per episode; the submission is
their concatenation.

Result (2026-09-30, all 208 episodes, 2,496 questions, judge gpt-5.2, $18.48 at the guard's
worst-case prices, 1 h 50 min at `EPISODE_CONCURRENCY=4`), scored with the board's own
`compute_scores_from_submissions`:

| | Recall (A) | Causal (B) | State update (C) | State abstraction (D) | Domain |
|---|---:|---:|---:|---:|---:|
| TEXT2SQL | 0.874 | 0.791 | 0.813 | 0.500 | 0.745 |
| SOFTWARE | 0.429 | 0.600 | 0.315 | 0.486 | 0.458 |
| WEB | 0.744 | 0.785 | 0.763 | 0.672 | 0.741 |
| GAME | 0.825 | 0.733 | 0.878 | 0.767 | 0.801 |
| EMBODIED_AI | 0.147 | 0.467 | 0.680 | 0.525 | 0.455 |
| OPENWORLD_QA | 0.704 | 0.789 | 0.757 | 0.750 | 0.750 |

Mean of the 24 cells: **0.658**; accuracy over all questions: 0.678. On the published board
GPT-5 mini reading the whole trajectory scores 0.656. Submitted to the leaderboard on
2026-09-30 as an agent entry (model family GPT-5 mini), self-reported until the board's
judge verifies it. On 72 questions (episodes 8, 47, 61, 102, 147, 178) a local Qwen3-32B
judge agreed with gpt-5.2 on 65 and accepted 53 answers against 50.

Outputs: `results/answers_*.jsonl`, `results/results_*.json` (judge verdicts),
`leaderboard.jsonl`, `budget/ledger.jsonl` (per call: tokens, $, latency),
`budget/summary.json` (total $, wall time, stop reason), `run.log`. Exit code 3 = the
guard stopped the run before the ceiling; the results are then incomplete and not
converted.

Full run: `EPISODES=208` (the selector returns every episode).

## Dry run through the stub (no paid call)

```bash
S=/path/to/scratch
printf 'OPENAI_API_KEY=dry-run-dummy\n' > "$S/dummy.env"; chmod 600 "$S/dummy.env"
.venv/bin/python docs/benchmarks/tam_bench_common/stub_openai.py --port 18801 \
  --expect-key-file "$S/dummy.env" --log "$S/stub-requests.jsonl" &
AMA_HARNESS_DIR=$S/ama-harness UPSTREAM=http://127.0.0.1:18801/v1 KEY_FILE=$S/dummy.env \
  docs/benchmarks/ama-bench/run_pilot.sh "$S/dry/ama"
# budget stop check: add CEILING_USD=0.05
```

## Cost expectation

Per question: one gpt-5-mini call (prompt <= ~40k chars of retrieved steps, about 10k
tokens; short answer) and one gpt-5.2 judge call (a few hundred tokens). At list prices
that is roughly $0.003 + $0.001 per question, i.e. about $1 for the 240-question pilot and
about $10 for the full 2,496 questions. The guard's ceiling bounds it regardless.

## Measured locally (dry run through the stub, 2026-09-28)

20 episodes / 240 questions / 480 stubbed API calls in 12.3 min wall on an Apple M2 Max
(TAM store build + recall per episode dominate; the real API adds its own latency).
A second dry run with `CEILING_USD=0.05` stopped with exit code 3 at $0.040 spent.
