#!/usr/bin/env bash
# AMA-Bench pilot with TAM: answer gpt-5-mini, judge gpt-5.2, both through the local
# budget proxy with a hard dollar ceiling.
#
#   AMA_HARNESS_DIR=/path/to/AMA-Bench run_pilot.sh OUT_DIR
#
# Environment (all optional except AMA_HARNESS_DIR):
#   AMA_HARNESS_DIR   checkout prepared by setup_harness.sh
#   TAM_BENCH_PYTHON  interpreter with TAM's and the harness's dependencies
#                     (default: <repo>/.venv/bin/python)
#   CEILING_USD       hard budget for all OpenAI calls of this run (default 25)
#   EPISODES          number of episodes, stratified by domain (default 20)
#   KEY_FILE          env file with OPENAI_API_KEY=... (default ~/.config/tam-bench/openai.env)
#   UPSTREAM          OpenAI-compatible upstream (default https://api.openai.com/v1);
#                     a dry run sets it to the local stub together with a dummy KEY_FILE
#   EPISODE_CONCURRENCY / QUESTION_CONCURRENCY   harness concurrency (default 2 / 4)
#   METHOD_CONFIG     TAM method config (default configs/tam_method.yaml)
#   ANSWER_CONFIG     answerer config (default configs/answer_gpt5_mini.yaml)
#   EPISODE_IDS       explicit comma-separated episode ids (overrides EPISODES)
#
# Outputs under OUT_DIR: results/ (harness answers + judge results), leaderboard.jsonl,
# budget/ledger.jsonl + budget/summary.json ($ and wall time), run.log.
# Exit code 3 means the budget guard stopped the run before the ceiling was crossed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
COMMON="$REPO_ROOT/docs/benchmarks/tam_bench_common"
OUT_DIR="${1:?usage: AMA_HARNESS_DIR=... run_pilot.sh OUT_DIR}"
HARNESS="${AMA_HARNESS_DIR:?set AMA_HARNESS_DIR to the prepared AMA-Bench checkout}"
PY="${TAM_BENCH_PYTHON:-$REPO_ROOT/.venv/bin/python}"
CEILING_USD="${CEILING_USD:-25}"
EPISODES="${EPISODES:-20}"
UPSTREAM="${UPSTREAM:-https://api.openai.com/v1}"

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
TEST_FILE="$HARNESS/dataset/test/open_end_qa_set.jsonl"
METHOD_CONFIG="${METHOD_CONFIG:-$HERE/configs/tam_method.yaml}"
ANSWER_CONFIG="${ANSWER_CONFIG:-$HERE/configs/answer_gpt5_mini.yaml}"
if [ -z "${EPISODE_IDS:-}" ]; then
  EPISODE_IDS="$("$PY" "$HERE/select_episodes.py" --test-file "$TEST_FILE" --count "$EPISODES")"
fi

guard_args=(--ceiling-usd "$CEILING_USD" --allow-model gpt-5-mini --allow-model gpt-5.2
            --out-dir "$OUT_DIR" --upstream "$UPSTREAM")
if [ -n "${KEY_FILE:-}" ]; then
  guard_args+=(--key-file "$KEY_FILE")
fi

export TAM_SRC_DIR="$REPO_ROOT/src"
export TAM_BENCH_WORK_ROOT="$OUT_DIR/tam-stores"
# Episode stores are temporary; a stopped run (budget stop, Ctrl-C) cannot clean up its own.
trap 'rm -rf "$TAM_BENCH_WORK_ROOT"' EXIT

cd "$HARNESS"
set +e
caffeinate -i "$PY" "$COMMON/run_guarded.py" "${guard_args[@]}" -- \
  "$PY" src/run.py \
    --llm-server api \
    --llm-config "$ANSWER_CONFIG" \
    --judge-config "$HERE/configs/judge_gpt52.yaml" \
    --subset openend \
    --method tam \
    --method-config "$METHOD_CONFIG" \
    --test-file "$TEST_FILE" \
    --episode-ids "$EPISODE_IDS" \
    --max-concurrency-episodes "${EPISODE_CONCURRENCY:-2}" \
    --max-concurrency-questions-per-episode "${QUESTION_CONCURRENCY:-4}" \
    --output-dir "$OUT_DIR/results" 2>&1 | tee "$OUT_DIR/run.log"
status="${PIPESTATUS[0]}"
set -e
if [ "$status" -ne 0 ]; then
  echo "run finished with status $status (3 = budget stop); see $OUT_DIR/budget/summary.json" >&2
  exit "$status"
fi

answers="$(ls -t "$OUT_DIR"/results/answers_*.jsonl | head -1)"
results="$(ls -t "$OUT_DIR"/results/results_*.json | head -1)"
"$PY" "$HERE/to_leaderboard.py" --answers "$answers" --results "$results" --test-file "$TEST_FILE" \
  --output "$OUT_DIR/leaderboard.jsonl"
cat "$OUT_DIR/budget/summary.json"
