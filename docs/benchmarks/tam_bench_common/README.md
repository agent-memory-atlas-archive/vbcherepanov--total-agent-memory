# tam_bench_common

Shared plumbing for the TAM benchmark adapters in `docs/benchmarks/ama-bench/` and
`docs/benchmarks/longmemeval-v2/`.

| File | Purpose |
|---|---|
| `tam_worker.py` | A private TAM store in its own worker process (`python -m tam_bench_common.tam_worker`), temp `TAM_MEMORY_DIR`, secrets stripped from its env, fast mode forced (no LLM inside TAM). |
| `budget_proxy.py` | OpenAI metering proxy with a hard dollar ceiling; the only component that holds the API key. |
| `run_guarded.py` | Launcher: loads the key, starts the proxy, runs the benchmark command with a session token, stops it on budget trip (exit 3), writes `budget/ledger.jsonl` and `budget/summary.json`. |
| `prices.json` | OpenAI list prices used by the guard, with source and retrieval date. |
| `stub_openai.py` | Local OpenAI-compatible stub (not a model) for zero-cost dry runs. |

## API key

The key lives in `~/.config/tam-bench/openai.env` (mode `0600`), one line:

```
OPENAI_API_KEY=sk-...
```

`export` and quotes are accepted. If the file does not exist, `OPENAI_API_KEY` from the
environment is used. `run_guarded.py` refuses a group/other-readable key file, refuses to
send the default key file (or the env key) to any upstream other than
`https://api.openai.com/v1`, removes every `*API_KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*`
variable from the benchmark's environment and gives it a random session token instead.
The key is never written to configs, ledgers or logs.

## Budget guard

- Each request is reserved at its worst case before it is forwarded: input tokens <=
  request body bytes + 256, output tokens <= the request's own `max_output_tokens` /
  `max_completion_tokens` / `max_tokens` (requests without one are refused, as are
  `stream=true` and models without a price).
- A request is forwarded only if `spent + in-flight + worst case <= ceiling`; otherwise
  the proxy answers HTTP 402 (the OpenAI SDK does not retry it), refuses everything after
  it and `run_guarded.py` terminates the benchmark's process group. Spend therefore never
  crosses the ceiling.
- After the response the reservation is replaced by the cost from the API's `usage`
  fields: `input_tokens * input + output_tokens * output` (reasoning tokens are part of
  output tokens). The guard ignores the cached-input discount (upper bound); the ledger
  also records the list-price cost with it (`list_usd`).
- A 200 without `usage`, or a transport failure after the request was sent, is charged
  the full reservation.

Prices (`prices.json`, Standard tier, USD per 1M tokens, retrieved 2026-09-28 from
https://developers.openai.com/api/docs/pricing, cross-checked with OpenRouter's
pass-through prices):

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| gpt-5-mini | 0.25 | 0.025 | 2.00 |
| gpt-5.2 | 1.75 | 0.175 | 14.00 |

## Dry run (no paid call)

```bash
S=/path/to/scratch
printf 'OPENAI_API_KEY=dry-run-dummy\n' > "$S/dummy.env"; chmod 600 "$S/dummy.env"
python docs/benchmarks/tam_bench_common/stub_openai.py --port 18801 \
  --expect-key-file "$S/dummy.env" --log "$S/stub-requests.jsonl" &
# then any pilot command with UPSTREAM=http://127.0.0.1:18801/v1 KEY_FILE=$S/dummy.env
```

Tests: `tests/test_bench_budget_proxy.py`, `tests/test_bench_adapters.py`
(`RUN_BENCH_ADAPTER_SLOW=1` also starts a real TAM worker).
