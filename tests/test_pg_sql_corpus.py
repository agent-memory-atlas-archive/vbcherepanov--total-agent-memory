"""Every SQL statement the team server issued on SQLite translates and prepares on PostgreSQL.

tests/fixtures/pg_sql_corpus.jsonl is recorded by scripts/pg_sql_corpus.py from the team
suite. Each statement is translated (tam_db.translate) and checked against a provisioned
database: queries and DML are prepared (parsed, resolved and planned, not executed), DDL
and other statements run inside a transaction that is rolled back, PRAGMAs go through the
compatibility connection.

A statement may fail only when every site that issued it is SQLite-only: the call site is
not reached on PostgreSQL because the caller has an explicit PostgreSQL branch (schema
setup, FTS5, SQLite file handling). Any other failure is SQL the PostgreSQL backend would
hit at run time, so it fails here.
"""

import importlib
import importlib.util
import json
import re
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tam_db.contracts import (
    CONTROL_SCHEMA,
    LEARNING_SCHEMA,
    PgDatabaseError,
    UntranslatableSQL,
    schema_for,
)
from tam_db.translate import (
    TRANSACTION_CONTROL_KINDS,
    StatementKind,
    split_statements,
    translate,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "pg_sql_corpus.jsonl"
RECORDER = ROOT / "scripts" / "pg_sql_corpus.py"
TARGET_SCHEMAS = {"identity": CONTROL_SCHEMA, "learning": LEARNING_SCHEMA}
WORKSPACE_KEY = "corpus-workspace"
TEXT_OID = 25
UNKNOWN_OID = 0
PLACEHOLDER = re.compile(r"%%|%s|%\((\w+)\)s")

# Call sites that never run on the PostgreSQL backend (first function of a recorded site).
SQLITE_ONLY_SITES = frozenset({
    # Store schema setup: PostgreSQL workspaces get migrations/postgres/workspace instead.
    "server.py:Store._open_sqlite",
    "server.py:Store._schema",
    "server.py:Store._migrate",
    "server.py:Store._apply_sql_migrations",
    "server.py:Store._check_fts",
    "server.py:Store._check_binary_search",
    "server.py:Store._check_embed_dim_compat",
    "server.py:Store._create_observations_table",
    "server.py:Store._create_self_improvement_tables",
    "memory_core/schema_migration.py:MigrationRunner.apply",
    "team_memory/audit.py:install",
    "memory_core/episodes/retriever.py:_fts_available",
    # Side connections disabled on PostgreSQL (plan 1.6).
    "cache_layer.py:L2EmbeddingCache.__init__",
    "cache_layer.py:L2EmbeddingCache._ensure_table",
    # Control plane: SQLite files, their legacy upgrades and file checks.
    "team_memory/registry.py:Registry._migrate",
    "team_memory/learning/repository.py:LearningRepository.__init__",
    "team_memory/database.py:SqliteControlBackend.connect",
    "team_memory/database.py:SqliteControlBackend._learning",
    "team_memory/database.py:sqlite_instance_id",
    "team_memory/lifecycle.py:verify_database",
    "team_memory/insights.py:WorkspaceReader._open",
})


# Callers that branch to memory_core.pg_fts on PostgreSQL: their FTS5 statements are
# refused by the translator (UntranslatableSQL) on purpose and never reach it at run time.
# Only UntranslatableSQL is tolerated here; any other error at these sites still fails.
FTS_BRANCH_SITES = frozenset({
    "server.py:Recall._search_impl",
    "server.py:Store._find_duplicate",
    "server.py:Store.supersede_values",
    "server.py:Recall.timeline",
    "memory_core/dedup.py:find_duplicate",
    "memory_core/atomic_facts.py:FactRepository._search",
    "memory_core/episodes/retriever.py:_bm25_fts",
    "memory_core/passage_index.py:PassageIndex.rank",
})


def _function(site: str) -> str:
    return site.split(" < ", 1)[0]


def _issuer(site: str) -> str:
    """The function that owns the SQL: the caller when the recorded function is Store.q/q1."""
    parts = site.split(" < ")
    if parts[0] in ("server.py:Store.q", "server.py:Store.q1") and len(parts) > 1:
        return parts[1]
    return parts[0]


@dataclass(frozen=True)
class Statement:
    target: str
    method: str
    sql: str
    sites: tuple[str, ...]

    @property
    def sqlite_only(self) -> bool:
        return all(_function(site) in SQLITE_ONLY_SITES for site in self.sites)

    @property
    def fts_branch(self) -> bool:
        return all(_issuer(site) in FTS_BRANCH_SITES for site in self.sites)


def load_corpus() -> list[Statement]:
    statements = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        parts = split_statements(entry["sql"]) if entry["method"] == "executescript" else [entry["sql"]]
        statements += [Statement(entry["target"], entry["method"], part, tuple(entry["sites"])) for part in parts]
    return statements


def test_corpus_is_recorded_and_covers_every_target():
    statements = load_corpus()
    targets = {statement.target for statement in statements}
    assert targets == {"workspace", "identity", "learning"}
    assert all(statement.sites for statement in statements)
    runtime = [statement for statement in statements if not statement.sqlite_only]
    assert sum(statement.target == "workspace" for statement in runtime) >= 50


@pytest.fixture(scope="module")
def provisioned(pg_server) -> Iterator[dict[str, object]]:
    import psycopg
    from psycopg import sql

    from tam_db import pg_schema
    from tam_db.pg_connection import connect_url
    from team_memory.audit import install_postgres
    from team_memory.pg_provision import PgProvisioner
    from tests.pg_support import fresh_database

    with fresh_database(pg_server) as database:
        PgProvisioner(database.url).bootstrap(str(uuid.uuid4()))
        workspace_schema = schema_for(WORKSPACE_KEY)
        with psycopg.connect(database.url, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(workspace_schema)))
        connections = {"workspace": connect_url(database.url, schema=workspace_schema)}
        pg_schema.ensure(connections["workspace"])
        # Audit tables and triggers the team worker installs on its workspace.
        install_postgres(connections["workspace"])
        for target, schema in TARGET_SCHEMAS.items():
            connections[target] = connect_url(database.url, schema=schema)
        try:
            yield connections
        finally:
            for connection in connections.values():
                connection.close()


