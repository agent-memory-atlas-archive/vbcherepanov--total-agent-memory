"""SQLite -> PostgreSQL translation rules (pure Python, no server needed)."""

import sqlite3

import pytest

from tam_db.contracts import UntranslatableSQL
from tam_db.translate import (
    REPLACE_CONFLICT_TARGETS,
    TRANSLATION_CACHE_SIZE,
    StatementKind,
    TableShape,
    split_statements,
    translate,
)


class Catalog:
    def __init__(self, *shapes: TableShape):
        self.shapes = {shape.name: shape for shape in shapes}

    def table(self, name: str) -> TableShape | None:
        return self.shapes.get(name)


def sql(statement: str) -> str:
    """Translated text; ORDER BY NULLS clauses rendered against an empty catalog (all kept)."""
    translation = translate(statement)
    text = translation.sql
    if translation.null_orderings and translation.replace is None and translation.rowid_table is None:
        text = translation.render(Catalog())
    return " ".join(text.split())


KV = TableShape(name="kv", columns=("k", "v", "n"), rowid_column=None, unique_keys=(("k",),))
KNOWLEDGE = TableShape(name="knowledge", columns=("id", "content", "project", "search"), rowid_column="id",
                       unique_keys=(("id",), ("content",)), generated=frozenset({"search"}),
                       identity=frozenset({"id"}))
PAIR = TableShape(name="pair", columns=("a", "b", "c"), rowid_column=None, unique_keys=(("a",), ("b",)))


