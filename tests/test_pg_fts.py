"""FTS5 MATCH / bm25() on PostgreSQL (memory_core.pg_fts) against real SQLite FTS5.

The same corpus is loaded into a fresh SQLite Store (FTS5 tables and triggers as
shipped) and into a PostgreSQL workspace (side tables from the baseline), then the
MATCH expressions TAM generates are run on both. Plan acceptance is a top-10 Jaccard
of at least 0.9 on English; the implementation reproduces FTS5 exactly, so these
tests also require the same matching rows with the same bm25() scores.
"""

from __future__ import annotations

import random
import re
import sqlite3
import unicodedata

import pytest

from memory_core import pg_fts
from memory_core.pg_fts import (
    And,
    FtsQueryError,
    Not,
    Or,
    Phrase,
    fts5_tokens,
    match_source,
    parse,
)

CORPUS_SEED = 20260925
CORPUS_DOCUMENTS = 1500
FACTS = 600
PASSAGES = 600
ERRORS = 300
EPISODES = 300
QUERY_SEED = 7
RANDOM_QUERIES = 80
TOP_K = 10
MIN_TOP_K_JACCARD = 0.9
SCORE_TOLERANCE = 1e-9
PROJECTS = ("general", "alpha", "бета", "team/core")
UNICODE_BATCH = 8192
# Code points whose case mapping post-dates SQLite's Unicode tables (Cherokee, Georgian
# Mtavruli, Osage, Adlam, ...): FTS5 leaves them as they are, PostgreSQL lowercases them.
MAX_CASE_DRIFT_CODE_POINTS = 450
CORE_SCRIPT_RANGES = ((0x0000, 0x036F), (0x0400, 0x04FF), (0x0590, 0x06FF), (0x0900, 0x097F),
                      (0x2000, 0x206F), (0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF))

VOCABULARY_TEXT = """
the of and to in is for on that with as be by this are from or at it an not was can will all have
server database backup restore worker team memory recall save record project session query index
vector search token config install docker postgres sqlite migration schema table column trigger
python module function error retry timeout lease lock queue enrich embed model provider dashboard
admin user role password secret policy audit history export import report insight learning
workspace personal shared department release version test build deploy commit branch merge review
quick brown fox jumps over lazy dog alpha beta gamma delta kernel network latency throughput cache
memory leak crash panic stack trace log metric counter histogram alert incident outage recovery
document paragraph sentence phrase keyword ranking score relevance precision latency budget
customer invoice payment order shipment warehouse inventory supplier contract renewal discount
meeting agenda decision action owner deadline milestone roadmap quarter planning estimate risk
"""
VOCABULARY = VOCABULARY_TEXT.split()
FIXED_DOCUMENTS = (
    "See src/server.py and src/memory_core/pg_fts.py for the e-mail parser (user@example.org).",
    "Café naïve façade: déjà vu at the Zürich office — rôle of the coördinator.",
    "Маша живёт в Москве, а Маше нравится Санкт-Петербург. Машу ждут на встрече.",
    "Ёлка и елка: FTS5 не сворачивает ё, поэтому это разные токены.",
    "snake_case identifiers like max_retry_count and CamelCase ServerLease objects",
    "Version 14.6.0 ships PostgreSQL 18 support; x² and ½ are numbers too.",
    "日本語のテキストと中文文本 mixed with English words server backup",
    "quick brown fox; the quick brown fox jumps; brown quick fox",
)
QUESTIONS = (
    "How does the team server backup work?",
    "What is the recall latency budget for postgres?",
    "Где живёт Маша?",
    "src/server.py migration",
    "café coördinator",
    "max_retry_count",
    "ёлка",
    "日本語",
)


def _corpus() -> list[tuple[int, str, str, str, str]]:
    rng = random.Random(CORPUS_SEED)
    weights = [1 / (rank + 1) for rank in range(len(VOCABULARY))]
    rows = []
    for index in range(CORPUS_DOCUMENTS):
        length = rng.randint(4, 120)
        words = rng.choices(VOCABULARY, weights=weights, k=length)
        text = " ".join(words).capitalize() + "."
        if index < len(FIXED_DOCUMENTS):
            text = FIXED_DOCUMENTS[index]
        context = rng.choice(("", "setup notes", "incident review", "quick fix for the server"))
        tags = '["' + '","'.join(rng.sample(VOCABULARY[40:80], rng.randint(0, 3))) + '"]'
        rows.append((index + 1, text, context, tags, PROJECTS[index % len(PROJECTS)]))
    return rows


