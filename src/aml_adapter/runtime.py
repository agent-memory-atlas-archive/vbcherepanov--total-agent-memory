"""Per-user worker process: one TAM Store + Recall bound to one AML user_id.

`serve()` runs in a spawned process whose TAM_MEMORY_DIR is that user's
directory, so every table, cache and index TAM keeps lives in a store that no
other user_id can reach. Add is a single SQLite transaction covering the TAM
records, the fragment rows and the idempotency row.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from aml_adapter.contracts import AddRequest, SearchRequest, content_text
from aml_adapter.errors import AdapterError, Conflict, Unavailable
from aml_adapter.fragments import build_fragments, iso_from_ms
from aml_adapter.logs import configure_logging

LOGGER = logging.getLogger("aml_adapter.runtime")
PROJECT = "aml"
KNOWLEDGE_TYPE = "fact"
SQLITE_MAX_PARAMS = 900

# Applied in the worker before TAM is imported. They keep the save path free
# of LLM calls and of any store outside the user's directory.
FORCED_TAM_ENV = {
    "MEMORY_MODE": "fast",
    "USE_BINARY_SEARCH": "true",  # vectors in SQLite only, no Chroma side store
    "MEMORY_ASYNC_ENRICHMENT": "false",
    "MEMORY_ENRICHMENT_ENABLED": "false",
    "MEMORY_LLM_ENABLED": "false",
    "MEMORY_USE_LLM_IN_HOT_PATH": "false",
    "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH": "false",
    "USE_OLLAMA_EMBED": "false",
    "USE_ADVANCED_RAG": "false",
    "MEMORY_QUERY_REWRITE": "0",
    "MEMORY_COREF_ENABLED": "false",
    "MEMORY_QUALITY_GATE_ENABLED": "false",
    "MEMORY_CONTRADICTION_DETECT_ENABLED": "false",
    "MEMORY_ENTITY_DEDUP_ENABLED": "false",
    "MEMORY_WIKI_AUTO_REFRESH_EVERY_N": "0",
    "MEMORY_ACTIVECONTEXT_DISABLE": "true",
    # Recall embeds the query in more than one tier; the per-user SQLite
    # embedding cache makes that one API call instead of several.
    "V9_CACHE_L2_ENABLED": "true",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS aml_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aml_requests (
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    fragments INTEGER NOT NULL,
    committed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS aml_fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id INTEGER NOT NULL UNIQUE,
    request_id TEXT NOT NULL REFERENCES aml_requests(request_id),
    session_id TEXT NOT NULL,
    message_index INTEGER NOT NULL,
    part_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    timestamp_ms INTEGER,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS aml_fragments_request ON aml_fragments(request_id);
"""