@pytest.mark.parametrize(("source", "expected"), [
    ("SELECT * FROM t WHERE a = ? AND b = ?", "SELECT * FROM t WHERE a = %s AND b = %s"),
    ("SELECT '100%', 5 % 2, ? LIKE '%x'", "SELECT '100%%', 5 %% 2, %s ILIKE '%%x' ESCAPE ''"),
    ("SELECT a FROM t WHERE a == 1", "SELECT a FROM t WHERE a = 1"),
    ("SELECT a FROM t WHERE a IS ?", "SELECT a FROM t WHERE a IS NOT DISTINCT FROM %s"),
    ("SELECT a FROM t WHERE a IS NOT b", "SELECT a FROM t WHERE a IS DISTINCT FROM b"),
    ("SELECT a FROM t WHERE a IS NULL OR b IS NOT NULL OR c IS TRUE",
     "SELECT a FROM t WHERE a IS NULL OR b IS NOT NULL OR c IS TRUE"),
    ("SELECT a FROM t WHERE a IS NOT DISTINCT FROM b", "SELECT a FROM t WHERE a IS NOT DISTINCT FROM b"),
    ("SELECT a FROM t WHERE a LIKE ? ESCAPE '\\'", "SELECT a FROM t WHERE a ILIKE %s ESCAPE '\\'"),
    ("SELECT a FROM t WHERE a NOT LIKE '%' || ? || '%' AND b = 1",
     "SELECT a FROM t WHERE a NOT ILIKE '%%' || %s || '%%' ESCAPE '' AND b = 1"),
    ("SELECT a FROM t WHERE lower(a) LIKE lower(?)", "SELECT a FROM t WHERE lower(a) ILIKE lower(%s) ESCAPE ''"),
    ("SELECT id FROM g INDEXED BY idx WHERE x = 1", "SELECT id FROM g WHERE x = 1"),
    ("SELECT k.* FROM s CROSS JOIN knowledge k ON k.id = s.kid WHERE s.f = ?",
     "SELECT k.* FROM s JOIN knowledge k ON k.id = s.kid WHERE s.f = %s"),
    ("SELECT * FROM a CROSS JOIN b USING (id)", "SELECT * FROM a JOIN b USING (id)"),
    ("SELECT * FROM a CROSS JOIN b WHERE a.x = b.x", "SELECT * FROM a CROSS JOIN b WHERE a.x = b.x"),
    ("SELECT * FROM a CROSS JOIN b JOIN c ON c.id = b.id", "SELECT * FROM a CROSS JOIN b JOIN c ON c.id = b.id"),
    ("SELECT id FROM g NOT INDEXED WHERE x = 1", "SELECT id FROM g WHERE x = 1"),
    ("SELECT id FROM g WHERE +status = 'a' AND x = +5", "SELECT id FROM g WHERE status = 'a' AND x = +5"),
    ("SELECT a + b FROM t", "SELECT a + b FROM t"),
    ("SELECT * FROM t LIMIT -1", "SELECT * FROM t LIMIT ALL"),
    ("SELECT * FROM t LIMIT 10, 5", "SELECT * FROM t LIMIT 5 OFFSET 10"),
    ("SELECT * FROM t LIMIT ? OFFSET ?", "SELECT * FROM t LIMIT %s OFFSET %s"),
    ("SELECT * FROM t WHERE id IN ()", "SELECT * FROM t WHERE id = ANY('{}')"),
    ("SELECT * FROM t WHERE id NOT IN ()", "SELECT * FROM t WHERE id <> ALL('{}')"),
    ("SELECT X'00FF'", "SELECT '\\x00ff'::bytea"),
    ("SELECT CAST(a AS INTEGER), CAST(b AS REAL), CAST(c AS BLOB), CAST(d AS TEXT) FROM t",
     "SELECT CAST(a AS bigint), CAST(b AS double precision), CAST(c AS bytea), CAST(d AS TEXT) FROM t"),
    ("SELECT integer, real, blob FROM t", "SELECT integer, real, blob FROM t"),
    ("SELECT json_object('a', x, 'b', 1) FROM t",
     "SELECT tam_compat.json_compact(json_build_object('a', x, 'b', 1)) FROM t"),
    ("SELECT char(65), char(72, 105)", "SELECT chr(65), (chr(72) || chr( 105))"),
    ("SELECT CURRENT_TIMESTAMP, CURRENT_DATE, CURRENT_TIME, t.current_date FROM t",
     "SELECT tam_compat.datetime('now'), tam_compat.date('now'), tam_compat.time('now'), t.current_date FROM t"),
    ("SELECT name FROM sqlite_schema", "SELECT name FROM sqlite_master"),
    ("SELECT [Weird Name], `Other`, \"Mixed\" FROM t", 'SELECT "weird name", "other", "mixed" FROM t'),
    ("SELECT a FROM t -- trailing ? comment", "SELECT a FROM t"),
    ("SELECT /* ? % */ a FROM t", "SELECT a FROM t"),
    ("SELECT a FROM t ORDER BY a COLLATE BINARY", 'SELECT a FROM t ORDER BY a COLLATE "C" NULLS FIRST'),
    ("SELECT a FROM t;", "SELECT a FROM t"),
    ("SELECT CAST(? AS text[]), (ARRAY[1, 2])[1] FROM t", "SELECT CAST(%s AS text[]), (ARRAY[1, 2])[1] FROM t"),
    ("SELECT ARRAY[?, 'a'], x[?] FROM t", "SELECT ARRAY[%s, 'a'], x[%s] FROM t"),
    ("SELECT a FROM t; ;", "SELECT a FROM t"),
])
def test_statement_rewrites(source, expected):
    assert sql(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("SELECT strftime('%s', 'now'), datetime('now', '-1 day'), date(x), time(x), julianday(x), unixepoch()",
     ("SELECT tam_compat.strftime('%%s', 'now'), tam_compat.datetime('now', '-1 day'), tam_compat.date(x), "
      "tam_compat.time(x), tam_compat.julianday(x), tam_compat.unixepoch()")),
    ("SELECT json_extract(a, '$.x'), instr(a, ':'), hex(b), ifnull(c, 0), glob('a*', d), group_concat(e)",
     ("SELECT tam_compat.json_extract(a, '$.x'), tam_compat.instr(a, ':'), tam_compat.hex(b), "
      "tam_compat.ifnull(c, 0), tam_compat.glob('a*', d), tam_compat.group_concat(e)")),
    ("SELECT round(x), round(x, 2), max(a, b), min(a, b, c), max(a), min(a) FROM t",
     ("SELECT tam_compat.round(x), tam_compat.round(x, 2), tam_compat.max(a, b), tam_compat.min(a, b, c), "
      "max(a), min(a) FROM t")),
    ("SELECT t.date, t.round FROM t", "SELECT t.date, t.round FROM t"),
])
def test_compat_functions_are_schema_qualified(source, expected):
    assert sql(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("SELECT a FROM t WHERE b GLOB 'x*'", "SELECT a FROM t WHERE tam_compat.glob_match(b , 'x*')"),
    ("SELECT a FROM t WHERE t.b NOT GLOB ? AND c = 1",
     "SELECT a FROM t WHERE NOT tam_compat.glob_match(t.b , %s) AND c = 1"),
    ("SELECT a FROM t WHERE lower(b) GLOB '*' || ?", "SELECT a FROM t WHERE tam_compat.glob_match(lower(b) , '*' || %s)"),
])
def test_glob_operator(source, expected):
    assert sql(source) == expected


