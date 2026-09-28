"""Hard erasure of a personal-memory record: the row, its earlier versions and everything derived.

`memory_delete` without `hard` only hides a record. This removes it for good: SQLite
rows (triggers take the FTS index, atomic facts, graph links and evidence passages),
vectors in every embedding space, queue and log rows, quotes of it inside later
versions, and the copies of its text in the raw call log. `secure_delete` zeroes the
freed pages and a WAL checkpoint truncates the log, so the text is not left on disk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

ERASED = "[erased]"
SECONDS_PER_DAY = 86_400

# Tables whose `knowledge_id` rows describe a record. Triggers on DELETE FROM knowledge
# already cover knowledge_fts, atomic_fact*, knowledge_nodes and passage_sources.
DERIVED_TABLES = (
    "embeddings", "enrichment_queue", "deep_enrichment_queue", "representations_queue",
    "triple_extraction_queue", "knowledge_representations", "knowledge_enrichment", "episode_facts",
    "entity_dedup_log", "filter_savings", "quality_gate_log", "write_intents", "knowledge_nodes_quarantine",
    "evidence_passages",
)
PAIR_TABLES = (("contradiction_log", ("new_knowledge_id", "candidate_knowledge_id")),
               ("relations", ("from_id", "to_id")))
# `memory_update` stores "Was: <first 200 chars of the old content>" in the new version's context.
QUOTED_PREFIX_CHARS = 200


class ErasureUnsupported(RuntimeError):
    """Hard erasure runs on the personal SQLite store only."""


def version_chain(db, record_id: int) -> list[int]:
    """`record_id` and every earlier version that was superseded into it, oldest last."""
    chain, frontier = [record_id], [record_id]
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = db.execute(f"SELECT id FROM knowledge WHERE superseded_by IN ({marks})", frontier).fetchall()
        frontier = [row[0] for row in rows if row[0] not in chain]
        chain.extend(frontier)
    return chain


def _existing_tables(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _fts_tables(db) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%USING fts5%'")]


def _merge_fts(db) -> None:
    """FTS5 keeps deleted tokens in older index segments until a merge; `optimize` rewrites them."""
    for table in _fts_tables(db):
        db.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")


CHROMA_DELETE_OPERATION = 3


def compact_chroma(chroma_dir: Path, ids: list[int]) -> None:
    """Remove the erased documents' text from Chroma's own SQLite file.

    Chroma replays writes from `embeddings_queue`, whose `metadata` holds the document text. The
    add/upsert entries of erased ids lose it (their DELETE entries stay for the vector index);
    then its FTS is merged and the file vacuumed.
    """
    path = chroma_dir / "chroma.sqlite3"
    if not path.is_file():
        return
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute("PRAGMA secure_delete=ON")
        with db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='embeddings_queue'").fetchone():
                marks = ",".join("?" * len(ids))
                db.execute(f"UPDATE embeddings_queue SET metadata=NULL WHERE id IN ({marks}) AND operation != ?",
                           (*[str(i) for i in ids], CHROMA_DELETE_OPERATION))
            _merge_fts(db)
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()


def scrub_raw_logs(raw_dir: Path, texts: list[str]) -> int:
    """Replace every copy of `texts` in the JSONL call logs; returns the number of files rewritten."""
    needles = sorted({json.dumps(t, ensure_ascii=False)[1:-1] for t in texts if t and t.strip()}, key=len,
                     reverse=True)
    if not needles or not raw_dir.is_dir():
        return 0
    rewritten = 0
    for path in sorted(raw_dir.glob("*.jsonl")):
        original = path.read_text(encoding="utf-8")
        cleaned = original
        for needle in needles:
            cleaned = cleaned.replace(needle, ERASED)
        if cleaned == original:
            continue
        fd, temp = tempfile.mkstemp(dir=raw_dir, prefix=".erase-", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cleaned)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        rewritten += 1
    return rewritten


def erase(store, record_id: int, raw_dir: Path, chroma_dir: Path | None = None) -> dict | None:
    """Erase `record_id` with its earlier versions; None when no such record exists."""
    if getattr(store, "is_postgres", False):
        raise ErasureUnsupported("Hard delete is available for personal (SQLite) memory only")
    db = store.db
    if db.execute("SELECT 1 FROM knowledge WHERE id=?", (record_id,)).fetchone() is None:
        return None
    ids = version_chain(db, record_id)
    marks = ",".join("?" * len(ids))
    rows = db.execute(f"SELECT id, content, context, project FROM knowledge WHERE id IN ({marks})", ids).fetchall()
    texts = [value for row in rows for value in (row[1], row[2]) if value]
    projects = sorted({row[3] for row in rows if row[3]})
    spaces = {row[0] for row in db.execute(
        f"SELECT DISTINCT embedding_space FROM embeddings WHERE knowledge_id IN ({marks})", ids)}

    tables = _existing_tables(db)
    db.execute("PRAGMA secure_delete=ON")
    try:
        for kid in ids:
            store._delete_embedding(kid)
        for table in DERIVED_TABLES:
            if table in tables:
                db.execute(f"DELETE FROM {table} WHERE knowledge_id IN ({marks})", ids)
        for table, columns in PAIR_TABLES:
            if table in tables:
                where = " OR ".join(f"{column} IN ({marks})" for column in columns)
                db.execute(f"DELETE FROM {table} WHERE {where}", ids * len(columns))
        for row in rows:
            quoted = (row[1] or "")[:QUOTED_PREFIX_CHARS]
            if quoted.strip():
                db.execute(f"UPDATE knowledge SET context=replace(context, ?, ?) "
                           f"WHERE instr(context, ?) > 0 AND id NOT IN ({marks})", (quoted, ERASED, quoted, *ids))
        db.execute(f"DELETE FROM knowledge WHERE id IN ({marks})", ids)
        _merge_fts(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA secure_delete=OFF")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    result = {"erased": ids, "projects": projects, "raw_logs_rewritten": scrub_raw_logs(raw_dir, texts)}
    for space in sorted({"text", *spaces}):
        collection = store._chroma_collection_for(space)
        if collection is None:
            continue
        try:
            collection.delete(ids=[str(kid) for kid in ids])
        except Exception as error:  # noqa: BLE001 — chroma raises its own types; SQLite erasure already committed
            result.setdefault("vector_store_errors", []).append(f"{space}: {error}")
    if chroma_dir is not None and store._chroma_client is not None:
        try:
            compact_chroma(chroma_dir, ids)
        except sqlite3.Error as error:
            result.setdefault("vector_store_errors", []).append(f"compact: {error}")
    return result


def prune_raw_logs(raw_dir: Path, days: str | None, now: float | None = None) -> int:
    """Remove call logs not written for `days` days; unset, blank or invalid keeps everything."""
    if not days or not str(days).strip().isdigit() or int(days) <= 0 or not raw_dir.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - int(days) * SECONDS_PER_DAY
    removed = 0
    for path in raw_dir.glob("*.jsonl"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed
