"""SQLite -> PostgreSQL copier and verification (team_memory.migrate_to_pg)."""
import sqlite3
from contextlib import closing

import numpy as np
import pytest

from team_memory.database_contracts import DatabaseKind

psycopg = pytest.importorskip("psycopg", reason="install the postgres extra: pip install '.[postgres]'")

from psycopg import sql

from team_memory.migrate_to_pg import (
    ConversionError,
    MigrationCancelled,
    MigrationError,
    SourceDatabase,
    Throughput,
    codec_for,
    compare,
    copy_database,
    discover_sources,
    estimate,
    float32_blob,
    foreign_key_violations,
    has_rows,
    open_source,
    parse_vector,
    source_digest,
    source_tables,
    target_digest,
    truncate_copied_tables,
    vector_literal,
)
from tests.pg_migration_support import mirror_schema

SCHEMA = "ws_" + "a" * 48
VECTOR = np.array([0.1, -2.5, 3.25, 1e-7], dtype=np.float32)
KEY = "team_" + "b" * 64


class Recorder:
    def __init__(self, cancel_after_tables: int | None = None):
        self.tables: list[tuple[str, str, int]] = []
        self.rows = 0
        self.cancel_after_tables = cancel_after_tables

    def table_started(self, database: str, table: str, rows: int) -> None:
        self.tables.append((database, table, rows))

    def rows_copied(self, rows: int) -> None:
        self.rows += rows

    def checkpoint(self) -> None:
        if self.cancel_after_tables is not None and len(self.tables) >= self.cancel_after_tables:
            raise MigrationCancelled("cancelled")