@pytest.mark.parametrize(("source", "expected"), [
    ("SELECT a FROM t ORDER BY a", "SELECT a FROM t ORDER BY a NULLS FIRST"),
    ("SELECT a FROM t ORDER BY a ASC, b DESC LIMIT 5",
     "SELECT a FROM t ORDER BY a ASC NULLS FIRST, b DESC NULLS LAST LIMIT 5"),
    ("SELECT a FROM t ORDER BY a DESC NULLS FIRST, b NULLS LAST",
     "SELECT a FROM t ORDER BY a DESC NULLS FIRST, b NULLS LAST"),
    ("SELECT a FROM t ORDER BY a COLLATE BINARY DESC, lower(b), c + 1",
     'SELECT a FROM t ORDER BY a COLLATE "C" DESC NULLS LAST, lower(b) NULLS FIRST, c + 1 NULLS FIRST'),
    ("SELECT a FROM t ORDER BY CASE WHEN a IS NULL THEN 1 ELSE 0 END, a DESC LIMIT ? OFFSET ?",
     ("SELECT a FROM t ORDER BY CASE WHEN a IS NULL THEN 1 ELSE 0 END NULLS FIRST, a DESC NULLS LAST "
      "LIMIT %s OFFSET %s")),
    ("SELECT group_concat(x ORDER BY y DESC), group_concat(DISTINCT z) FROM t",
     "SELECT tam_compat.group_concat(x ORDER BY y DESC NULLS LAST), tam_compat.group_concat(DISTINCT z) FROM t"),
    ("SELECT row_number() OVER (PARTITION BY p ORDER BY q ROWS UNBOUNDED PRECEDING) FROM t",
     "SELECT row_number() OVER (PARTITION BY p ORDER BY q NULLS FIRST ROWS UNBOUNDED PRECEDING) FROM t"),
    ("SELECT rank() OVER w FROM t WINDOW w AS (ORDER BY q DESC)",
     "SELECT rank() OVER w FROM t WINDOW w AS (ORDER BY q DESC NULLS LAST)"),
    ("SELECT * FROM (SELECT a FROM t ORDER BY a) s ORDER BY s.a DESC",
     "SELECT * FROM (SELECT a FROM t ORDER BY a NULLS FIRST) s ORDER BY s.a DESC NULLS LAST"),
    ("SELECT a FROM t UNION ALL SELECT b FROM u ORDER BY 1", "SELECT a FROM t UNION ALL SELECT b FROM u ORDER BY 1 NULLS FIRST"),
    ("INSERT OR IGNORE INTO k SELECT a FROM t ORDER BY a",
     "INSERT INTO k SELECT a FROM t ORDER BY a NULLS FIRST ON CONFLICT DO NOTHING"),
    ("SELECT e.id FROM e ORDER BY CAST(e.v AS vector(16)) <=> CAST(? AS vector(16)) LIMIT ?",
     "SELECT e.id FROM e ORDER BY CAST(e.v AS vector(16)) <=> CAST(%s AS vector(16)) NULLS FIRST LIMIT %s"),
    ("SELECT id FROM t ORDER BY id DESC LIMIT 10", "SELECT id FROM t ORDER BY id DESC NULLS LAST LIMIT 10"),
    ("SELECT id FROM t ORDER BY CURRENT_TIMESTAMP DESC",
     "SELECT id FROM t ORDER BY tam_compat.datetime('now') DESC NULLS LAST"),
])
def test_order_by_gets_sqlite_null_placement(source, expected):
    assert sql(source) == expected


