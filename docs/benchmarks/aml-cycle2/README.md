# TAM in the Agent Memory Leaderboard — cycle 2

Adapter that puts total-agent-memory (TAM) behind the
[Agent Memory Leaderboard](https://agentmemoryleaderboard.ai) Add/Search contract,
for the **open-source division, Textual and Coding tracks**. Source:
`src/aml_adapter/`, console entry point `tam-aml`.

Contract and rules were read on 2026-09-25 from `/rules`, `/api-guide`, `/docs`,
`/competition` and <https://github.com/AML-memory/agent-memory-leaderboard>
(the repository holds per-benchmark contracts and `api_config.py`; there is no
reference server, example adapter or OpenAPI file).

## Run it

Local smoke run (local fastembed model, no API key, nothing leaves the machine):

```bash
export AML_DATA_DIR=/srv/aml-run            # dedicated directory, never ~/.tam
export AML_API_KEYS="$(openssl rand -hex 32)"
export MEMORY_EMBED_PROVIDER=fastembed
tam-aml serve --host 127.0.0.1 --port 8765
```

Full run (mandatory open-source models: text-embedding-v4; no LLM on Add):

```bash
export AML_DATA_DIR=/srv/aml-run
export AML_API_KEYS=<key-registered-with-AML>        # comma-separated for rotation
export MEMORY_EMBED_PROVIDER=dashscope
export DASHSCOPE_API_KEY=<Model Studio key>           # region-bound
export MEMORY_EMBED_API_BASE=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
export MEMORY_EMBED_DIMENSIONS=1024
export AML_REQUIRE_EMBED_MODEL=text-embedding-v4      # workers refuse to start on any other model
python scripts/aml_freeze.py --run-label <run-label>  # writes FROZEN.md, refuses a dirty tree
tam-aml serve --host 127.0.0.1 --port 8765            # behind a TLS reverse proxy
```

Other commands: `tam-aml settings` prints the result-shaping settings and their
hash; `tam-aml purge --expired` / `tam-aml purge --all --yes` delete run data
offline (the server must be stopped; the running server purges on its own).

Endpoints (and nothing else): `GET /health` (no auth), `POST /add`, `POST /search`.
Accepted credentials: `Authorization: Bearer <key>`, `Authorization: Token <key>`,
`X-Api-Key: <key>`, or a bare `Token: <key>` header.

### Adapter settings

| Variable | Default | Meaning |
|---|---|---|
| `AML_DATA_DIR` | required | Run data; refused if inside `~/.tam` or `~/.claude-memory` |
| `AML_API_KEYS` | required | Comma-separated accepted keys |
| `AML_AUTH_DISABLED` | `false` | Only for the public no-auth smoke |
| `AML_HOST` / `AML_PORT` | `127.0.0.1` / `8765` | Listener |
| `AML_WORKERS` | `4` | Concurrent per-user worker processes |
| `AML_MAX_INFLIGHT` | `16` | Requests in flight before `429` |
| `AML_RETRY_AFTER_SECONDS` | `5` | `Retry-After` on `429` (1–60) |
| `AML_WORKER_WAIT_SECONDS` | `30` | Wait for a free worker before `429` |
| `AML_OPERATION_TIMEOUT_SECONDS` | `1680` | Worker deadline (the platform allows 30 min) |
| `AML_MAX_BODY_BYTES` | `33554432` | Larger bodies get `413` |
| `AML_FRAGMENT_MAX_CHARS` | `6000` | Longer messages are split (text-embedding-v4: 8,192 tokens/text) |
| `AML_CONTENT_FORMAT` | `annotated` | `annotated` = `[ISO time] role: text`; `raw` = message text only |
| `AML_QUERY_INCLUDE_OPTIONS` | `false` | Append multiple-choice options to the retrieval query |
| `AML_MAX_TOP_K` | `1000` | Upper bound on results (never more than `top_k`) |
| `AML_EMBED_CONCURRENCY` | `4` | Parallel embedding requests inside one Add |
| `AML_REQUIRE_EMBED_MODEL` | empty | Refuse to serve with any other embedding model |
| `AML_RETENTION_DAYS` | `14` | TTL since a user's last Add (max 30) |
| `AML_PURGE_INTERVAL_SECONDS` | `3600` | Purge cycle |
| `AML_METRICS_INTERVAL_SECONDS` | `60` | `metrics.prom` refresh |

Embedding settings (`src/config.py`, `src/embed_provider.py`):

| Variable | dashscope default | Notes |
|---|---|---|
| `MEMORY_EMBED_PROVIDER` | — | `dashscope` selects `DashScopeEmbedProvider` |
| `MEMORY_EMBED_MODEL` | `text-embedding-v4` | |
| `MEMORY_EMBED_API_BASE` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | Beijing: `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `DASHSCOPE_API_KEY` / `MEMORY_EMBED_API_KEY` | — | |
| `MEMORY_EMBED_DIMENSIONS` | `1024` | Allowed: 2048, 1536, 1024, 768, 512, 256, 128, 64 |
| `MEMORY_EMBED_BATCH_SIZE` | `10` | API maximum is 10 texts per request; larger values are refused |
| `MEMORY_EMBED_MAX_RETRIES` | `6` | 408/425/429/5xx and network errors; honours `Retry-After` |
| `MEMORY_EMBED_TIMEOUT_SEC` | `60` | Per request |
| `MEMORY_EMBED_MAX_BACKOFF_SEC` | `30` | Cap for one retry delay |

Invalid numeric values raise at startup instead of falling back to a default.

## Architecture

```
AML platform ──HTTPS──> reverse proxy ──> tam-aml (Starlette/uvicorn, one process)
                                            │ Guard: auth, in-flight limit → 429, body limit → 413
                                            │ AmlService: validation, registry, metrics, JSON logs
                                            │ WorkerPool: one spawned process per active user_id (LRU)
                                            ▼
                           users/<sha256(user_id)[:32]>/memory.db   (a complete TAM store)
                              Store.save_knowledge  ← Add (one SQLite transaction)
                              Recall.search         ← Search (FTS5 + vectors + RRF + local cross-encoder)
```

**Isolation — a separate store and a separate process per `user_id`.** A shared
store with a `project` namespace was rejected: TAM keeps about 85 tables (graph
nodes, episodes, atomic facts, caches, queues), several derived from content and
not all keyed by project, plus module-level state in the process. A namespace
would need every one of them to filter correctly forever, and the 30-day
deletion would need to find every derived row. With one directory per user
the guarantee is structural — Search opens only that user's database — and
deletion is removing the directory. Each worker is a spawned process
(`TAM_MEMORY_DIR` = the user's directory), so no in-process cache can be shared
between users. Cost: about 1 s to start a worker (`import server` + store open);
workers are reused LRU. `tests/test_aml_adapter_http.py::test_user_isolation_has_zero_leakage`
checks it.

**Add.** Messages become fragments: one per message, split on line boundaries
above `AML_FRAGMENT_MAX_CHARS`. All fragments are embedded first — batches of
10, `AML_EMBED_CONCURRENCY` in parallel — so an embedding outage returns `503`
before anything is written. Then one `BEGIN IMMEDIATE` transaction holds the
idempotency row, every `Store.save_knowledge` call (with the precomputed vector,
`source_format=conversation`, no dedup, no quality gate) and the fragment rows;
TAM's internal commits are deferred by `AtomicConnection`. `success: true`
means committed and searchable: Search reads the same database and TAM's query
caches are invalidated.

**Idempotency.** Key `(user_id, request_id)` — one store per user, primary key
`request_id`. The row stores SHA-256 of `session_id` + `messages`. Same payload
→ the original success, nothing written; different payload → `409`. A retry
after a crash or timeout finds no row (the transaction rolled back) and runs again.

**Search.** `Recall.search(project="aml", limit=min(top_k, AML_MAX_TOP_K),
record_usage=False)`. Every hit is mapped back to its fragment row; only stored
fragments are returned, verbatim, sorted by TAM's fused score (descending,
stable). `id` = `<ns[:12]>-<fragment id>` (stable), `created_at` = the message
timestamp (omitted when the message had none). No generation, no answer hints.
If the query embedding fails, the request fails with `503` instead of silently
falling back to keyword-only results. The query is embedded once and cached in
the user's store.

**Forced TAM settings in every worker** (`runtime.FORCED_TAM_ENV`):
`MEMORY_LLM_ENABLED=false` and every LLM stage off (quality gate, contradiction
detector, coref, query rewrite, HyDE), `USE_BINARY_SEARCH=true` (vectors only in
SQLite, no Chroma side store), async enrichment off, activeContext markdown off.
`MEMORY_CROSS_RERANK=auto` is resolved to `on`, so every worker ranks the same
way from its first query; set `off` to disable. The dashscope mode never falls
back to another embedding model.

**Back-pressure.** More than `AML_MAX_INFLIGHT` requests, or no free worker
within `AML_WORKER_WAIT_SECONDS`, gives `429` with `Retry-After`. A worker that
exceeds `AML_OPERATION_TIMEOUT_SECONDS` is killed and the request gets `503`
(rolled back, safe to retry).

**Retention and deletion journal.** `registry.db` records each user's last Add.
Every `AML_PURGE_INTERVAL_SECONDS` users idle longer than `AML_RETENTION_DAYS`
are deleted: the worker is stopped, the directory removed, and one JSON line
appended (fsync) to `deletion-journal.jsonl` with `user_ns`, `user_id`,
`created_at`, `last_write_at`, `deleted_at`, fragment/request counts and bytes —
never content. A decision made before a newer Add is discarded.

**Observability.** One JSON log line per request (`operation`, `status`,
`duration_ms`, `user_ns`, counts — no content, no raw `user_id`). Metrics
(`aml_requests_total` counter, `aml_request_duration_seconds` histogram,
`aml_items_total`) go to `<AML_DATA_DIR>/metrics.prom` for a node_exporter
textfile collector, because AML allows only the three endpoints on the listener.

### Compliance map

| AML rule | Where |
|---|---|
| `success:true` only when durable and searchable | single transaction in `Runtime.add`; `test_add_then_immediate_search_*` |
| Echo `request_id`, `user_id`, `session_id` | `AmlService.add` |
| `user_id` is the only isolation field | per-user store + process; `session_id` is stored, never filtered on |
| Idempotent Add, 32 retries | `aml_requests`; `test_idempotent_retry_stores_once`, `test_failed_add_leaves_nothing_and_retry_succeeds` |
| 429 with `Retry-After` ≤ 60 | `Guard`, `WorkerPool`; `test_inflight_limit_returns_429_with_retry_after` |
| Search returns stored fragments only, ≤ `top_k`, `[]` when empty | `Runtime.search`; `test_unknown_user_gets_empty_list` |
| No dataset-specific logic | none; the only inputs are the contract fields |
| Delete within 30 days | TTL purge + journal; `test_retention_purge_deletes_data_and_journals` |
| Do not log payloads | logs carry ids, counts, timings |
| text-embedding-v4 | `DashScopeEmbedProvider`; `AML_REQUIRE_EMBED_MODEL`; `test_text_embedding_v4_batches_ten_and_embeds_once` |
| LLM on Add only gpt-4o-mini | no LLM at all on Add or Search |

## What is frozen for a Full run

`scripts/aml_freeze.py` writes `FROZEN.md` next to this file:

- commit SHA (refuses uncommitted changes unless `--allow-dirty`, smoke only);
- adapter settings, embedding settings (provider, model, base URL, dimensions,
  batch size, retries; the key only as "present"), the TAM settings forced in
  workers, and one SHA-256 over those three blocks;
- package version, Python and platform, versions of fastembed, onnxruntime,
  numpy, starlette, uvicorn, pydantic, httpx, mcp;
- non-secret `AML_*`, `MEMORY_*`, `V9_*`, `USE_*`, `FASTEMBED_*`, `TAM_*`
  variables; names containing KEY/TOKEN/SECRET/PASSWORD are listed without values.

Template of the generated file:

```markdown
# AML cycle 2 — frozen configuration
- Run label: `<label>`
- Frozen at: <UTC time>
- Commit: `<sha>`
- Package version: <x.y.z>
- Configuration SHA-256: `<sha256>`
- Python / Platform
## Adapter settings        (JSON)
## Embedding               (JSON)
## TAM settings forced in every worker (JSON)
## Packages
## Environment (non-secret)
Secret variables present (values withheld): ...
```

Procedure: tag the commit, deploy exactly that commit, run the script on the
evaluation host with the service's environment, commit `FROZEN.md` in a
follow-up commit (it names the run commit, so it cannot live in it).

## Hosting

| | Mac + Cloudflare Tunnel | Hetzner Cloud VPS |
|---|---|---|
| Use | Smoke runs | Full runs |
| Cost | €0 beyond electricity | Regular Performance CPX32 (4 vCPU, 8 GB) or CPX42 (8 vCPU, 16 GB); hetzner.com rendered no prices for us — check the console |
| Public HTTPS | Tunnel gives a public hostname | Caddy/nginx with Let's Encrypt, DNS record not proxied |
| Long requests | Proxied requests fail with **524 after 125 s** (current Cloudflare docs; the often-quoted 100 s is outdated). Only Enterprise can raise it (up to 6,000 s). The platform retries Add on 524 (idempotent, so safe) but **not Search** | No proxy limit; set `proxy_read_timeout 1800s` (nginx) or equivalent |
| Availability | Laptop sleep, home uplink | Data-centre uptime |
| Data location | Personal machine | Dedicated disk, wiped after the run |

A text-embedding-v4 Add of a large Coding session (thousands of fragments) can
exceed 125 s, so Full runs should not go through a Cloudflare-proxied hostname.

## Cost of text-embedding-v4

```
cost_usd = price_per_Mtok × (T_corpus × (1 + h) × r  +  Q × T_query) / 1e6
```

- `price_per_Mtok` = $0.07 (Singapore / Hong Kong), $0.072 (Beijing) — Model
  Studio pricing page, 2026-09-25; 1M free tokens for 90 days.
- `T_corpus` = tokens written through Add. The competition page gives
  **~300M** for Textual (6,000+ instances, 32 sources) and **~1.8B** for Coding
  (~300 instances). Assumed to be counted with a tokenizer close to Qwen's.
- `h` = annotation overhead (`[ISO time] role: ` ≈ 10 tokens per message);
  assume 0.05. Use 0 with `AML_CONTENT_FORMAT=raw`.
- `r` = re-embedding factor. An Add replayed after success costs nothing; one
  rolled back after embedding (timeout, crash) is embedded again. Assume 1.02.
- `Q × T_query`: one query embedding per Search (cached per user). 6,300 × ~50
  tokens ≈ 0.3M tokens — negligible.

| Track | T_corpus | Estimate per Full run |
|---|---|---|
| Textual | 300M | 0.07 × 300 × 1.05 × 1.02 ≈ **$22.5** |
| Coding | 1.8B | 0.07 × 1,800 × 1.05 × 1.02 ≈ **$135** |

Two Full runs per track are allowed: budget about $315 plus smoke runs. Rate
limits matter more than money for Coding: at a tokens-per-minute limit `L`,
embedding takes `T_corpus / L` minutes (1.8B at 1M TPM ≈ 30 h). Check the
account's text-embedding-v4 RPM/TPM before the Coding run.

## Questions for the organizers

1. License: TAM is MIT. The license list on the site belongs to the benchmark
   submission form; open-source methods need a public repository at a fixed
   commit with attribution. Please confirm MIT is fine (low risk).
2. text-embedding-v4: which provider/region counts (Alibaba Cloud International
   Singapore endpoint `dashscope-intl`, Beijing, or a workspace endpoint)? Is
   the dimension free to choose (we use the default 1,024), and does the rule
   also cover query embeddings (we use it for both)?
3. The English rules say open-source entries are "expected to use gpt-4o-mini
   during Add". Is an Add path with no LLM at all compliant? (We store and index
   messages without any LLM.)
4. Answer and judge models: which ones, and does the Answer prompt include
   `created_at` or only `content`? This decides `annotated` vs `raw` content.
5. Is a fragment prefixed with its stored time and role (`[2023-05-08T12:00:00Z]
   user: …`) acceptable as "stored memory", or must `content` be the message text only?
6. A genuine request_id conflict answers `409`, which the platform retries up to
   32 times. Would you prefer `422` for that case?
7. Can Textual or Coding messages contain `image_url` parts? We reject them with
   `422` (text tracks only).
8. Typical Add size and platform concurrency for the Coding track (to size
   workers and the embedding rate limit).
9. Is the JSONL deletion journal acceptable as the deletion record for the report?
10. Search is not retried on `524`: please confirm, since it rules out
    Cloudflare-proxied endpoints for Full runs.

## Discrepancies found on the AML site (2026-09-25)

- text-embedding-v4 and "LLM only gpt-4o-mini" appear on `/competition` only;
  `/rules` and `/docs` say the platform prescribes no embedding model, and
  `/rules` says open-source entries are "expected to use gpt-4o-mini during Add".
- `content` and `query` may be a string **or** a `ContentPart[]` list.
- Search retries 408/425/429/500/502/503/504 only — not 409 and not 524. Add
  also retries 409 and 524. `429` without `Retry-After` waits 60 s.
- A `202` asynchronous Add exists but needs pre-approval and an Add-status URL.
- The response must not exceed `top_k`; formal evaluations use `top_k = 100`.
  The Answer model keeps a token-counted prefix of the results (117,760 input
  tokens), so fragment size limits how many results it reads.
- Endpoints must be public HTTPS; no credentials in URLs; private/loopback
  addresses are rejected.
- Full: at most two per key and track; the second unlocks 30 days after the
  first **completes**. With a first Full around 2026-10-01, a second one is only
  possible in the last days before evaluation closes (2026-11-04 23:59 UTC+8);
  plan on one Full per track. Smoke: one per hour, at most 30 per track.
- Track sizes disagree: `/competition` says Textual 6,000+ instances and Coding
  ~300; the GitHub README says Textual 5,000+ questions and Coding 12
  repositories / 1,290 tasks; `/rules` says Coding is 150 tasks × 2 conditions.
- Deletion is required within 30 days after the run; no journal format is specified.
- Cloudflare's proxy read timeout is 125 s, not 100 s.
- Model Studio's current embedding page shows workspace endpoints
  (`https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`);
  `dashscope-intl.aliyuncs.com/compatible-mode/v1` is the long-standing
  Singapore endpoint and is the default here — check it with the real key.