def _numbered(text: str, parameter_names: list[str]) -> str:
    """psycopg placeholders -> $n (named ones numbered by first appearance), %% -> %."""
    counter = iter(range(1, 10_000))

    def replace(match: re.Match) -> str:
        if match.group(0) == "%%":
            return "%"
        name = match.group(1)
        if name is None:
            return f"${next(counter)}"
        if name not in parameter_names:
            parameter_names.append(name)
        return f"${parameter_names.index(name) + 1}"

    return PLACEHOLDER.sub(replace, text)


def _check(connection, statement: Statement, index: int) -> None:
    """Raise UntranslatableSQL / PgDatabaseError when the statement cannot run on PostgreSQL."""
    from psycopg.pq import ExecStatus

    from tam_db.pg_connection import map_error
    from tam_db.pragma import PgCatalog

    translation = translate(statement.sql)
    if translation.kind in TRANSACTION_CONTROL_KINDS or translation.kind in (StatementKind.EMPTY,
                                                                             StatementKind.LAST_INSERT_ROWID):
        return
    raw = connection.raw
    if translation.kind is StatementKind.PRAGMA:
        connection.execute(statement.sql).fetchall()
        return
    catalog = PgCatalog(lambda query, parameters: raw.execute(query, parameters).fetchall())
    text = translation.render(catalog) if translation.needs_catalog else translation.sql
    if translation.kind is StatementKind.INSERT and translation.insert_table and not translation.has_returning:
        shape = catalog.table(translation.insert_table)
        if shape is not None and shape.rowid_column is not None:
            text = f'{text} RETURNING "{shape.rowid_column}"'
    names: list[str] = []
    numbered = _numbered(text, names)
    if translation.kind in (StatementKind.SELECT, StatementKind.INSERT, StatementKind.UPDATE,
                            StatementKind.DELETE) or numbered.lstrip().upper().startswith("WITH"):
        count = len(names) if translation.named else translation.param_count if translation.param_order is None \
            else len(translation.param_order)
        types = [TEXT_OID if (names[position] if translation.named else position) in translation.text_parameters
                 else UNKNOWN_OID for position in range(count)]
        name = f"tam_corpus_{index}".encode()
        result = raw.pgconn.prepare(name, numbered.encode("utf-8"), types)
        if result.status != ExecStatus.COMMAND_OK:
            message = result.error_message.decode("utf-8", "replace").strip()
            raise PgDatabaseError(message, sqlstate=(result.error_field(ord("C")) or b"").decode() or None)
        raw.execute(f"DEALLOCATE tam_corpus_{index}")
        return
    # DDL and other utility statements cannot be prepared: run them and roll back.
    import psycopg

    try:
        with raw.transaction(force_rollback=True):
            raw.execute(numbered.replace("%", "%%"))
    except psycopg.Error as error:
        raise map_error(error) from error