def test_null_placement_is_dropped_for_not_null_columns():
    catalog = Catalog(
        TableShape(name="knowledge", columns=("id", "created_at", "project"), rowid_column="id",
                   unique_keys=(("id",),), not_null=frozenset({"id", "created_at"})),
        TableShape(name="notes", columns=("id", "kid"), rowid_column="id", unique_keys=(("id",),),
                   not_null=frozenset({"id"})),
    )

    def render(statement: str) -> str:
        return " ".join(translate(statement).render(catalog).split())

    assert render("SELECT id FROM knowledge ORDER BY id DESC LIMIT 10") == (
        "SELECT id FROM knowledge ORDER BY id DESC LIMIT 10")
    assert render("SELECT id FROM knowledge ORDER BY created_at DESC, project") == (
        "SELECT id FROM knowledge ORDER BY created_at DESC, project NULLS FIRST")
    assert render("SELECT k.id FROM knowledge AS k JOIN notes n ON n.kid = k.id ORDER BY k.id DESC, n.kid, id") == (
        "SELECT k.id FROM knowledge AS k JOIN notes n ON n.kid = k.id ORDER BY k.id DESC, n.kid NULLS FIRST, "
        "id NULLS FIRST")
    assert render('SELECT id FROM knowledge ORDER BY "ID" COLLATE BINARY DESC') == (
        'SELECT id FROM knowledge ORDER BY "id" COLLATE "C" DESC')
    assert render("SELECT id FROM (SELECT id FROM knowledge) s ORDER BY s.id") == (
        "SELECT id FROM (SELECT id FROM knowledge) s ORDER BY s.id NULLS FIRST")
    assert render("SELECT id FROM knowledge ORDER BY id + 0 DESC") == (
        "SELECT id FROM knowledge ORDER BY id + 0 DESC NULLS LAST")
    assert render("SELECT id FROM missing ORDER BY id") == "SELECT id FROM missing ORDER BY id NULLS FIRST"


def test_null_placement_survives_rowid_resolution():
    catalog = Catalog(KNOWLEDGE)
    rendered = translate("SELECT content FROM knowledge ORDER BY rowid DESC LIMIT 1").render(catalog)
    assert " ".join(rendered.split()) == "SELECT content FROM knowledge ORDER BY id DESC NULLS LAST LIMIT 1"


def test_placeholders_and_parameter_order():
    numbered = translate("SELECT id FROM g WHERE n >= lower(?1) AND n < lower(?1) || 'x' AND s = ?2")
    assert numbered.param_count == 2
    assert numbered.param_order == (0, 0, 1)
    assert numbered.bind(("a", "b")) == ("a", "a", "b")

    mixed = translate("SELECT ?2, ?, ?1")
    assert mixed.param_count == 3
    assert mixed.bind(("x", "y", "z")) == ("y", "z", "x")

    swapped = translate("SELECT * FROM t WHERE a = ? LIMIT ?, ?")
    assert "LIMIT %s OFFSET %s" in swapped.sql
    assert swapped.bind(("a", 10, 5)) == ("a", 5, 10)

    named = translate("SELECT * FROM t WHERE a = :a AND b = :b_2 AND c = :a")
    assert named.named and named.param_count == 0
    assert "%(a)s" in named.sql and "%(b_2)s" in named.sql
    assert named.bind({"a": 1, "b_2": 2}) == {"a": 1, "b_2": 2}

    plain = translate("SELECT * FROM t WHERE a = ? AND b = ?")
    assert plain.param_order is None
    assert plain.bind(["x", "y"]) == ("x", "y")


def test_binding_errors_match_sqlite3():
    statement = translate("SELECT ?, ?")
    with pytest.raises(sqlite3.ProgrammingError, match="Incorrect number of bindings"):
        statement.bind((1,))
    with pytest.raises(sqlite3.ProgrammingError):
        translate("SELECT :a").bind((1,))
    assert translate("SELECT 1").bind(None) == ()
    with pytest.raises(sqlite3.ProgrammingError, match="one statement"):
        translate("SELECT 1; SELECT 2")