def _sentences(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [" ".join(rng.choices(VOCABULARY, k=rng.randint(3, 25))) for _ in range(count)]


# ─── parser (no database) ───────────────────────────────────────────────────

def test_fts5_tokens_match_unicode61():
    assert fts5_tokens("Hello, src/server.py Ärger") == ("hello", "src", "server", "py", "arger")
    assert fts5_tokens("Ёлка") == ("ёлка",)
    assert fts5_tokens("!!!") == ()


def test_parse_builds_fts5_precedence():
    assert parse('"a" OR "b" "c"', "knowledge_fts") == Or((
        Phrase(("a",), False), And((Phrase(("b",), False), Phrase(("c",), False)))))
    assert parse('"a" NOT "b" AND "c"', "knowledge_fts") == And((
        Not(Phrase(("a",), False), Phrase(("b",), False)), Phrase(("c",), False)))
    assert parse('"src/server.py"*', "knowledge_fts") == Phrase(("src", "server", "py"), True)
    assert parse("foo + bar", "knowledge_fts") == Phrase(("foo", "bar"), False)
    assert parse("and or", "knowledge_fts") == And((Phrase(("and",), False), Phrase(("or",), False)))


def test_parse_column_filters():
    text = frozenset({"content", "context", "tags"})
    assert parse('{content context tags} : ("a" OR "b") AND fts_project : p61', "knowledge_fts") == And((
        Or((Phrase(("a",), False, text), Phrase(("b",), False, text))),
        Phrase(("p61",), False, frozenset({"fts_project"}))))
    assert parse("- content : a", "knowledge_fts") == Phrase(
        ("a",), False, frozenset({"context", "tags", "fts_project"}))
    assert parse('{content tags} : (context : a)', "knowledge_fts") == Phrase(("a",), False, frozenset())
    assert parse("CONTENT : a", "knowledge_fts") == Phrase(("a",), False, frozenset({"content"}))


def test_empty_phrases_are_dropped_like_fts5():
    assert parse('"x" "!!"', "knowledge_fts") == Phrase(("x",), False)
    assert parse('"x" NOT "!!"', "knowledge_fts") == Phrase(("x",), False)
    assert parse('"!!" OR "..."', "knowledge_fts") is None


@pytest.mark.parametrize("expression", [
    '"unterminated', "a AND", "NOT", "(a", "a)", "", "nosuch : a", "NEAR(a b)", "a ^b", 'a* + b', "a ; b",
])
def test_malformed_or_unsupported_expressions_raise(expression):
    with pytest.raises(FtsQueryError) as caught:
        match_source("knowledge_fts", expression)
    assert isinstance(caught.value, sqlite3.OperationalError)


def test_parser_accepts_what_fts5_accepts():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE knowledge_fts USING fts5(content, context, tags, fts_project)")
    expressions = ['"a" OR "b"', 'a b c', 'a AND (b OR c)', 'a NOT b', '{content tags} : (a OR b)',
                   'content : ("a" "b") AND fts_project : p1', '"маш"* OR "мир"', 'a*', '- tags : a',
                   '"a"', 'x AND y OR z', 'a AND', '(a', 'a OR', 'NOT a', 'nosuch : a']
    for expression in expressions:
        try:
            db.execute("SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH ?", (expression,))
            fts5_ok = True
        except sqlite3.OperationalError:
            fts5_ok = False
        try:
            match_source("knowledge_fts", expression)
            ours_ok = True
        except FtsQueryError:
            ours_ok = False
        assert ours_ok == fts5_ok, expression


def test_unknown_table_is_rejected():
    with pytest.raises(FtsQueryError, match="no PostgreSQL full-text index"):
        match_source("wiki_fts", '"a"')


def test_source_is_translator_safe():
    from tam_db.translate import translate

    sql, params = match_source("knowledge_fts", '{content context tags} : ("src/server.py" OR "маш"*) '
                                                'AND fts_project : p61', weights=(1.0, 1.0, 1.0, 0.0))
    translated = translate(f"SELECT id, rank FROM ({sql}) f")
    assert translated.sql.count("%s") == len(params)
    assert translated.sql.replace("%s", "?") == f"SELECT id, rank FROM ({sql}) f"


# ─── PostgreSQL ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engines(tmp_path_factory, pg_server):
    """(SQLite Store connection, PostgreSQL workspace PgConnection) holding the same corpus."""
    import server
    from tam_db import pg_connection
    from tests.pg_store_support import provision_workspace
    from tests.pg_support import fresh_database

    root = tmp_path_factory.mktemp("fts-parity")
    previous = server.MEMORY_DIR
    server.MEMORY_DIR = root
    try:
        store = server.Store()
    finally:
        server.MEMORY_DIR = previous
    with fresh_database(pg_server) as database:
        pg = pg_connection.connect(provision_workspace(database.url, "fts-parity"))
        pg.row_factory = sqlite3.Row
        try:
            for connection in (store.db, pg):
                _load(connection)
            _load_episode_fts(store.db)
            yield store.db, pg
        finally:
            pg.close()
            store.db.close()


def _load(connection) -> None:
    connection.executemany(
        "INSERT INTO knowledge (id, session_id, type, content, context, tags, project, created_at) "
        "VALUES (?, 's', 'fact', ?, ?, ?, ?, '2026-01-01T00:00:00Z')", _corpus())
    facts = _sentences(FACTS, CORPUS_SEED + 1)
    connection.executemany(
        "INSERT INTO atomic_facts (id, knowledge_id, subject, predicate, object, content) "
        "VALUES (?, ?, 's', 'p', ?, ?)",
        [(index + 1, index % CORPUS_DOCUMENTS + 1, str(index), text) for index, text in enumerate(facts)])
    connection.executemany("INSERT INTO passage_sources (knowledge_id, fingerprint, model) VALUES (?, 'f', 'm')",
                           [(index + 1,) for index in range(PASSAGES)])
    connection.executemany(
        "INSERT INTO evidence_passages (id, knowledge_id, ordinal, start_char, end_char, speaker, content) "
        "VALUES (?, ?, 0, 0, 1, 'user', ?)",
        [(index + 1, index + 1, text) for index, text in enumerate(_sentences(PASSAGES, CORPUS_SEED + 2))])
    errors = _sentences(ERRORS * 3, CORPUS_SEED + 3)
    connection.executemany(
        "INSERT INTO errors (id, session_id, category, description, context, fix, tags, created_at) "
        "VALUES (?, 's', 'bug', ?, ?, ?, '[]', '2026-01-01T00:00:00Z')",
        [(index + 1, errors[3 * index], errors[3 * index + 1], errors[3 * index + 2]) for index in range(ERRORS)])
    summaries = _sentences(EPISODES * 2, CORPUS_SEED + 4)
    connection.executemany(
        "INSERT INTO episodes_v11 (id, project, started_at, ended_at, participants, summary, outcome) "
        "VALUES (?, 'p', ?, ?, ?, ?, ?)",
        [(index + 1, f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z", "2026-02-01T00:00:00Z",
          '["' + '", "'.join(summaries[2 * index + 1].split()[:3]) + '"]', summaries[2 * index],
          None if index % 3 else "shipped") for index in range(EPISODES)])
    connection.commit()


def _load_episode_fts(db) -> None:
    """What memory_core.episodes.extractor writes into the contentless SQLite index."""
    import json

    for row in db.execute("SELECT id, summary, participants, outcome FROM episodes_v11").fetchall():
        db.execute("INSERT INTO episodes_v11_fts (rowid, summary, participants, outcome) VALUES (?, ?, ?, ?)",
                   (row[0], row[1], " ".join(json.loads(row[2])), row[3] or ""))
    db.commit()


def _sqlite_ranked(db, table: str, expression: str, weights: tuple[float, ...] | None):
    arguments = ", " + ", ".join(repr(weight) for weight in weights) if weights else ""
    return [(row[0], row[1]) for row in db.execute(
        f"SELECT rowid, bm25({table}{arguments}) FROM {table} WHERE {table} MATCH ? ORDER BY 2, 1",
        (expression,))]


def _pg_ranked(pg, table: str, expression: str, weights: tuple[float, ...] | None):
    sql, params = match_source(table, expression, weights=weights)
    return [(row[0], row[1]) for row in pg.execute(f"SELECT id, rank FROM ({sql}) f ORDER BY rank, id", params)]


def _jaccard(left: list, right: list) -> float:
    a, b = {row[0] for row in left[:TOP_K]}, {row[0] for row in right[:TOP_K]}
    return len(a & b) / len(a | b) if a | b else 1.0


def _assert_same_ranking(engines, table: str, expression: str, weights=None) -> float:
    db, pg = engines
    expected = _sqlite_ranked(db, table, expression, weights)
    actual = _pg_ranked(pg, table, expression, weights)
    assert dict(actual).keys() == dict(expected).keys(), expression
    scores = dict(actual)
    for rowid, score in expected:
        assert scores[rowid] == pytest.approx(score, rel=SCORE_TOLERANCE, abs=SCORE_TOLERANCE), (expression, rowid)
    return _jaccard(expected, actual)


def _queries() -> list[str]:
    rng = random.Random(QUERY_SEED)
    queries = [" ".join(rng.sample(VOCABULARY, rng.randint(1, 4))) for _ in range(RANDOM_QUERIES)]
    return queries + list(QUESTIONS)


@pytest.mark.postgres
def test_recall_queries_rank_like_fts5(engines):
    from memory_core.fts_schema import SCOPED_BM25_WEIGHTS, scoped_match
    from memory_core.query_terms import fts_match_query

    scoped_weights = tuple(float(weight) for weight in SCOPED_BM25_WEIGHTS.split(","))
    jaccards = []
    for query in _queries():
        expression = fts_match_query(query)
        jaccards.append(_assert_same_ranking(engines, "knowledge_fts", expression))
        for project in PROJECTS:
            jaccards.append(_assert_same_ranking(engines, "knowledge_fts", scoped_match(expression, project),
                                                 scoped_weights))
    assert sum(jaccards) / len(jaccards) >= MIN_TOP_K_JACCARD
    assert min(jaccards) == 1.0


@pytest.mark.postgres
def test_dedup_and_session_queries_match_fts5(engines):
    from memory_core.dedup import candidate_match_query, prefix_match_query
    from memory_core.fts_schema import project_token

    db, _ = engines
    rows = db.execute("SELECT content, project FROM knowledge WHERE id % 97 = 1").fetchall()
    for content, project in rows:
        for terms in (candidate_match_query(content), prefix_match_query(content)):
            if terms:
                _assert_same_ranking(engines, "knowledge_fts",
                                     f"content : ({terms}) AND fts_project : {project_token(project)}")
    for query in QUESTIONS:
        words = [word for word in query.split() if len(word) > 2]
        escaped = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
        if escaped:
            _assert_same_ranking(engines, "knowledge_fts", f"content : ({escaped})")


@pytest.mark.postgres
@pytest.mark.parametrize("expression", [
    '"quick brown"', '"brown quick fox"', '"quick brown"* OR "src/server.py"', 'serv* OR bac*',
    'server NOT backup', '(server OR backup) AND team', '{context tags} : (quick OR fix)', '- content : server',
    '"машу" OR "маш"*', 'ёлка OR елка', 'cafe OR naive', '"x²"', '日本語のテキストと中文文本', 'user example org',
    'max retry count', '"max_retry_count"', 'serverlease', '"14 6 0"', 'fts_project : pd0b1d0b5d182d0b0',
])
def test_query_syntax_ranks_like_fts5(engines, expression):
    _assert_same_ranking(engines, "knowledge_fts", expression)
    _assert_same_ranking(engines, "knowledge_fts", expression, (2.0, 0.5, 0.25, 0.0))


@pytest.mark.postgres
def test_other_fts_tables_rank_like_fts5(engines):
    from memory_core.episodes.retriever import _sanitize_fts
    from memory_core.query_terms import lexical_terms

    for query in _queries():
        terms = lexical_terms(query)
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        if not expression:
            continue
        _assert_same_ranking(engines, "atomic_facts_fts", expression)
        _assert_same_ranking(engines, "evidence_passages_fts", expression)
        _assert_same_ranking(engines, "errors_fts", expression)
        sanitized = _sanitize_fts(query)
        if sanitized:
            _assert_same_ranking(engines, "episodes_v11_fts", sanitized)


@pytest.mark.postgres
def test_empty_expression_matches_nothing(engines):
    _, pg = engines
    sql, params = match_source("knowledge_fts", '"!!!"')
    assert pg.execute(f"SELECT id, rank FROM ({sql}) f", params).fetchall() == []


@pytest.mark.postgres
def test_updates_and_deletes_keep_ranking_in_step(engines):
    db, pg = engines
    for connection in (db, pg):
        connection.execute("UPDATE knowledge SET content = 'rewritten server backup text' WHERE id = 20")
        connection.execute("UPDATE knowledge SET recall_count = recall_count + 1 WHERE id = 21")
        connection.execute("DELETE FROM knowledge WHERE id = 22")
        connection.execute("DELETE FROM atomic_facts WHERE id = 5")
        connection.commit()
    _assert_same_ranking(engines, "knowledge_fts", '"server" OR "backup" OR "rewritten"')
    _assert_same_ranking(engines, "atomic_facts_fts", '"server" OR "memory"')


@pytest.mark.postgres
def test_rebuild_restores_side_tables_and_statistics(engines):
    _, pg = engines
    before = _pg_ranked(pg, "knowledge_fts", '"server" OR "backup"', None)
    stats = pg.execute("SELECT source, doc_count, total_length FROM fts_stats ORDER BY source").fetchall()
    pg.execute_native("DELETE FROM knowledge_tsv WHERE id <= 100")
    pg.execute_native("UPDATE fts_stats SET doc_count = 0, total_length = 0")
    pg.commit()
    assert _pg_ranked(pg, "knowledge_fts", '"server" OR "backup"', None) != before
    pg_fts.rebuild(pg)
    pg.commit()
    assert _pg_ranked(pg, "knowledge_fts", '"server" OR "backup"', None) == before
    assert pg.execute("SELECT source, doc_count, total_length FROM fts_stats ORDER BY source").fetchall() == stats


@pytest.mark.postgres
def test_gin_indexes_serve_the_match_predicate(engines):
    _, pg = engines
    sql, params = match_source("knowledge_fts", '"coördinator"')
    pg.execute("BEGIN")
    try:
        pg.execute_native("SET LOCAL enable_seqscan = off")
        plan = "\n".join(row[0] for row in pg.execute_native(
            f"EXPLAIN SELECT id, rank FROM ({sql.replace('?', '%s')}) f", params))
    finally:
        pg.rollback()
    assert "knowledge_tsv_content" in plan


@pytest.mark.postgres
def test_tokenizer_matches_fts5_over_all_code_points(pg_database):
    """tam_fts_tokens() against FTS5 unicode61 for every Unicode scalar value."""
    from tam_db import pg_connection
    from tests.pg_store_support import provision_workspace

    pg = pg_connection.connect(provision_workspace(pg_database.url, "tokenizer"))
    lite = sqlite3.connect(":memory:")
    lite.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    lite.execute("CREATE VIRTUAL TABLE terms USING fts5vocab(t, 'instance')")
    code_points = [cp for cp in range(1, 0x110000) if not 0xD800 <= cp <= 0xDFFF]
    drift = []
    try:
        for start in range(0, len(code_points), UNICODE_BATCH):
            batch = code_points[start:start + UNICODE_BATCH]
            documents = ["a" + chr(cp) + "b" for cp in batch]
            lite.execute("DELETE FROM t")
            lite.executemany("INSERT INTO t (rowid, x) VALUES (?, ?)", list(enumerate(documents)))
            expected: dict[int, list[str]] = {}
            for doc, term in lite.execute("SELECT doc, term FROM terms ORDER BY doc, offset"):
                expected.setdefault(doc, []).append(term)
            rows = pg.execute_native("SELECT tam_fts_tokens(x) FROM unnest(%s::text[]) WITH ORDINALITY AS u(x, o) "
                                     "ORDER BY o", (documents,)).fetchall()
            for index, (tokens,) in enumerate(rows):
                if list(tokens) != expected.get(index, []):
                    drift.append((batch[index], expected.get(index, []), list(tokens)))
    finally:
        pg.close()
    for cp, fts5, postgres in drift:
        char = chr(cp)
        # Only case-mapping drift: FTS5 kept the letter, PostgreSQL lowercased it.
        assert fts5 == ["a" + char + "b"], hex(cp)
        assert len(postgres) == 1 and len(postgres[0]) == 3 and postgres[0][1] != char, hex(cp)
        assert not any(low <= cp <= high for low, high in CORE_SCRIPT_RANGES), (hex(cp), unicodedata.name(char, "?"))
    assert len(drift) <= MAX_CASE_DRIFT_CODE_POINTS


@pytest.mark.postgres
def test_long_tokens_are_cut_consistently(engines):
    db, pg = engines
    long_word = "x" * (pg_fts.MAX_TOKEN_CHARS + 50)
    for connection in (db, pg):
        connection.execute("INSERT INTO knowledge (id, session_id, type, content, created_at) "
                           "VALUES (99999, 's', 'fact', ?, '2026-01-01T00:00:00Z')", (f"blob {long_word} end",))
        connection.commit()
    stored = pg.execute_native("SELECT content FROM knowledge_tsv WHERE id = 99999").fetchone()[0]
    assert [len(token) for token in stored] == [4, pg_fts.MAX_TOKEN_CHARS, 3]
    assert [row[0] for row in _pg_ranked(pg, "knowledge_fts", f'"{long_word}"', None)] == [99999]
    assert re.fullmatch(r"x+", stored[1])
