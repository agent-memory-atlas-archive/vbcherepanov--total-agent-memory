"""memory_delete(hard=true): the record, its earlier versions and every derived copy leave the disk."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.pg_store_support import store_backend  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

NEEDLE = "Zanzibarquokka"
OLD = f"Customer {NEEDLE} asked to cancel the enterprise contract in March"
NEW = f"Customer {NEEDLE} renewed the enterprise contract for two years"


@pytest.fixture
def live_server(store_backend, monkeypatch, tmp_path):  # noqa: F811 — pytest fixture injection
    for sub in ("raw", "blobs", "chroma"):
        (tmp_path / sub).mkdir(exist_ok=True)
    import server
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    store = server.Store(database=store_backend)
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "recall", server.Recall(store))
    monkeypatch.setattr(server, "SID", "sess-erase")
    monkeypatch.setattr(server, "BRANCH", "")
    monkeypatch.setattr(server, "_v5_modules", {})
    store.db.execute("INSERT INTO sessions (id, started_at, project, status) VALUES (?, ?, ?, ?)",
                     (server.SID, "2026-09-28T00:00:00Z", "p", "open"))
    store.db.commit()
    yield server, store, tmp_path
    store.db.close()


def call(server, tool, **args):
    content, is_error = asyncio.run(server._call_tool_impl(tool, args))
    return json.loads(content[0].text) if content[0].text.startswith("{") else content[0].text, is_error


def files_containing(root: Path, needle: str) -> list[str]:
    """Case-insensitive: FTS5 stores tokens lower-cased."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path.is_file() and needle.lower().encode() in path.read_bytes().lower())


def test_hard_delete_erases_record_versions_and_every_copy(live_server):
    server, store, root = live_server
    if store.is_postgres:
        pytest.skip("personal memory is SQLite; the Postgres refusal is covered below")
    first, _ = call(server, "memory_save", type="fact", project="p", content=OLD)
    call(server, "memory_save", type="fact", project="p", content="Unrelated note about CI caching")
    updated, _ = call(server, "memory_update", id=first["id"], new_content=NEW, reason="renewal")
    call(server, "memory_recall", query="enterprise contract cancellation", project="p")
    successor, _ = call(server, "memory_save", type="fact", project="p",
                        content="Enterprise renewals are tracked in the CRM pipeline")
    store.db.execute("UPDATE knowledge SET context=? WHERE id=?", (f"Was: {NEW[:200]}", successor["id"]))
    store.db.commit()
    chroma = store._chroma_collection_for("text")
    if chroma is not None:
        vector = [0.1] * 384
        chroma.upsert(ids=[str(first["id"]), str(updated["new_id"])], embeddings=[vector, vector],
                      documents=[OLD, NEW])

    result, is_error = call(server, "memory_delete", id=updated["new_id"], hard=True)

    assert not is_error, result
    assert sorted(result["erased"]) == sorted([first["id"], updated["new_id"]])
    assert result["raw_logs_rewritten"] >= 1
    assert store.db.execute("SELECT COUNT(*) FROM knowledge WHERE id IN (?, ?)",
                            (first["id"], updated["new_id"])).fetchone()[0] == 0
    assert store.db.execute("SELECT context FROM knowledge WHERE id=?", (successor["id"],)).fetchone()[0] \
        == "Was: [erased]"
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert files_containing(root, NEEDLE) == []
    found, _ = call(server, "memory_recall", query=NEEDLE, project="p")
    assert NEEDLE not in json.dumps(found["results"])
    assert store.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0] == 2


def test_soft_delete_is_still_the_default(live_server):
    server, store, _ = live_server
    saved, _ = call(server, "memory_save", type="fact", project="p", content=OLD)
    result, _ = call(server, "memory_delete", id=saved["id"])
    assert result["deleted"] is True and "hard" not in result
    assert store.db.execute("SELECT status FROM knowledge WHERE id=?", (saved["id"],)).fetchone()[0] == "deleted"


def test_hard_delete_of_a_missing_record_reports_it(live_server):
    server, store, _ = live_server
    if store.is_postgres:
        pytest.skip("personal memory is SQLite")
    result, is_error = call(server, "memory_delete", id=987654, hard=True)
    assert not is_error
    assert result == {"error": "Record not found", "id": 987654}


def test_hard_delete_refuses_a_postgres_store(live_server):
    server, store, _ = live_server
    if not store.is_postgres:
        pytest.skip("Postgres-only refusal")
    saved, _ = call(server, "memory_save", type="fact", project="p", content=OLD)
    result, is_error = call(server, "memory_delete", id=saved["id"], hard=True)
    assert is_error
    assert "SQLite" in result