@pytest.mark.postgres
def test_every_runtime_statement_translates_and_prepares(provisioned):
    failures = []
    for index, statement in enumerate(load_corpus()):
        try:
            _check(provisioned[statement.target], statement, index)
        except UntranslatableSQL as error:
            if not (statement.sqlite_only or statement.fts_branch):
                failures.append(f"[{statement.target}] {statement.sites[0]}: {error}\n    {statement.sql.strip()}")
        except PgDatabaseError as error:
            if not statement.sqlite_only:
                failures.append(f"[{statement.target}] {statement.sites[0]}: {error}\n    {statement.sql.strip()}")
    assert not failures, "\n".join([f"{len(failures)} statements fail on PostgreSQL:", *failures])


# ── the recorder (scripts/pg_sql_corpus.py) ──

@pytest.fixture
def recorder(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("pg_sql_corpus_under_test", RECORDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sqlite3, "connect", sqlite3.connect)
    monkeypatch.setenv(module.DIRECTORY_ENV, str(tmp_path / "records"))
    (tmp_path / "records").mkdir()
    return module


def test_recorder_maps_database_files_to_targets(recorder, tmp_path):
    assert recorder._target(tmp_path / "workspaces" / "k" / "memory.db") == "workspace"
    assert recorder._target(str(tmp_path / "identity.db")) == "identity"
    assert recorder._target(f"file:{tmp_path / 'learning.db'}?mode=ro") == "learning"
    assert recorder._target(":memory:") is None
    assert recorder._target(tmp_path / "other.db") is None


def test_recorder_records_source_sites_and_merges(recorder, tmp_path, monkeypatch):
    # A stand-in source tree: SQL issued from modules under it is recorded, test code is not.
    source_root = tmp_path / "src"
    (source_root / "probe").mkdir(parents=True)
    (source_root / "probe" / "issuer.py").write_text(
        "import sqlite3\n\n\n"
        "def issue(path):\n"
        "    db = sqlite3.connect(path)\n"
        "    db.execute('CREATE TABLE IF NOT EXISTS t (a TEXT)')\n"
        "    db.cursor().execute('INSERT INTO t VALUES (?)', ('x',))\n"
        "    db.executemany('INSERT INTO t VALUES (?)', [('y',)])\n"
        "    db.close()\n", encoding="utf-8")
    monkeypatch.setattr(recorder, "SOURCE_ROOT", source_root)
    monkeypatch.syspath_prepend(str(source_root))
    recorder.install()
    issuer = importlib.import_module("probe.issuer")
    try:
        issuer.issue(str(tmp_path / "memory.db"))
        issuer.issue(str(tmp_path / "memory.db"))
    finally:
        for name in ("probe.issuer", "probe"):
            sys.modules.pop(name, None)
    direct = sqlite3.connect(tmp_path / "identity.db")
    direct.execute("SELECT 1")
    direct.close()
    entries = recorder.merge(tmp_path / "records")
    assert [(entry["target"], entry["method"], entry["sql"]) for entry in entries] == [
        ("workspace", "execute", "CREATE TABLE IF NOT EXISTS t (a TEXT)"),
        ("workspace", "execute", "INSERT INTO t VALUES (?)"),
        ("workspace", "executemany", "INSERT INTO t VALUES (?)"),
    ]
    assert all(entry["sites"] == ["probe/issuer.py:issue"] for entry in entries)


def test_default_suite_excludes_sqlite_only_files(recorder):
    tests = recorder.default_tests()
    assert tests and all(test.startswith("tests/test_team_") for test in tests)
    assert not recorder.EXCLUDED_TEST_FILES & set(tests)
    assert sys.executable