@pytest.mark.parametrize(("source", "reason"), [
    ("SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH ?", "FTS5"),
    ("SELECT id FROM knowledge WHERE content MATCH ?", "MATCH"),
    ("SELECT bm25(f) FROM f", "bm25"),
    ("SELECT highlight(f, 0, '[', ']') FROM f", "highlight"),
    ('INSERT INTO "errors_fts"(rowid, description) VALUES (?, ?)', "FTS5"),
    ("CREATE VIRTUAL TABLE x USING fts5(content)", "VIRTUAL"),
    ("CREATE TRIGGER t AFTER INSERT ON k BEGIN SELECT 1; END", "triggers"),
    ("CREATE TEMP TRIGGER t AFTER INSERT ON k BEGIN SELECT 1; END", "triggers"),
    ("DROP TRIGGER IF EXISTS t", "triggers"),
    ("ATTACH DATABASE 'x.db' AS x", "ATTACH"),
    ("VACUUM INTO '/tmp/x.db'", "VACUUM INTO"),
    ("SELECT a FROM t WHERE a REGEXP ?", "REGEXP"),
    ("SELECT a FROM t ORDER BY a COLLATE NOCASE", "NOCASE"),
    ("UPDATE OR IGNORE t SET a = 1", "UPDATE OR IGNORE"),
    ("SELECT @name", "parameter style"),
    ("SELECT :a, ?", "mixed"),
    ("SELECT 'unterminated", "unterminated"),
    ("SELECT a FROM t, u WHERE t.rowid = u.x", "several tables"),
    ("INSERT OR IGNORE INTO t(a) VALUES (1) ON CONFLICT (a) DO NOTHING", "ON CONFLICT"),
    ("BEGIN WORK", "BEGIN"),
    ("PRAGMA", "PRAGMA"),
])
def test_untranslatable_statements_fail_loudly(source, reason):
    with pytest.raises(UntranslatableSQL) as raised:
        translate(source)
    assert reason.lower() in str(raised.value).lower()
    assert raised.value.sql == source
    assert isinstance(raised.value, sqlite3.NotSupportedError)