def build_source(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, score REAL,
                                    importance INTEGER, payload BLOB, created_at TEXT);
            CREATE TABLE embeddings (knowledge_id INTEGER PRIMARY KEY, binary_vector BLOB NOT NULL,
                                     float32_vector BLOB NOT NULL, embed_model TEXT NOT NULL, embed_dim INTEGER NOT NULL);
            CREATE TABLE tam_requests (user_id TEXT NOT NULL, request_id TEXT NOT NULL, result TEXT,
                                       PRIMARY KEY (user_id, request_id));
            CREATE TABLE tag_links (tag TEXT, record INTEGER);
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(content);
            CREATE TABLE embedding_cache (key TEXT PRIMARY KEY, embedding BLOB NOT NULL);
        """)
        db.executemany("INSERT INTO knowledge(id, content, score, importance, payload, created_at) VALUES (?,?,?,?,?,?)", [
            (1, "Ünïcode — 日本語 and emoji 🚀", 0.5, 3, b"\x00\x01\xff", "2026-09-25T10:00:00.000Z"),
            (2, "second", None, None, None, None),
            (3, "numbers stored as text", 1e300, 7, b"", "2026-09-25T10:00:01.000Z"),
            (9, "to be deleted", 0.0, 0, None, None),
        ])
        db.execute("DELETE FROM knowledge WHERE id = 9")
        db.execute("INSERT INTO embeddings VALUES (1, ?, ?, 'test-model', 4)", (b"\x0f", VECTOR.tobytes()))
        db.executemany("INSERT INTO tam_requests VALUES (?,?,?)",
                       [("anna", "r2", "{}"), ("anna", "r1", None), ("Boris", "r1", "x")])
        db.executemany("INSERT INTO tag_links VALUES (?,?)", [("b", 1), ("a", 2), ("a", 2), (None, None)])
        db.execute("INSERT INTO knowledge_fts(rowid, content) VALUES (1, 'x')")
        db.execute("INSERT INTO embedding_cache VALUES ('k', x'00')")
        db.commit()
    return SourceDatabase(DatabaseKind.WORKSPACE, KEY, path)


# Pure units


def test_source_tables_skip_internal_fts_and_shadow_tables(tmp_path):
    source = build_source(tmp_path / "memory.db")
    with closing(open_source(source.path)) as db:
        assert source_tables(db) == ["embedding_cache", "embeddings", "knowledge", "tag_links", "tam_requests"]


def test_discover_sources_orders_control_then_workspaces_and_rejects_foreign_directories(tmp_path):
    for name in ("identity.db", "learning.db"):
        sqlite3.connect(tmp_path / name).close()
    for key in (KEY, "shared"):
        (tmp_path / "workspaces" / key).mkdir(parents=True)
        sqlite3.connect(tmp_path / "workspaces" / key / "memory.db").close()
    sources = discover_sources(tmp_path)
    assert [(s.kind, s.name) for s in sources] == [
        (DatabaseKind.IDENTITY, "identity"), (DatabaseKind.LEARNING, "learning"),
        (DatabaseKind.WORKSPACE, "shared"), (DatabaseKind.WORKSPACE, KEY)]
    (tmp_path / "workspaces" / "evil").mkdir()
    sqlite3.connect(tmp_path / "workspaces" / "evil" / "memory.db").close()
    with pytest.raises(MigrationError, match="Unexpected workspace directory"):
        discover_sources(tmp_path)


def test_discover_sources_requires_identity(tmp_path):
    with pytest.raises(MigrationError, match="identity database"):
        discover_sources(tmp_path)


@pytest.mark.parametrize(("pg_type", "value", "expected"), [
    ("bigint", "42", 42), ("bigint", 7.0, 7), ("integer", None, None),
    ("double precision", 3, 3.0), ("double precision", "2.5", 2.5),
    ("text", 12, "12"), ("text", "é".encode(), "é"),
    ("bytea", "abc", b"abc"), ("boolean", 1, True), ("jsonb", '{"a": 1}', '{"a": 1}'),
])
def test_codecs_convert_sqlite_values(pg_type, value, expected):
    assert codec_for(pg_type).to_target(value) == expected


@pytest.mark.parametrize(("pg_type", "value", "message"), [
    ("bigint", 3.7, "fractional"), ("bigint", "abc", "integer"), ("text", "a\x00b", "NUL"),
    ("text", b"\xff\xfe", "UTF-8"), ("boolean", 2, "0 and 1"), ("jsonb", "{broken", "JSON"),
    ("double precision", "x", "not a number"),
])
def test_codecs_reject_values_postgres_cannot_hold(pg_type, value, message):
    with pytest.raises(ConversionError, match=message):
        codec_for(pg_type).to_target(value)


def test_real_columns_round_to_float32():
    assert codec_for("real").to_target(0.1) == float(np.float32(0.1))


def test_unknown_column_type_is_refused():
    with pytest.raises(MigrationError, match="no SQLite conversion"):
        codec_for("timestamp with time zone")


def test_vector_literal_round_trips_float32_exactly():
    assert parse_vector(vector_literal(VECTOR)) == VECTOR.tobytes()
    tiny = np.array([np.finfo(np.float32).tiny, np.finfo(np.float32).max, -0.0], dtype=np.float32)
    assert parse_vector(vector_literal(tiny)) == tiny.tobytes()


@pytest.mark.parametrize("blob", [b"", b"\x00\x00\x00", np.array([np.nan], dtype=np.float32).tobytes(), "text"])
def test_float32_blob_rejects_malformed_vectors(blob):
    with pytest.raises(ConversionError):
        float32_blob(blob)


def test_foreign_key_violations_are_reported(tmp_path):
    path = tmp_path / "fk.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE parent (id INTEGER PRIMARY KEY);"
                         "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));"
                         "INSERT INTO child VALUES (1, 99);")
        db.commit()
    assert foreign_key_violations(path) == ["child"]


def test_estimate_counts_rows_and_bytes(tmp_path):
    source = build_source(tmp_path / "memory.db")
    result = estimate(source)
    rows = {table.name: table.rows for table in result.tables}
    assert rows == {"embedding_cache": 1, "embeddings": 1, "knowledge": 3, "tag_links": 4, "tam_requests": 3}
    assert result.rows == 12 and result.bytes > 0


def test_throughput_estimate_uses_the_slower_bound():
    speed = Throughput(rows_per_second=100.0, bytes_per_second=1000.0, seconds_per_database=1.0)
    assert speed.seconds([]) == 0
    source = estimate_from(rows=1000, size=500)
    assert speed.seconds([source]) == 11


def estimate_from(rows: int, size: int):
    from team_memory.database_contracts import DatabaseEstimate, TableEstimate

    return DatabaseEstimate(kind=DatabaseKind.WORKSPACE, name="shared",
                            tables=(TableEstimate(name="knowledge", rows=rows, bytes=size),))


# PostgreSQL


@pytest.fixture
def pg(pg_database):
    with psycopg.connect(pg_database.url, autocommit=True) as connection:
        yield connection


@pytest.fixture
def copied(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    mirror_schema(source.path, pg, SCHEMA)
    pg.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(SCHEMA, "embedding_cache")))
    pg.execute(f"""CREATE FUNCTION {SCHEMA}.forbid() RETURNS trigger LANGUAGE plpgsql AS
                   $$ BEGIN RAISE EXCEPTION 'trigger fired'; END $$""")
    pg.execute(f"CREATE TRIGGER guard BEFORE INSERT ON {SCHEMA}.knowledge FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.forbid()")
    recorder = Recorder()
    digest = copy_database(source, pg, SCHEMA, recorder)
    return source, digest, recorder


@pytest.mark.postgres
def test_copy_preserves_every_value_and_verifies(copied, pg):
    source, digest, recorder = copied
    assert compare(digest, target_digest(source, pg, SCHEMA)) == []
    assert compare(source_digest(source, pg, SCHEMA), digest) == []
    assert recorder.rows == 11
    assert [table for _, table, _ in recorder.tables] == ["embeddings", "knowledge", "tag_links", "tam_requests"]
    rows = pg.execute(f"SELECT id, content, score, importance, payload FROM {SCHEMA}.knowledge ORDER BY id").fetchall()
    assert rows == [(1, "Ünïcode — 日本語 and emoji 🚀", 0.5, 3, b"\x00\x01\xff"), (2, "second", None, None, None),
                    (3, "numbers stored as text", 1e300, 7, b"")]
    stored = pg.execute(f"SELECT embedding::text FROM {SCHEMA}.embeddings").fetchone()[0]
    assert parse_vector(stored) == VECTOR.tobytes()
    assert pg.execute(f"SELECT count(*) FROM {SCHEMA}.tag_links").fetchone()[0] == 4


@pytest.mark.postgres
def test_identity_continues_after_the_autoincrement_high_water_mark(copied, pg):
    pg.execute(f"DROP TRIGGER guard ON {SCHEMA}.knowledge")
    new_id = pg.execute(f"INSERT INTO {SCHEMA}.knowledge (content) VALUES ('after') RETURNING id").fetchone()[0]
    assert new_id == 10


@pytest.mark.postgres
def test_user_triggers_are_enabled_again_after_the_copy(copied, pg):
    with pytest.raises(psycopg.errors.RaiseException, match="trigger fired"):
        pg.execute(f"INSERT INTO {SCHEMA}.knowledge (content) VALUES ('after')")


@pytest.mark.postgres
@pytest.mark.parametrize(("statement", "reason"), [
    (f"UPDATE {SCHEMA}.knowledge SET content = 'tampered' WHERE id = 2", "row contents differ"),
    (f"DELETE FROM {SCHEMA}.tag_links WHERE tag = 'a'", "4 rows in SQLite, 2 in PostgreSQL"),
    (f"UPDATE {SCHEMA}.embeddings SET embedding = '[0,0,0,0]'", "row contents differ"),
    (f"UPDATE {SCHEMA}.knowledge SET score = score + 1e-12 WHERE id = 1", "row contents differ"),
])
def test_verification_detects_any_difference(copied, pg, statement, reason):
    source, digest, _ = copied
    pg.execute(f"ALTER TABLE {SCHEMA}.knowledge DISABLE TRIGGER guard")
    pg.execute(statement)
    mismatches = compare(digest, target_digest(source, pg, SCHEMA))
    assert [mismatch.reason for mismatch in mismatches] == [reason]


@pytest.mark.postgres
def test_copy_refuses_a_non_empty_target(copied, pg):
    source, _, _ = copied
    with pytest.raises(MigrationError, match="not empty"):
        copy_database(source, pg, SCHEMA, Recorder())
    assert has_rows(source, pg, SCHEMA)
    with pg.transaction():
        truncate_copied_tables(source, pg, SCHEMA)
    assert not has_rows(source, pg, SCHEMA)


@pytest.mark.postgres
def test_cancel_rolls_the_whole_database_back(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    mirror_schema(source.path, pg, SCHEMA)
    with pytest.raises(MigrationCancelled):
        copy_database(source, pg, SCHEMA, Recorder(cancel_after_tables=3))
    assert not has_rows(source, pg, SCHEMA)
    assert pg.execute(f"SELECT count(*) FROM {SCHEMA}.embedding_cache").fetchone()[0] == 0
    disabled = pg.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                          "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND t.tgenabled = 'D'",
                          (SCHEMA,)).fetchone()[0]
    assert disabled == 0


@pytest.mark.postgres
def test_cache_tables_without_a_counterpart_are_skipped_but_others_fail(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    mirror_schema(source.path, pg, SCHEMA)
    pg.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(SCHEMA, "tag_links")))
    with pytest.raises(MigrationError, match="has no table tag_links"):
        copy_database(source, pg, SCHEMA, Recorder())


@pytest.mark.postgres
def test_missing_target_column_is_refused(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    mirror_schema(source.path, pg, SCHEMA)
    pg.execute(f"ALTER TABLE {SCHEMA}.knowledge DROP COLUMN payload")
    with pytest.raises(MigrationError, match="lacks columns payload"):
        copy_database(source, pg, SCHEMA, Recorder())


@pytest.mark.postgres
def test_unconvertible_value_names_table_and_column(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    with closing(sqlite3.connect(source.path)) as db:
        db.execute("INSERT INTO knowledge(content, importance) VALUES ('bad', 2.5)")
        db.commit()
    mirror_schema(source.path, pg, SCHEMA)
    with pytest.raises(MigrationError, match=r'knowledge\.importance row \{"id": 10\}: a fractional number'):
        copy_database(source, pg, SCHEMA, Recorder())
    assert not has_rows(source, pg, SCHEMA)


@pytest.mark.postgres
def test_seeded_tables_merge_and_verify_on_the_sqlite_keys(tmp_path, pg):
    path = tmp_path / "learning.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                         "INSERT INTO meta VALUES ('instance_id', 'from-sqlite'), ('schema', '1');")
        db.commit()
    source = SourceDatabase(DatabaseKind.LEARNING, "learning", path)
    mirror_schema(path, pg, "tam_learning")
    pg.execute("INSERT INTO tam_learning.meta VALUES ('instance_id', 'seeded'), ('pg_only', 'x')")
    seeded = frozenset({"meta"})
    digest = copy_database(source, pg, "tam_learning", Recorder(), merge=seeded)
    assert compare(digest, target_digest(source, pg, "tam_learning", seeded)) == []
    assert pg.execute("SELECT key, value FROM tam_learning.meta ORDER BY key").fetchall() == [
        ("instance_id", "from-sqlite"), ("pg_only", "x"), ("schema", "1")]
    with pg.transaction():
        truncate_copied_tables(source, pg, "tam_learning", seeded)
    assert pg.execute("SELECT count(*) FROM tam_learning.meta").fetchone()[0] == 3
    pg.execute("UPDATE tam_learning.meta SET value = 'other' WHERE key = 'instance_id'")
    assert [m.reason for m in compare(digest, target_digest(source, pg, "tam_learning", seeded))] == [
        "row contents differ"]


@pytest.mark.postgres
def test_post_copy_hooks_run_inside_the_copy_transaction(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    mirror_schema(source.path, pg, SCHEMA)
    pg.execute(f"CREATE TABLE {SCHEMA}.fts_stats (docs bigint)")
    seen = []

    def rebuild(connection, schema):
        seen.append(connection.info.transaction_status)
        connection.execute(sql.SQL("INSERT INTO {} SELECT count(*) FROM {}").format(
            sql.Identifier(schema, "fts_stats"), sql.Identifier(schema, "knowledge")))

    copy_database(source, pg, SCHEMA, Recorder(), post_copy=(rebuild,))
    assert seen == [psycopg.pq.TransactionStatus.INTRANS]
    assert pg.execute(f"SELECT docs FROM {SCHEMA}.fts_stats").fetchone()[0] == 3


@pytest.fixture
def workspace_db(tmp_path, monkeypatch):
    """A real team workspace memory.db written through the worker Runtime."""
    import server
    from team_memory.contracts import Actor, Save, Scope, Work, Workspace
    from team_memory.worker import Runtime

    data_dir = tmp_path / "workspace"
    monkeypatch.setattr(server, "MEMORY_DIR", data_dir)
    for key, value in {"TAM_MEMORY_DIR": str(data_dir), "CLAUDE_MEMORY_DIR": str(data_dir),
                       "MEMORY_QUALITY_GATE_ENABLED": "false", "MEMORY_ASYNC_ENRICHMENT": "false",
                       "USE_BINARY_SEARCH": "true"}.items():
        monkeypatch.setenv(key, value)
    runtime = Runtime(str(data_dir))
    actor = Actor(user_id="anna", display_name="Anna", client="test")
    workspace = Workspace(key="shared", scope=Scope(), writable=True)
    try:
        for content in ("Deploys happen on Tuesdays after the change review.",
                        "The billing service retries webhooks five times with backoff.",
                        "Ünïcode note: 東京 office closes at 18:00."):
            runtime.execute(Work(actor=actor, workspace=workspace, operation="memory_save",
                                 arguments=Save(content=content, project="ops").model_dump(mode="json", exclude={"scope"})))
    finally:
        runtime.store.db.close()
    return SourceDatabase(DatabaseKind.WORKSPACE, "shared", data_dir / "memory.db")


@pytest.mark.postgres
def test_real_workspace_database_copies_and_verifies(workspace_db, pg):
    mirror_schema(workspace_db.path, pg, SCHEMA)
    digest = copy_database(workspace_db, pg, SCHEMA, Recorder())
    assert compare(digest, target_digest(workspace_db, pg, SCHEMA)) == []
    assert digest.table("knowledge").rows == 3
    assert pg.execute(f"SELECT count(*) FROM {SCHEMA}.embeddings WHERE embedding IS NOT NULL").fetchone()[0] == \
        digest.table("embeddings").rows > 0
    assert digest.table("tam_history").rows == 3


# Orphans and quarantine


def build_orphans(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE graph_nodes (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE knowledge (id INTEGER PRIMARY KEY, content TEXT, payload BLOB);
            CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY, knowledge_id INTEGER REFERENCES knowledge(id),
                                          node_id TEXT REFERENCES graph_nodes(id), payload BLOB);
            CREATE TABLE node_notes (link_id INTEGER REFERENCES knowledge_nodes(id), note TEXT);
            CREATE TABLE tam_history (sequence INTEGER PRIMARY KEY, record_id INTEGER REFERENCES knowledge(id),
                                      operation TEXT);
            INSERT INTO graph_nodes VALUES ('n1', 'alpha');
            INSERT INTO knowledge VALUES (1, 'kept', NULL), (2, 'kept too', x'00ff');
            INSERT INTO knowledge_nodes VALUES (10, 1, 'n1', NULL), (11, 2, 'gone', x'01'), (12, 99, 'n1', NULL),
                                               (13, NULL, NULL, NULL);
            INSERT INTO node_notes VALUES (10, 'fine'), (11, 'parent is an orphan'), (NULL, 'no parent needed');
            INSERT INTO tam_history VALUES (1, 1, 'insert'), (2, 42, 'insert');
        """)
        db.commit()
    return SourceDatabase(DatabaseKind.WORKSPACE, KEY, path)


