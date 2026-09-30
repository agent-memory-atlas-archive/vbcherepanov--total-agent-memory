#!/usr/bin/env bash
# Inner loop of run_pilot.sh; runs under run_guarded.py (TAM_BENCH_PROXY_URL is set).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${TAM_BENCH_PROXY_URL:?pilot_inner.sh must run under run_guarded.py}"

for point in $OPERATING_POINTS; do
  case "$point" in
    norerank) rerank=off ;;
    rerank) rerank=on ;;
    *) echo "unknown operating point $point" >&2; exit 2 ;;
  esac
  for domain in web enterprise; do
    selection=()
    if [ "$QUESTIONS_PER_DOMAIN" != "all" ]; then
      selection=(--question-ids "$("$LME_PYTHON" "$HERE/select_questions.py" --data-root "$LME_DATA_ROOT" \
        --domain "$domain" --count "$QUESTIONS_PER_DOMAIN")")
    fi
    run_dir="$OUT_DIR/tam_${point}_${domain}_small"
    started=$(date +%s)
    "$LME_PYTHON" "$HERE/run_tam.py" \
      --harness-dir "$LME_HARNESS_DIR" \
      --data-root "$LME_DATA_ROOT" \
      --domain "$domain" \
      ${selection[@]+"${selection[@]}"} \
      --output-dir "$run_dir" \
      --cross-rerank "$rerank" \
      --tam-python "$TAM_BENCH_PYTHON" \
      --work-root "$OUT_DIR/tam-stores" \
      --reader-model "$READER_MODEL" \
      --reader-base-url "$READER_BASE_URL"
    echo "{\"event\": \"domain_done\", \"point\": \"$point\", \"domain\": \"$domain\", \"wall_seconds\": $(( $(date +%s) - started ))}"
  done
  "$LME_PYTHON" "$LME_HARNESS_DIR/leaderboard/combine_aggregated_metrics.py" \
    "$OUT_DIR/tam_${point}_enterprise_small/aggregated_metrics.json" \
    "$OUT_DIR/tam_${point}_web_small/aggregated_metrics.json" \
    -o "$OUT_DIR/tam_${point}_small_combined_metrics.json"
done
