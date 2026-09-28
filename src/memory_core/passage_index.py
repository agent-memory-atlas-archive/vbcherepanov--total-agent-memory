from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from memory_core.query_terms import lexical_terms
from memory_core.retrieval import MemoryHit
from memory_core.telemetry import counters, op_timer
from tam_db.contracts import Backend

PASSAGE_CHARS = 900
EMBED_BATCH_SIZE = 64
RANK_OFFSET = 60
INDEX_VERSION = 'passages-v1'
_ROLE = re.compile(r'(?m)^(?:\[[^\n]+\]\s*)?([\w][\w .-]{0,40}):[ \t]+')
EmbedTexts = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True)
class Passage:
    ordinal: int
    start: int
    end: int
    speaker: str
    content: str


@dataclass(frozen=True)
class RankedPassage:
    source_id: int
    passage: Passage
    score: float
    semantic_rank: int
    lexical_rank: int | None


def split_passages(content: str) -> list[Passage]:
    roles = list(_ROLE.finditer(content))
    boundaries = sorted({0, len(content), *(m.start() for m in roles)})
    result = []
    role_index = 0
    for left, right in pairwise(boundaries):
        while role_index + 1 < len(roles) and roles[role_index + 1].start() <= left:
            role_index += 1
        speaker = roles[role_index].group(1) if roles and roles[role_index].start() <= left else ''
        start = left
        while start < right:
            end = min(start + PASSAGE_CHARS, right)
            if end < right:
                boundary = max(content.rfind('\n', start, end), content.rfind('. ', start, end))
                if boundary > start:
                    end = boundary + 1
            result.append(Passage(len(result), start, end, speaker, content[start:end]))
            start = end
    return result


class PassageIndex:
    def __init__(self, db: sqlite3.Connection, embed: EmbedTexts, model: str,
                 query_embed: Callable[[str], list[float]] | None = None):
        self.db = db
        self.embed = embed
        self.model = f'{model}:{INDEX_VERSION}'
        self.query_embed = query_embed

    def ensure(self, sources: Sequence[MemoryHit]) -> None:
        with op_timer('passage_index_ms'):
            for source in sources:
                content = source['content']
                fingerprint = hashlib.sha256(content.encode()).hexdigest()
                cached = self.db.execute('SELECT fingerprint,model FROM passage_sources WHERE knowledge_id=?', (source['id'],)).fetchone()
                if cached and tuple(cached) == (fingerprint, self.model):
                    counters.bump('passage_index_cache_hits')
                    continue
                passages = split_passages(content)
                inserts = []
                for offset in range(0, len(passages), EMBED_BATCH_SIZE):
                    batch = passages[offset:offset + EMBED_BATCH_SIZE]
                    vectors = self.embed([p.content for p in batch])
                    if len(vectors) != len(batch):
                        raise ValueError('Passage encoder returned an incomplete batch')
                    for passage, vector in zip(batch, vectors, strict=True):
                        array = np.asarray(vector, dtype='<f4')
                        if array.ndim != 1 or not array.size or not np.isfinite(array).all():
                            raise ValueError('Invalid passage embedding')
                        inserts.append((source['id'], passage.ordinal, passage.start, passage.end,
                                        passage.speaker, passage.content, array.tobytes()))
                self.db.execute('SAVEPOINT passage_index_build')
                try:
                    actual = self.db.execute('SELECT content,status FROM knowledge WHERE id=?', (source['id'],)).fetchone()
                    if not actual or actual['content'] != content or actual['status'] != 'active':
                        raise ValueError('Source changed while building the passage index; retry retrieval')
                    self.db.execute('DELETE FROM passage_sources WHERE knowledge_id=?', (source['id'],))
                    self.db.execute('INSERT INTO passage_sources VALUES(?,?,?)', (source['id'], fingerprint, self.model))
                    self.db.executemany('INSERT INTO evidence_passages(knowledge_id,ordinal,start_char,end_char,speaker,content,vector) VALUES(?,?,?,?,?,?,?)', inserts)
                    self.db.execute('RELEASE passage_index_build')
                    counters.bump('passage_index_sources')
                except Exception:
                    self.db.execute('ROLLBACK TO passage_index_build')
                    self.db.execute('RELEASE passage_index_build')
                    counters.bump('passage_index_errors')
                    raise

    def rank(self, query: str, source_ids: list[int]) -> list[RankedPassage]:
        if not source_ids:
            return []
        with op_timer('passage_search_ms'):
            marks = ','.join('?' for _ in source_ids)
            rows = self.db.execute(f'SELECT * FROM evidence_passages WHERE knowledge_id IN ({marks}) ORDER BY knowledge_id,ordinal', source_ids).fetchall()
            if not rows:
                return []
            terms = lexical_terms(query)
            match = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
            if match and getattr(self.db, 'backend', None) is Backend.POSTGRES:
                from memory_core.pg_fts import match_source

                source, source_params = match_source('evidence_passages_fts', match)
                lexical = self.db.execute(
                    f'SELECT p.id FROM ({source}) m JOIN evidence_passages p ON p.id=m.id '
                    f'WHERE p.knowledge_id IN ({marks}) ORDER BY m.rank,p.id',
                    [*source_params, *source_ids],
                ).fetchall()
            else:
                lexical = self.db.execute(
                    f'SELECT p.id FROM evidence_passages_fts JOIN evidence_passages p ON p.id=evidence_passages_fts.rowid '
                    f'WHERE evidence_passages_fts MATCH ? AND p.knowledge_id IN ({marks}) ORDER BY rank,p.id',
                    [match, *source_ids],
                ).fetchall() if match else []
            vectors = np.asarray([np.frombuffer(r['vector'], dtype='<f4') for r in rows])
            query_vector = np.asarray(self.query_embed(query) if self.query_embed else self.embed([query])[0], dtype=np.float32)
            if vectors.shape[1:] != query_vector.shape or not np.isfinite(query_vector).all():
                raise ValueError('Query and passage embedding dimensions must match')
            norms = np.linalg.norm(vectors, axis=1) * np.linalg.norm(query_vector)
            cosine = (vectors @ query_vector) / np.maximum(norms, np.finfo(np.float32).tiny)
            semantic_order = sorted(range(len(rows)), key=lambda i: (-float(cosine[i]), rows[i]['id']))
            semantic_ranks = {rows[i]['id']: rank for rank, i in enumerate(semantic_order, 1)}
            lexical_ranks = {row['id']: rank for rank, row in enumerate(lexical, 1)}
            scores = {rows[i]['id']: 1 / (RANK_OFFSET + rank) for rank, i in enumerate(semantic_order, 1)}
            for rank, row in enumerate(lexical, 1):
                if row['id'] in scores:
                    scores[row['id']] += 1 / (RANK_OFFSET + rank)
            result = [RankedPassage(r['knowledge_id'], Passage(r['ordinal'], r['start_char'], r['end_char'], r['speaker'], r['content']), scores[r['id']], semantic_ranks[r['id']], lexical_ranks.get(r['id'])) for r in rows]
            counters.bump('passage_search_calls')
            return sorted(result, key=lambda r: (-r.score, r.source_id, r.passage.ordinal))
