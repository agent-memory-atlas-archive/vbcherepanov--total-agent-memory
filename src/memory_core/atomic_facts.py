from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from memory_core.event_time import normalize_event_time
from memory_core.query_terms import lexical_terms
from memory_core.retrieval import MemoryHit, SearchScope
from memory_core.telemetry import counters, op_timer
from tam_db.contracts import Backend

HISTORY_RECORDS = 6


class InvalidExtraction(ValueError):
    pass


class SourceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    id: int
    content: str
    project: str
    session_id: str
    branch: str
    observed_at: str = ""


@dataclass(frozen=True)
class Citation:
    source_id: int
    quote: str


@dataclass(frozen=True)
class AtomicFact:
    subject: str
    predicate: str
    object: str
    temporal_text: str
    sources: tuple[Citation, ...]

    @property
    def content(self) -> str:
        return f"{self.subject} {self.predicate} {self.object} {self.temporal_text}".strip()


class FactRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def sources(self, knowledge_id: int) -> tuple[Source, ...]:
        target = self.db.execute(
            "SELECT id, content, project, session_id, COALESCE(branch,''), COALESCE(created_at,'') FROM knowledge "
            "WHERE id=? AND status='active'",
            (knowledge_id,),
        ).fetchone()
        if target is None:
            return ()
        rows = self.db.execute(
            "SELECT id, content, project, session_id, COALESCE(branch,''), COALESCE(created_at,'') FROM knowledge "
            "WHERE id<=? AND project=? AND session_id=? AND COALESCE(branch,'')=? "
            "AND status='active' ORDER BY id DESC LIMIT ?",
            (knowledge_id, target[2], target[3], target[4], HISTORY_RECORDS),
        ).fetchall()
        return tuple(Source(*row) for row in reversed(rows))

    def completed(self, source: Source) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM atomic_fact_runs WHERE knowledge_id=? AND source_content=?",
                (source.id, source.content),
            ).fetchone()
            is not None
        )

    def pending(self, project: str, limit: int) -> list[int]:
        if not project or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("A project and limit between 1 and 10000 are required")
        rows = self.db.execute(
            "SELECT k.id FROM knowledge k LEFT JOIN atomic_fact_runs r ON r.knowledge_id=k.id "
            "WHERE k.project=? AND k.status='active' AND r.knowledge_id IS NULL "
            "ORDER BY k.id LIMIT ?",
            (project, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def replace(
        self,
        target: Source,
        sources: tuple[Source, ...],
        facts: tuple[AtomicFact, ...],
        model: str,
    ) -> int:
        self.db.execute("SAVEPOINT atomic_fact_replace")
        try:
            for source in sources:
                current = self.db.execute(
                    "SELECT id, content, project, session_id, COALESCE(branch,''), COALESCE(created_at,'') "
                    "FROM knowledge WHERE id=? AND status='active'",
                    (source.id,),
                ).fetchone()
                if current is None or Source(*current) != source:
                    raise SourceChanged(
                        f"Source changed during extraction: {source.id}"
                    )
            self.db.execute(
                "DELETE FROM atomic_facts WHERE knowledge_id=?", (target.id,)
            )
            self.db.execute(
                "DELETE FROM atomic_fact_dependencies WHERE target_id=?", (target.id,)
            )
            self.db.executemany(
                "INSERT INTO atomic_fact_dependencies VALUES (?,?)",
                [(target.id, source.id) for source in sources],
            )
            created = 0
            for fact in facts:
                dates = {
                    source.observed_at
                    for source in sources
                    for citation in fact.sources
                    if citation.source_id == source.id
                    and fact.temporal_text
                    and fact.temporal_text in citation.quote
                }
                anchor_at = next(iter(dates)) if len(dates) == 1 else ""
                event = normalize_event_time(fact.temporal_text, anchor_at)
                event_key = hashlib.sha256(
                    f"{target.id}:{fact.content}:{target.observed_at}".encode()
                ).hexdigest()
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO atomic_facts "
                    "(knowledge_id,subject,predicate,object,temporal_text,content,observed_at,"
                    "event_start,event_end,event_precision,event_key,temporal_anchor_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        target.id,
                        fact.subject,
                        fact.predicate,
                        fact.object,
                        fact.temporal_text,
                        fact.content,
                        target.observed_at,
                        event.start,
                        event.end,
                        event.precision,
                        event_key,
                        anchor_at,
                    ),
                )
                if cursor.rowcount:
                    created += 1
                    self.db.executemany(
                        "INSERT INTO atomic_fact_sources(fact_id,knowledge_id,quote) VALUES (?,?,?)",
                        [
                            (cursor.lastrowid, ref.source_id, ref.quote)
                            for ref in fact.sources
                        ],
                    )
            self.db.execute(
                "INSERT OR REPLACE INTO atomic_fact_runs VALUES (?,?,?,?)",
                (target.id, target.content, created, model),
            )
            self.db.execute(
                "DELETE FROM atomic_fact_rebuild WHERE knowledge_id=?", (target.id,)
            )
            self.db.execute("RELEASE atomic_fact_replace")
            return created
        except (sqlite3.Error, SourceChanged):
            self.db.execute("ROLLBACK TO atomic_fact_replace")
            self.db.execute("RELEASE atomic_fact_replace")
            raise

    def search(
        self, query: str, scope: SearchScope, limit: int = 30
    ) -> list[MemoryHit]:
        with op_timer("atomic_fact_search_ms"):
            counters.bump("atomic_fact_search_calls")
            return self._search(query, scope, limit)

    def _search(self, query: str, scope: SearchScope, limit: int) -> list[MemoryHit]:
        terms = lexical_terms(query)
        if not terms or limit <= 0:
            return []
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        predicates, scope_params = scope.sql()
        if getattr(self.db, "backend", None) is Backend.POSTGRES:
            from memory_core.pg_fts import match_source

            source, source_params = match_source("atomic_facts_fts", expression)
            rows = self.db.execute(
                f"SELECT f.id FROM ({source}) m JOIN atomic_facts f ON f.id=m.id "
                "JOIN knowledge k ON k.id=f.knowledge_id WHERE "
                + " AND ".join(predicates)
                + " ORDER BY m.rank, f.id LIMIT ?",
                [*source_params, *scope_params, limit],
            ).fetchall()
        else:
            predicates.insert(0, "atomic_facts_fts MATCH ?")
            rows = self.db.execute(
                "SELECT f.id FROM atomic_facts_fts JOIN atomic_facts f ON f.id=atomic_facts_fts.rowid "
                "JOIN knowledge k ON k.id=f.knowledge_id WHERE "
                + " AND ".join(predicates)
                + " ORDER BY bm25(atomic_facts_fts) LIMIT ?",
                [expression, *scope_params, limit],
            ).fetchall()
        if not rows:
            return []
        fact_ids = [row[0] for row in rows]
        predicates, scope_params = scope.sql()
        placeholders = ",".join("?" for _ in fact_ids)
        sources = self.db.execute(
            "SELECT s.fact_id, k.* FROM atomic_fact_sources s "
            "CROSS JOIN knowledge k ON k.id=s.knowledge_id "
            f"WHERE s.fact_id IN ({placeholders}) AND "
            + " AND ".join(predicates)
            + " ORDER BY k.id DESC",
            [*fact_ids, *scope_params],
        ).fetchall()
        grouped: dict[int, list[MemoryHit]] = {}
        for source in sources:
            hit = dict(source)
            grouped.setdefault(hit.pop("fact_id"), []).append(hit)
        hits: list[MemoryHit] = []
        seen: set[int] = set()
        for identity in fact_ids:
            for hit in grouped.get(identity, []):
                if hit["id"] not in seen:
                    seen.add(hit["id"])
                    hits.append(hit)
                    if len(hits) == limit:
                        return hits
        counters.bump("atomic_fact_search_candidates", len(hits))
        return hits