def test_orphans_cascade_to_a_fixpoint_and_cover_foreign_key_check(tmp_path):
    from team_memory.migrate_to_pg import find_orphans, quarantine_summary

    source = build_orphans(tmp_path / "memory.db")
    with closing(open_source(source.path)) as db:
        orphans = find_orphans(db)
        first_level = {(row[0], row[1]) for row in db.execute("PRAGMA foreign_key_check")}
    assert orphans == {
        "knowledge_nodes": {(11,): "fk:knowledge_nodes.node_id->graph_nodes.id",
                            (12,): "fk:knowledge_nodes.knowledge_id->knowledge.id"},
        "node_notes": {(2,): "fk:node_notes.link_id->knowledge_nodes.id"},
        "tam_history": {(2,): "fk:tam_history.record_id->knowledge.id"},
    }
    found = {(table, ident[0]) for table, rows in orphans.items() for ident in rows}
    assert first_level <= found
    summary = {item.table: item for item in quarantine_summary(source)}
    assert summary["knowledge_nodes"].rows == 2 and summary["knowledge_nodes"].sample_pks == ('{"id": 11}',
                                                                                               '{"id": 12}')
    assert summary["node_notes"].sample_pks == ('{"rowid": 2}',)
    assert summary["tam_history"].audit and not summary["knowledge_nodes"].audit