def test_conflict_algorithms():
    ignore = translate("INSERT OR IGNORE INTO kv(k, v) VALUES (?, ?)")
    assert " ".join(ignore.sql.split()) == "INSERT INTO kv(k, v) VALUES (%s, %s) ON CONFLICT DO NOTHING"
    assert ignore.kind is StatementKind.INSERT and ignore.insert_table == "kv"

    returning = translate("INSERT OR IGNORE INTO kv(k) VALUES (?) RETURNING k")
    assert " ".join(returning.sql.split()) == "INSERT INTO kv(k) VALUES (%s) ON CONFLICT DO NOTHING RETURNING k"
    assert returning.has_returning

    select = translate("INSERT OR IGNORE INTO kv(k) SELECT name FROM other WHERE x = ?")
    assert select.sql.endswith("WHERE x = %s ON CONFLICT DO NOTHING")

    with_cte = translate("WITH c AS (SELECT 1 AS k) INSERT OR IGNORE INTO kv(k) SELECT k FROM c")
    assert with_cte.kind is StatementKind.OTHER and with_cte.sql.endswith("ON CONFLICT DO NOTHING")

    for algorithm in ("ABORT", "FAIL", "ROLLBACK"):
        assert " ".join(translate(f"INSERT OR {algorithm} INTO kv(k) VALUES (1)").sql.split()) == \
            "INSERT INTO kv(k) VALUES (1)"
        assert " ".join(translate(f"UPDATE OR {algorithm} kv SET v = 1").sql.split()) == "UPDATE kv SET v = 1"

    upsert = translate("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
    assert upsert.replace is None and "ON CONFLICT(k) DO UPDATE SET v = excluded.v" in upsert.sql

    qualified = translate("INSERT INTO main.kv(k) VALUES (1)")
    assert qualified.insert_table == "kv" and "main" not in qualified.sql


def test_replace_resolves_the_conflict_target_from_the_catalog():
    catalog = Catalog(KV, KNOWLEDGE, PAIR)
    replace = translate("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)")
    assert replace.needs_catalog
    assert " ".join(replace.render(catalog).split()) == (
        "INSERT INTO kv(k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, n = DEFAULT")

    into = translate("REPLACE INTO kv VALUES (?, ?, ?)")
    assert " ".join(into.render(catalog).split()) == (
        "INSERT INTO kv VALUES (%s, %s, %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, n = EXCLUDED.n")

    keys_only = translate("INSERT OR REPLACE INTO kv(k, v, n) VALUES (1, 2, 3) RETURNING k")
    assert "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, n = EXCLUDED.n RETURNING k" in keys_only.render(catalog)

    only_key = translate("INSERT OR REPLACE INTO kv(k) VALUES (1)")
    assert "ON CONFLICT (k) DO UPDATE SET v = DEFAULT, n = DEFAULT" in only_key.render(catalog)

    by_content = translate("INSERT OR REPLACE INTO knowledge(content, project) VALUES (?, ?)")
    rendered = " ".join(by_content.render(catalog).split())
    assert rendered.endswith("ON CONFLICT (content) DO UPDATE SET project = EXCLUDED.project")

    ambiguous = translate("INSERT OR REPLACE INTO pair(a, b, c) VALUES (1, 2, 3)")
    with pytest.raises(UntranslatableSQL, match="several unique keys"):
        ambiguous.render(catalog)
    REPLACE_CONFLICT_TARGETS["pair"] = ("b",)
    try:
        assert "ON CONFLICT (b) DO UPDATE SET a = EXCLUDED.a, c = EXCLUDED.c" in ambiguous.render(catalog)
    finally:
        del REPLACE_CONFLICT_TARGETS["pair"]

    with pytest.raises(UntranslatableSQL, match="no complete unique key"):
        translate("INSERT OR REPLACE INTO kv(v) VALUES (1)").render(catalog)
    with pytest.raises(UntranslatableSQL, match="unknown table"):
        translate("INSERT OR REPLACE INTO missing(v) VALUES (1)").render(catalog)


def test_rowid_resolves_to_the_integer_primary_key():
    catalog = Catalog(KV, KNOWLEDGE)
    statement = translate("SELECT rowid, content FROM knowledge WHERE _rowid_ > ? ORDER BY rowid DESC")
    assert statement.rowid_table == "knowledge"
    assert " ".join(statement.render(catalog).split()) == (
        "SELECT id, content FROM knowledge WHERE id > %s ORDER BY id DESC NULLS LAST")
    with pytest.raises(UntranslatableSQL, match="no integer primary key"):
        translate("SELECT rowid FROM kv").render(catalog)


def test_ddl_types_and_identity():
    assert sql("CREATE TABLE IF NOT EXISTS x (id INTEGER PRIMARY KEY AUTOINCREMENT, v REAL, b BLOB, "
               "n INTEGER NOT NULL DEFAULT 0, blob BLOB, at TEXT DEFAULT (strftime('%Y','now'))) WITHOUT ROWID") == (
        "CREATE TABLE IF NOT EXISTS x (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY , "
        "v double precision, b bytea, n bigint NOT NULL DEFAULT 0, blob bytea, "
        "at TEXT DEFAULT (tam_compat.strftime('%%Y','now')))")
    assert sql("CREATE TABLE s (a TEXT, b INTEGER) STRICT") == "CREATE TABLE s (a TEXT, b bigint)"
    assert sql("CREATE TABLE p (k INTEGER PRIMARY KEY DESC, v TEXT)") == (
        "CREATE TABLE p (k bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY , v TEXT)")
    assert sql("ALTER TABLE x ADD COLUMN score REAL DEFAULT 0") == (
        "ALTER TABLE x ADD COLUMN score double precision DEFAULT 0")
    assert sql("CREATE INDEX IF NOT EXISTS i ON x(n) WHERE n IS NOT NULL") == (
        "CREATE INDEX IF NOT EXISTS i ON x(n) WHERE n IS NOT NULL")
    assert translate("CREATE TABLE t (a TEXT)").kind is StatementKind.DDL
    assert translate("DROP TABLE t").kind is StatementKind.DDL


@pytest.mark.parametrize(("source", "kind", "text"), [
    ("BEGIN", StatementKind.BEGIN, "BEGIN"),
    ("begin immediate transaction", StatementKind.BEGIN, "BEGIN"),
    ("BEGIN EXCLUSIVE", StatementKind.BEGIN, "BEGIN"),
    ("BEGIN ISOLATION LEVEL SERIALIZABLE", StatementKind.BEGIN, "BEGIN ISOLATION LEVEL SERIALIZABLE"),
    ("BEGIN READ ONLY", StatementKind.BEGIN, "BEGIN READ ONLY"),
    ("COMMIT", StatementKind.COMMIT, "COMMIT"),
    ("END TRANSACTION", StatementKind.COMMIT, "COMMIT TRANSACTION"),
    ("ROLLBACK", StatementKind.ROLLBACK, "ROLLBACK"),
    ("ROLLBACK TO SAVEPOINT sp", StatementKind.ROLLBACK_TO, "ROLLBACK TO SAVEPOINT sp"),
    ("SAVEPOINT sp", StatementKind.SAVEPOINT, "SAVEPOINT sp"),
    ("RELEASE SAVEPOINT sp", StatementKind.RELEASE, "RELEASE SAVEPOINT sp"),
    ("  -- leading comment\n SELECT 1", StatementKind.SELECT, "SELECT 1"),
    ("VALUES (1)", StatementKind.SELECT, "VALUES (1)"),
    ("WITH x AS (SELECT 1) SELECT * FROM x", StatementKind.SELECT, "WITH x AS (SELECT 1) SELECT * FROM x"),
    ("UPDATE t SET a = 1", StatementKind.UPDATE, "UPDATE t SET a = 1"),
    ("DELETE FROM t", StatementKind.DELETE, "DELETE FROM t"),
    ("ANALYZE", StatementKind.OTHER, "ANALYZE"),
    ("", StatementKind.EMPTY, ""),
    ("  ;  ", StatementKind.EMPTY, ""),
])
def test_statement_kinds(source, kind, text):
    translation = translate(source)
    assert translation.kind is kind
    assert " ".join(translation.sql.split()) == text


def test_dml_classification_follows_sqlite3_implicit_transactions():
    for statement in ("INSERT INTO t VALUES (1)", "REPLACE INTO t VALUES (1)", "UPDATE t SET a = 1",
                      "DELETE FROM t"):
        assert translate(statement).is_dml
    for statement in ("SELECT 1", "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", "CREATE TABLE u (a TEXT)"):
        assert not translate(statement).is_dml


@pytest.mark.parametrize(("source", "name", "argument", "assignment"), [
    ("PRAGMA table_info(knowledge)", "table_info", "knowledge", False),
    ("PRAGMA table_info('knowledge')", "table_info", "knowledge", False),
    ("pragma main.index_list(t)", "index_list", "t", False),
    ("PRAGMA journal_mode=WAL", "journal_mode", "WAL", True),
    ("PRAGMA busy_timeout = 5000", "busy_timeout", "5000", True),
    ("PRAGMA cache_size=-20000", "cache_size", "-20000", True),
    ("PRAGMA data_version", "data_version", None, False),
])
def test_pragma_parsing(source, name, argument, assignment):
    translation = translate(source)
    assert translation.kind is StatementKind.PRAGMA
    assert (translation.pragma.name, translation.pragma.argument, translation.pragma.assignment) == (
        name, argument, assignment)


def test_last_insert_rowid_and_json_object_parameters():
    assert translate("SELECT last_insert_rowid()").kind is StatementKind.LAST_INSERT_ROWID
    assert translate("select LAST_INSERT_ROWID();").kind is StatementKind.LAST_INSERT_ROWID
    assert translate("SELECT last_insert_rowid() + 1").kind is StatementKind.SELECT
    statement = translate("SELECT ?, json_object('a', ?, 'b', lower(?)), ?")
    assert statement.text_parameters == (1, 2)
    named = translate("SELECT json_object('a', :value), :other")
    assert named.text_parameters == ("value",)


def test_split_statements_handles_literals_and_trigger_bodies():
    script = """
        CREATE TABLE a (x TEXT DEFAULT ';');
        -- comment; with semicolon
        INSERT INTO a VALUES ('x;y');
        CREATE TRIGGER t AFTER INSERT ON a BEGIN
            UPDATE a SET x = 'z'; DELETE FROM a WHERE x = ';';
        END;
        SELECT 1
    """
    statements = split_statements(script)
    assert len(statements) == 4
    assert statements[0].startswith("CREATE TABLE a") and statements[0].endswith(";")
    assert "END;" in statements[2] and "DELETE FROM a" in statements[2]
    assert statements[3] == "SELECT 1"
    assert split_statements("  ;; -- nothing\n") == []


def test_translation_is_cached():
    translate.cache_clear()
    first = translate("SELECT 42 WHERE ? = 1")
    assert translate("SELECT 42 WHERE ? = 1") is first
    info = translate.cache_info()
    assert info.hits == 1 and info.maxsize == TRANSLATION_CACHE_SIZE
