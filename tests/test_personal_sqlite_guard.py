"""The personal install stays pure SQLite: no PostgreSQL code is loaded or reachable."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from team_memory.database_contracts import DATABASE_URL_ENV

SRC = Path(__file__).resolve().parent.parent / "src"
SUBPROCESS_TIMEOUT_SECONDS = 120
FOREIGN_DSN = "postgresql://tam:secret@127.0.0.1:5432/tam?sslmode=disable"

PROBE = """
import json, sqlite3, sys
import server
loaded_on_import = sorted(m for m in sys.modules if m.split('.')[0] == 'psycopg' or m.startswith('tam_db.pg_'))
store = server.Store()
store.save_knowledge('probe', 'Probe record about the personal SQLite store.', 'fact', project='guard')
server.Recall(store).search('personal SQLite store', project='guard', limit=3)
loaded_after_use = sorted(m for m in sys.modules if m.split('.')[0] == 'psycopg' or m.startswith('tam_db.pg_'))
print(json.dumps({
    'on_import': loaded_on_import,
    'after_use': loaded_after_use,
    'connection_type': type(store.db).__module__ + '.' + type(store.db).__qualname__,
    'is_sqlite': type(store.db) is sqlite3.Connection,
    'is_postgres': store.is_postgres,
    'background_queues': store.background_queues,
    'l2_type': type(store.v9_cache.l2).__name__,
}))
"""


def _run_probe(tmp_path: Path, extra_env: dict[str, str]) -> dict:
    home = tmp_path / "home"
    memory = tmp_path / "memory"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key != DATABASE_URL_ENV}
    env.update(HOME=str(home), USERPROFILE=str(home), TAM_MEMORY_DIR=str(memory), CLAUDE_MEMORY_DIR=str(memory),
               PYTHONPATH=str(SRC), MEMORY_ASYNC_ENRICHMENT="false", MEMORY_QUALITY_GATE_ENABLED="false",
               MEMORY_CROSS_RERANK="off", **extra_env)
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=tmp_path, env=env, capture_output=True, text=True,
                            timeout=SUBPROCESS_TIMEOUT_SECONDS, check=False)
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_personal_store_never_loads_postgres_code(tmp_path):
    probe = _run_probe(tmp_path, {})
    assert probe["on_import"] == []
    assert probe["after_use"] == []
    assert probe["is_sqlite"], probe["connection_type"]
    assert probe["is_postgres"] is False
    assert probe["background_queues"] is True
    assert probe["l2_type"] == "L2EmbeddingCache"
    assert (tmp_path / "memory" / "memory.db").is_file()


def test_team_database_url_cannot_move_the_personal_store(tmp_path):
    probe = _run_probe(tmp_path, {DATABASE_URL_ENV: FOREIGN_DSN})
    assert probe["after_use"] == []
    assert probe["is_sqlite"], probe["connection_type"]
    assert (tmp_path / "memory" / "memory.db").is_file()


def test_store_keeps_the_callers_sqlite_factory(tmp_path, monkeypatch):
    import server
    from team_memory.audit import AuditedConnection

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(connection_factory=AuditedConnection)
    try:
        assert type(store.db) is AuditedConnection
        assert isinstance(store.db, sqlite3.Connection)
        assert store.is_postgres is False
    finally:
        store.db.close()


def test_sqlite_store_database_is_the_default(tmp_path, monkeypatch):
    import server
    from tam_db.contracts import StoreDatabase

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(database=StoreDatabase.sqlite())
    try:
        assert type(store.db) is sqlite3.Connection
        assert store.database == StoreDatabase.sqlite()
    finally:
        store.db.close()


@pytest.mark.parametrize("enabled", ["true", "1", "on"])
def test_async_enrichment_is_refused_for_postgres(monkeypatch, enabled):
    import enrichment_worker

    monkeypatch.setenv("MEMORY_ASYNC_ENRICHMENT", enabled)
    with pytest.raises(enrichment_worker.AsyncEnrichmentUnsupported):
        enrichment_worker.reject_postgres()


def test_async_enrichment_off_is_accepted_for_postgres(monkeypatch):
    import enrichment_worker

    monkeypatch.setenv("MEMORY_ASYNC_ENRICHMENT", "false")
    enrichment_worker.reject_postgres()


def test_lease_heartbeat_refuses_a_postgres_connection():
    import logging

    from memory_core.leases import LeaseHeartbeat, LeaseUnsupported
    from tam_db.contracts import Backend

    class PostgresLike:
        backend = Backend.POSTGRES

    with pytest.raises(LeaseUnsupported):
        LeaseHeartbeat(PostgresLike(), [(1, "token")], 1.0, logging.getLogger(__name__))


def test_disabled_l2_cache_opens_no_database(tmp_path):
    from cache_layer import DisabledL2Cache, TwoLevelCache

    cache = TwoLevelCache(db_path=tmp_path / "memory.db", l2_enabled=False)
    assert isinstance(cache.l2, DisabledL2Cache)
    assert cache.embed_set("text", [0.1, 0.2], "model") is False
    assert cache.embed_get("text") is None
    assert cache.stats()["l2_size"] == 0
    cache.close()
    assert not (tmp_path / "memory.db").exists()


def test_personal_store_still_fills_background_queues(tmp_path, monkeypatch):
    """Decision D4 is team-only: the personal install keeps feeding its reflection queues."""
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store()
    try:
        store.save_knowledge("probe", "Reflection queues are drained by the personal runner.", "fact")
        for table in ("triple_extraction_queue", "deep_enrichment_queue", "representations_queue"):
            assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table
    finally:
        store.db.close()