@pytest.fixture
def quarantined(tmp_path, pg):
    from tests.pg_migration_support import create_quarantine

    source = build_orphans(tmp_path / "memory.db")
    mirror_schema(source.path, pg, SCHEMA)
    create_quarantine(pg, SCHEMA)
    digest = copy_database(source, pg, SCHEMA, Recorder())
    return source, digest


@pytest.mark.postgres
def test_orphans_are_quarantined_and_counted_by_verification(quarantined, pg):
    from team_memory.migrate_to_pg import quarantine_summary

    source, digest = quarantined
    assert compare(digest, target_digest(source, pg, SCHEMA)) == []
    counts = {table.table: (table.rows, table.quarantined) for table in digest.tables}
    assert counts["knowledge_nodes"] == (2, 2) and counts["node_notes"] == (2, 1) and counts["tam_history"] == (1, 1)
    assert pg.execute(f"SELECT id FROM {SCHEMA}.knowledge_nodes ORDER BY id").fetchall() == [(10,), (13,)]
    rows = pg.execute(f"SELECT source_table, source_pk, row, reason FROM {SCHEMA}.migration_quarantine "
                      "ORDER BY source_table, source_pk").fetchall()
    assert rows[0] == ("knowledge_nodes", '{"id": 11}',
                       {"id": 11, "knowledge_id": 2, "node_id": "gone", "payload": {"hex": "01"}},
                       "fk:knowledge_nodes.node_id->graph_nodes.id")
    planned = {(item.table, pk) for item in quarantine_summary(source) for pk in item.sample_pks}
    assert planned == {(table, pk) for table, pk, _, _ in rows}


