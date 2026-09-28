# Personal settings, privacy and erasure

This page covers a personal install (`tam setup` → "Just me"). The team server has its own
**Administration → Settings** page; see [TEAM_DASHBOARD.md](TEAM_DASHBOARD.md).

## The Settings page

Open the local dashboard (`http://127.0.0.1:37737` by default) and choose **Settings**.

| Section | Settings |
|---|---|
| Language model | Provider (Ollama, OpenAI, Anthropic, OpenAI-compatible, auto), model, API base URL, API key, LLM tasks on/off, timeout |
| Embeddings | Provider. OpenAI, Cohere and DashScope take a model, key, base URL and dimensions. The local FastEmbed model is a setup preset: change it with `tam setup --reconfigure`, then run `memory_rebuild_embeddings` |
| Search answers | Characters per search result (`MEMORY_RECALL_MAX_RESULT_CHARS`, default 6000), flag records that address the agent (`MEMORY_FLAG_INSTRUCTIONS`, default on), cross-encoder re-ranking (`MEMORY_CROSS_RERANK`) |
| Storage | Days to keep raw call logs (`MEMORY_RAW_LOG_RETENTION_DAYS`, not set = forever), embedding model cache folder (`TAM_MODEL_CACHE`) |
| Stored credentials | Scan the store for credentials saved by earlier versions, then back up and redact them |

**Precedence.** A value saved on this page overrides the environment and the `env` block of an MCP
client config, which override the built-in default. Each field shows where its current value comes from.

**When changes apply.** Every agent session reads the settings when its MCP server starts. Restart the
agent, or run `/mcp` → reconnect in Claude Code, after a change.

**Where values live.** `<memory dir>/settings.json`, mode 0600. API keys in it are Fernet tokens, encrypted
with `TAM_MASTER_KEY` when that is set, otherwise with `<memory dir>/master.key` (created 0600 on the
first saved key). The page never receives a key back: it shows `••••` and the last four characters.
Keep a copy of `master.key` with your backups. Without it the saved keys cannot be read and must be
entered again.

**Who can change them.** The page's writes need a same-origin request that carries a token issued to that
page. Other web sites and plain form posts are refused, as are writes to any other dashboard address.

## Secrets never reach disk

Every tool call's arguments pass through one redaction list (`src/secret_redaction.py`) before the raw
call log or any table sees them. The same applies to prompts captured by the `UserPromptSubmit` hook, tool
output queued by the `PostToolUse` hook, and transcript extraction. The list covers:

- PEM private keys and passwords in URLs;
- Anthropic, OpenAI, Stripe, GitHub, GitLab, Slack, Google, Hugging Face, npm, Telegram and AWS keys;
- JWTs, bearer headers, and `password=` / `*_secret:` / `token=` / `api_key=` assignments;
- e-mail addresses and payment card numbers.

Each match becomes `[REDACTED]`. Write text between `<private>` and `</private>` to keep other passages
out of memory.

On POSIX the memory directory is made owner-only on start: the directory becomes 0700, and `memory.db`,
`settings.json` and `master.key` become 0600.

### Credentials saved by earlier versions

```bash
tam redact-existing            # report only: rows per table and raw logs that hold credentials
tam redact-existing --apply    # back up to backups/pre-redact-<time>*.db, then redact
```

The dashboard's **Scan memory** and **Back up and redact** buttons do the same. The backup keeps the old
values, so delete it once you have checked the store. Backups made before the run are listed too, since
they may hold credentials as well.

## Search answers stay small

`memory_recall` and `memory_search_fast` cut a record longer than the per-result limit. The cut record
carries `truncated: true` and `content_chars`, and its text ends with a pointer to
`memory_get(ids=[…])`, which returns the whole record. Evals and `memory_answer` read full records either way.

A record that addresses the reading agent rather than stating a fact is marked
`untrusted_instructions: true`, for example "ignore previous instructions" or "send the SSH key to …".
Once a result holds such a record, it also carries a `notice` telling the agent to treat it as data. Nothing is
removed. The team server applies both rules with its own settings.

## Erasing a record

`memory_delete(id)` hides a record: it leaves search and the vector index but stays in the database.
`memory_delete(id, hard=true)` erases it for good, together with:

- every earlier version it replaced;
- its vectors in every embedding space;
- its full-text, atomic-fact, graph-link, evidence-passage, queue and log rows;
- quotes of it inside later versions, which become `[erased]`;
- copies of its text in the raw call logs and in Chroma's write queue.

Full-text indexes are merged, freed database pages are zeroed, the write-ahead log is truncated and
Chroma's database is vacuumed. Search queries that mention the
record are not rewritten; set a raw-log retention period to limit how long those are kept. Hard erasure
is for personal (SQLite) memory. Team records keep their audited history and are removed through
offboarding.