@dataclass(frozen=True)
class RuntimeSettings:
    fragment_max_chars: int
    content_format: str
    query_include_options: bool
    max_top_k: int
    embed_concurrency: int
    require_embed_model: str
    id_prefix: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class AtomicConnection(sqlite3.Connection):
    """Connection whose commits are deferred while `atomic()` is active.

    TAM's save path commits after each step; inside `atomic()` those commits
    are no-ops so the whole Add lands in one transaction or not at all.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.in_atomic = False
        self.failed = False

    def commit(self):
        if not self.in_atomic:
            super().commit()

    def rollback(self):
        if self.in_atomic:
            self.failed = True
        super().rollback()

    def executescript(self, script):
        if not self.in_atomic:
            return super().executescript(script)
        # executescript() would COMMIT first; run statement by statement instead.
        cursor = self.cursor()
        statement = ""
        for char in script:
            statement += char
            if char == ";" and sqlite3.complete_statement(statement):
                cursor.execute(statement)
                statement = ""
        if statement.strip():
            cursor.execute(statement)
        return cursor

    @contextmanager
    def atomic(self):
        super().commit()
        self.in_atomic, self.failed = True, False
        self.execute("BEGIN IMMEDIATE")
        try:
            yield
            if self.failed:
                raise Unavailable("storage transaction was rolled back; retry the request")
            super().commit()
        except BaseException:
            super().rollback()
            raise
        finally:
            self.in_atomic = False


class Runtime:
    def __init__(self, settings: RuntimeSettings):
        import server
        from server import HTTP_EMBED_MODES

        self.settings = settings
        self.http_modes = HTTP_EMBED_MODES
        self.store = server.Store(connection_factory=AtomicConnection)
        self.recall = server.Recall(self.store)
        self.db: AtomicConnection = self.store.db
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.dimension = self._check_embedding_identity()

    def _check_embedding_identity(self) -> int:
        mode = self.store._embed_mode
        if mode not in ("fastembed", *self.http_modes):
            raise RuntimeError(f"no usable embedding backend (mode={mode})")
        model = self.store._active_embed_model_name()
        required = self.settings.require_embed_model
        if required and model != required:
            raise RuntimeError(f"embedding model {model!r} does not match AML_REQUIRE_EMBED_MODEL={required!r}")
        if mode in self.http_modes:
            dimension = int(self.store.embed_provider.dim() or 0)
        else:
            probe = self.store.embed(["dimension probe"])
            dimension = len(probe[0]) if probe and probe[0] else 0
        if dimension < 1:
            raise RuntimeError(f"embedding dimension of {mode}:{model} is unknown")
        identity = f"{mode}:{model}:{dimension}"
        row = self.db.execute("SELECT value FROM aml_meta WHERE key='embedding_identity'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO aml_meta(key, value) VALUES ('embedding_identity', ?)", (identity,))
            self.db.commit()
        elif row[0] != identity:
            raise RuntimeError(f"store was built with {row[0]}, refusing to mix with {identity}")
        return dimension

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if self.store._embed_mode in self.http_modes:
            provider = self.store.embed_provider
            step = max(1, provider.batch_size if hasattr(provider, "batch_size") else len(texts))
            slices = [texts[i:i + step] for i in range(0, len(texts), step)]
            try:
                with ThreadPoolExecutor(max_workers=min(self.settings.embed_concurrency, len(slices))) as pool:
                    vectors = [vec for part in pool.map(provider.embed, slices) for vec in part]
            except urllib.error.HTTPError as exc:
                raise Unavailable(f"embedding API returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, OSError, RuntimeError, ValueError) as exc:
                raise Unavailable(f"embedding API failed: {type(exc).__name__}") from exc
        else:
            vectors = self.store.embed(texts)
            if vectors is None or any(v is None for v in vectors):
                raise Unavailable("local embedding model failed")
        if len(vectors) != len(texts) or any(len(v) != self.dimension for v in vectors):
            raise Unavailable("embedding backend returned vectors of the wrong shape")
        return vectors

    def add(self, request: AddRequest) -> dict:
        fingerprint = request.fingerprint()
        replay = self._replay(request.request_id, fingerprint)
        if replay is not None:
            return replay
        fragments = build_fragments(request.messages, max_chars=self.settings.fragment_max_chars,
                                    content_format=self.settings.content_format)
        vectors = self._embed([fragment.text for fragment in fragments])
        with self.db.atomic():
            replay = self._replay(request.request_id, fingerprint)
            if replay is not None:
                return replay
            self.db.execute("INSERT INTO aml_requests(request_id, payload_hash, fragments, committed_at) "
                            "VALUES (?, ?, ?, ?)", (request.request_id, fingerprint, len(fragments), time.time()))
            for fragment, vector in zip(fragments, vectors, strict=True):
                record_id, *_ = self.store.save_knowledge(
                    request.session_id, fragment.text, KNOWLEDGE_TYPE, project=PROJECT, tags=[],
                    skip_dedup=True, skip_quality=True, source_format="conversation",
                    repeat="confirm", embedding=vector)
                if record_id is None:
                    raise Unavailable("storage rejected a fragment; retry the request")
                self.db.execute(
                    "INSERT INTO aml_fragments(knowledge_id, request_id, session_id, message_index, part_index,"
                    " role, timestamp_ms, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (record_id, request.request_id, request.session_id, fragment.message_index,
                     fragment.part_index, fragment.role, fragment.timestamp_ms, fragment.text))
        self._invalidate_caches()
        return {"fragments": len(fragments), "replayed": False}

    def _replay(self, request_id: str, fingerprint: str) -> dict | None:
        row = self.db.execute("SELECT payload_hash, fragments FROM aml_requests WHERE request_id=?",
                              (request_id,)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise Conflict("request_id was already used with a different payload")
        return {"fragments": row[1], "replayed": True}

    def _invalidate_caches(self) -> None:
        if self.store.cache is not None:
            self.store.cache.invalidate()
        if getattr(self.store, "v9_cache", None) is not None:
            self.store.v9_cache.invalidate_all()

    def search(self, request: SearchRequest) -> list[dict]:
        from memory_core.retrieval import flatten_results

        query = content_text(request.query)
        if self.settings.query_include_options and request.options:
            query = "\n".join([query, *request.options])
        limit = min(request.top_k, self.settings.max_top_k)
        if self.db.execute("SELECT 1 FROM aml_fragments LIMIT 1").fetchone() is None:
            return []
        self.store._semantic_diagnostics = []
        result = self.recall.search(query, project=PROJECT, limit=limit, record_usage=False)
        if result.get("semantic_diagnostics"):
            raise Unavailable("semantic retrieval is unavailable; retry the request")
        hits = flatten_results(result)
        ranked = sorted(enumerate(hits), key=lambda item: (-_score(item[1]), item[0]))
        rows = self._fragments_for([int(hit["id"]) for _, hit in ranked])
        output: list[dict] = []
        seen: set[int] = set()
        for _, hit in ranked:
            row = rows.get(int(hit["id"]))
            if row is None or row["id"] in seen:
                continue
            seen.add(row["id"])
            item = {"id": f"{self.settings.id_prefix}-{row['id']}", "content": row["content"],
                    "score": _score(hit)}
            if row["timestamp_ms"] is not None:
                item["created_at"] = iso_from_ms(row["timestamp_ms"])
            output.append(item)
            if len(output) == limit:
                break
        return output

    def _fragments_for(self, knowledge_ids: list[int]) -> dict[int, sqlite3.Row]:
        rows: dict[int, sqlite3.Row] = {}
        unique = list(dict.fromkeys(knowledge_ids))
        for offset in range(0, len(unique), SQLITE_MAX_PARAMS):
            batch = unique[offset:offset + SQLITE_MAX_PARAMS]
            marks = ",".join("?" for _ in batch)
            for row in self.db.execute(
                "SELECT f.knowledge_id, f.id, f.content, f.timestamp_ms FROM aml_fragments f "
                f"JOIN aml_requests r ON r.request_id = f.request_id WHERE f.knowledge_id IN ({marks})", batch,
            ):
                rows[row["knowledge_id"]] = row
        return rows


def _score(hit: dict) -> float:
    value = hit.get("score", hit.get("rrf_score"))
    return float(value) if value is not None else 0.0


def serve(connection, user_dir: str, settings_json: str, tam_env: dict[str, str]) -> None:
    """Worker loop: receive JSON work items over the pipe, reply with JSON."""
    os.environ.update(tam_env)
    os.environ.update(FORCED_TAM_ENV)
    os.environ["TAM_MEMORY_DIR"] = user_dir
    os.environ["CLAUDE_MEMORY_DIR"] = user_dir
    configure_logging()
    runtime: Runtime | None = None
    init_error: str | None = None
    try:
        runtime = Runtime(RuntimeSettings(**json.loads(settings_json)))
    except Exception as exc:
        LOGGER.exception(json.dumps({"event": "aml_worker_init_failed", "error_type": type(exc).__name__}))
        init_error = f"worker initialisation failed: {type(exc).__name__}: {exc}"
    try:
        while True:
            payload = connection.recv()
            if payload is None:
                break
            if runtime is None:
                connection.send(json.dumps({"ok": False, "code": "fatal", "error": init_error}))
                break
            connection.send(json.dumps(_execute(runtime, json.loads(payload))))
    except EOFError:
        LOGGER.info(json.dumps({"event": "aml_worker_pipe_closed"}))
    finally:
        if runtime is not None:
            runtime.db.close()
        connection.close()


def _execute(runtime: Runtime, work: dict) -> dict:
    operation = work.get("op")
    try:
        if operation == "add":
            return {"ok": True, "data": runtime.add(AddRequest.model_validate(work["request"]))}
        if operation == "search":
            return {"ok": True, "data": runtime.search(SearchRequest.model_validate(work["request"]))}
        return {"ok": False, "code": "internal", "error": f"unknown operation {operation!r}"}
    except AdapterError as exc:
        if runtime.db.in_transaction:
            runtime.db.rollback()
        return {"ok": False, "code": exc.code, "error": str(exc)}
    except Exception as exc:
        LOGGER.exception(json.dumps({"event": "aml_worker_operation_failed", "operation": operation,
                                     "error_type": type(exc).__name__}))
        if runtime.db.in_transaction:
            runtime.db.rollback()
        runtime._invalidate_caches()
        return {"ok": False, "code": "unavailable", "error": f"{operation} failed: {type(exc).__name__}"}