@pytest.mark.postgres
def test_verification_notices_a_lost_quarantine_row(quarantined, pg):
    source, digest = quarantined
    pg.execute(f"DELETE FROM {SCHEMA}.migration_quarantine WHERE source_table = 'tam_history'")
    assert [m.reason for m in compare(digest, target_digest(source, pg, SCHEMA))] == [
        "1 orphan rows in SQLite, 0 quarantined in PostgreSQL"]


@pytest.mark.postgres
def test_orphans_without_a_quarantine_table_fail_the_copy(tmp_path, pg):
    source = build_orphans(tmp_path / "memory.db")
    mirror_schema(source.path, pg, SCHEMA)
    with pytest.raises(MigrationError, match="no migration_quarantine table"):
        copy_database(source, pg, SCHEMA, Recorder())
    assert not has_rows(source, pg, SCHEMA)


@pytest.mark.postgres
def test_postgres_only_columns_without_a_source_are_refused(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    mirror_schema(source.path, pg, SCHEMA)
    pg.execute(f"ALTER TABLE {SCHEMA}.knowledge ADD COLUMN surprise text")
    with pytest.raises(MigrationError, match="knowledge.surprise has no SQLite source"):
        copy_database(source, pg, SCHEMA, Recorder())


@pytest.mark.postgres
def test_ledger_is_checked_not_copied(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    with closing(sqlite3.connect(source.path)) as db:
        db.execute("CREATE TABLE migrations (version TEXT PRIMARY KEY, description TEXT, applied_at TEXT)")
        db.execute("INSERT INTO migrations VALUES ('001', 'schema', '2026-01-01')")
        db.commit()
    mirror_schema(source.path, pg, SCHEMA)
    ledgers = frozenset({"migrations"})
    with pytest.raises(MigrationError, match="newer TAM .migrations 001"):
        copy_database(source, pg, SCHEMA, Recorder(), ledgers=ledgers)
    pg.execute(f"INSERT INTO {SCHEMA}.migrations VALUES ('001', 'schema', 'built in'), ('002', 'later', 'x')")
    digest = copy_database(source, pg, SCHEMA, Recorder(), ledgers=ledgers)
    assert digest.table("migrations") is None
    assert compare(digest, target_digest(source, pg, SCHEMA, ledgers=ledgers)) == []
    assert pg.execute(f"SELECT count(*) FROM {SCHEMA}.migrations").fetchone()[0] == 2


def test_jsonb_refuses_escaped_nul_characters():
    with pytest.raises(ConversionError, match="NUL"):
        codec_for("jsonb").to_target('{"key": "a\\u0000b"}')
    with pytest.raises(ConversionError, match="NUL"):
        codec_for("jsonb").to_target('{"a\\u0000": 1}')
    assert codec_for("jsonb").to_target('{"key": "a\\\\u0000b"}') == '{"key": "a\\\\u0000b"}'


def test_text_problems_name_table_column_and_key(tmp_path):
    from team_memory.migrate_to_pg import text_problems

    source = build_source(tmp_path / "memory.db")
    with closing(sqlite3.connect(source.path)) as db:
        db.execute("INSERT INTO knowledge (id, content) VALUES (5, 'a' || char(0))")
        db.execute("INSERT INTO knowledge (id, content, created_at) VALUES (6, 'fine', x'ff')")
        db.execute("INSERT INTO knowledge (id, content, payload) VALUES (7, 'fine', x'ff')")
        db.commit()
    assert text_problems(source) == [
        f'{KEY}: knowledge.content row {{"id": 5}}: text contains NUL characters, which PostgreSQL cannot store',
        f'{KEY}: knowledge.created_at row {{"id": 6}}: a BLOB in a text column is not UTF-8']


@pytest.mark.postgres
def test_nul_in_text_fails_the_copy_with_its_row_key(tmp_path, pg):
    source = build_source(tmp_path / "memory.db")
    with closing(sqlite3.connect(source.path)) as db:
        db.execute("INSERT INTO knowledge (id, content) VALUES (5, 'a' || char(0))")
        db.commit()
    mirror_schema(source.path, pg, SCHEMA)
    with pytest.raises(MigrationError, match=r'knowledge\.content row \{"id": 5\}: PostgreSQL text cannot contain NUL'):
        copy_database(source, pg, SCHEMA, Recorder())
    assert not has_rows(source, pg, SCHEMA)
